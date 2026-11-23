# LLM synthesis: chunked summaries + "where are the diagrams" timestamps
import json
import logging
import re

import httpx
import redis

from common import config
from common.ratelimit import Limiter

from . import store
from .errors import Fatal, RateLimited, Retryable, chaos

log = logging.getLogger("worker.llm")
limiter = Limiter(redis.Redis.from_url(config.REDIS_URL, decode_responses=True))

SYSTEM = ("You write clear, compact study notes from video transcripts. "
          "You always answer with a single JSON object and nothing else.")


def mmss(sec):
    sec = int(sec)
    return f"{sec // 60:02d}:{sec % 60:02d}"


def _retry_after(res):
    for h in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
        v = res.headers.get(h)
        if v:
            m = re.match(r"([\d.]+)", v)
            if m:
                return float(m.group(1))
    return 0


def parse_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except ValueError:
        a, b = text.find("{"), text.rfind("}")
        if a != -1 and b > a:
            try:
                return json.loads(text[a:b + 1])
            except ValueError:
                pass
    raise Retryable("LLM_BAD_JSON", "LLM answer was not valid JSON")


def chat(prompt, jid=None, max_tokens=None):
    max_tokens = max_tokens or config.LLM_MAX_TOKENS
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
    # rough token estimate: ~3 chars per token for input, plus whatever we let it write
    est = (len(SYSTEM) + len(prompt)) // 3 + max_tokens

    def waiting(w):
        store.update(jid, detail=f"Waiting ~{int(w)}s for an LLM slot (shared rate limit)")

    # 63s instead of 60: requests reach the provider a little after we count them, so a call let
    # through exactly at the 60s edge could still land inside the provider's window. small margin fixes it
    ticket = limiter.take("llm", 63, max_n=config.LLM_RPM, max_cost=config.LLM_TPM, cost=est, on_wait=waiting)
    chaos("llm")

    body = {"model": config.LLM_MODEL, "messages": msgs, "temperature": 0.2, "max_tokens": max_tokens}
    if config.LLM_JSON_MODE:
        body["response_format"] = {"type": "json_object"}
    if config.LLM_REASONING_EFFORT:
        body["reasoning_effort"] = config.LLM_REASONING_EFFORT
    try:
        res = httpx.post(f"{config.LLM_BASE_URL}/chat/completions", json=body, timeout=120,
                         headers={"Authorization": f"Bearer {config.LLM_API_KEY}"})
    except httpx.HTTPError as e:
        raise Retryable("LLM_UNREACHABLE", f"Couldn't reach the LLM provider ({e.__class__.__name__})")

    if res.status_code == 429:
        raise RateLimited("llm", "LLM provider returned 429", retry_after=_retry_after(res))
    if res.status_code in (401, 403):
        raise Fatal("LLM_AUTH", "The LLM API key was rejected - check LLM_API_KEY")
    if res.status_code == 404:
        raise Fatal("LLM_MODEL", f"LLM model '{config.LLM_MODEL}' not found - check LLM_MODEL")
    if res.status_code >= 500 or res.status_code == 408:
        raise Retryable("LLM_UNAVAILABLE", f"LLM provider error {res.status_code}")
    if res.status_code >= 400:
        txt = res.text[:300]
        if "json" in txt.lower():  # e.g. groq's json_validate_failed, usually fine on a retry
            raise Retryable("LLM_BAD_JSON", "LLM produced invalid JSON")
        raise Fatal("LLM_BAD_REQUEST", f"LLM rejected the request: {txt}")

    try:
        data = res.json()
        # we reserved a pessimistic estimate, give the unused part back to the shared budget
        limiter.settle(ticket, (data.get("usage") or {}).get("total_tokens"))
        content = data["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError):
        raise Retryable("LLM_BAD_RESPONSE", "Unexpected response shape from LLM")
    return parse_json(content)


# --- transcript -> chunks

def to_lines(raw, every=12):
    """merge tiny caption snippets into ~12s lines like '[123] text', keeps tokens down"""
    out, cur, start = [], [], None
    for t, txt in raw:
        if start is None:
            start = t
        cur.append(txt)
        if t - start >= every:
            out.append((start, " ".join(cur)))
            cur, start = [], None
    if cur:
        out.append((start, " ".join(cur)))
    return [f"[{int(s)}] {txt}" for s, txt in out]


def chunk(lines, size=None):
    size = size or config.CHUNK_CHARS
    chunks, cur, n = [], [], 0
    for ln in lines:
        if cur and n + len(ln) > size:
            chunks.append(cur)
            cur, n = [], 0
        cur.append(ln)
        n += len(ln) + 1
    if cur:
        chunks.append(cur)
    return chunks


def _span(c):
    a = int(c[0].split("]")[0][1:])
    b = int(c[-1].split("]")[0][1:])
    return a, b


CHUNK_PROMPT = """Video title: "{title}"
This is part {i} of {n} of the transcript, covering {a} to {b}. Every line starts with [seconds].

Write study notes for THIS part only and point out moments where something visual is on screen.

Return JSON exactly like:
{{
  "sections": [
    {{"heading": "short topic title", "start_sec": 0, "bullets": ["point", "point"]}}
  ],
  "visuals": [
    {{"timestamp_sec": 0, "shows": "what is on screen", "why": "why it matters for the notes", "importance": 3}}
  ]
}}

Rules:
- 1 to 4 sections, in the order they happen in the video. start_sec = where the topic starts.
- 3 to 6 bullets per section. Short, factual, no filler like "the speaker says".
- visuals: only moments where the speaker clearly refers to something on screen - a diagram, slide,
  chart, table, code, equation or whiteboard ("as you can see here", "this diagram shows", "on the slide").
  Use a timestamp 2-4 seconds after that cue so the visual is actually showing. Must be between {a_s} and {b_s}.
- "shows": one sentence describing what the viewer is most likely looking at.
- "why": one sentence on why this frame was picked / why it helps understanding.
- importance: 1 (nice to have) to 5 (key diagram). At most {k} visuals. If nothing visual is referenced, use [].

Transcript:
{text}
"""


def summarize_chunk(jid, title, c, i, n):
    a, b = _span(c)
    prompt = CHUNK_PROMPT.format(title=title, i=i + 1, n=n, a=mmss(a), b=mmss(b), a_s=a, b_s=b + 12,
                                 k=max(2, config.MAX_FRAMES // n + 1), text="\n".join(c))
    out = chat(prompt, jid)
    secs = []
    for s in out.get("sections") or []:
        if not isinstance(s, dict) or not s.get("heading"):
            continue
        secs.append({
            "heading": str(s["heading"]).strip()[:150],
            "start_sec": _int(s.get("start_sec"), a),
            "bullets": [str(x).strip() for x in (s.get("bullets") or []) if str(x).strip()][:8],
        })
    vis = []
    for v in out.get("visuals") or []:
        if not isinstance(v, dict):
            continue
        ts = _int(v.get("timestamp_sec"), None)
        shows, why = str(v.get("shows") or "").strip(), str(v.get("why") or "").strip()
        if ts is None or not shows:  # no caption -> can't use it (constraint B)
            continue
        vis.append({"timestamp_sec": ts, "shows": shows[:400], "why": why[:400],
                    "importance": _int(v.get("importance"), 3)})
    if not secs:
        secs = [{"heading": f"Part {i + 1}", "start_sec": a, "bullets": []}]
    return {"sections": secs, "visuals": vis, "span": [a, b]}


def _int(v, default):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def merge(parts, duration):
    """parts are already in chunk order -> sections stay in video order"""
    sections = []
    for p in parts:
        for s in p["sections"]:
            # clamp start into this chunk's span so ordering can't get scrambled
            lo, hi = p["span"]
            s["start_sec"] = min(max(s["start_sec"], lo), hi)
            sections.append(s)

    vis = [v for p in parts for v in p["visuals"] if 0 <= v["timestamp_sec"] < max(1, (duration or 0) - 1)]
    vis.sort(key=lambda v: v["timestamp_sec"])
    # drop near duplicates (same slide flagged twice)
    dedup = []
    for v in vis:
        if dedup and v["timestamp_sec"] - dedup[-1]["timestamp_sec"] < 8:
            if v["importance"] > dedup[-1]["importance"]:
                dedup[-1] = v
            continue
        dedup.append(v)
    if len(dedup) > config.MAX_FRAMES:
        dedup = sorted(dedup, key=lambda v: -v["importance"])[:config.MAX_FRAMES]
        dedup.sort(key=lambda v: v["timestamp_sec"])
    return {"sections": sections, "visuals": dedup}


OVERVIEW_PROMPT = """These are section notes from a video titled "{title}".

{notes}

Return JSON: {{"overview": "2-3 sentence summary of the whole video", "takeaways": ["4 to 6 key takeaways"]}}
"""


def overview(jid, title, sections):
    txt = ""
    for s in sections:
        txt += f"## {s['heading']}\n" + "".join(f"- {b}\n" for b in s["bullets"])
    # reasoning models spend part of max_tokens on thinking, so don't go too low here
    out = chat(OVERVIEW_PROMPT.format(title=title, notes=txt[:7000]), jid, max_tokens=min(1500, config.LLM_MAX_TOKENS))
    return {
        "overview": str(out.get("overview") or "").strip(),
        "takeaways": [str(x).strip() for x in (out.get("takeaways") or []) if str(x).strip()][:8],
    }

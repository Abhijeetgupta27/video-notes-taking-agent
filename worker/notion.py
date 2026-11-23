# notion page assembly. built so that running it twice for the same job never makes two pages.
import hashlib
import logging
import re

import httpx
import redis

from common import config
from common.ratelimit import Limiter

from . import store
from .errors import Fatal, RateLimited, Retryable, chaos

log = logging.getLogger("worker.notion")
limiter = Limiter(redis.Redis.from_url(config.REDIS_URL, decode_responses=True))


def mmss(sec):
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"


def clean_id(s):
    m = re.findall(r"[0-9a-fA-F]{32}", (s or "").replace("-", ""))
    return m[-1].lower() if m else None


class Notion:
    def __init__(self, token):
        self.token = token
        self.key = hashlib.sha1(token.encode()).hexdigest()[:10]  # rate limit is per integration token
        self.c = httpx.Client(base_url=config.NOTION_API, timeout=60, headers={
            "Authorization": f"Bearer {token}", "Notion-Version": config.NOTION_VERSION})

    def call(self, method, path, **kw):
        limiter.take(f"notion:{self.key}", 1.1, max_n=config.NOTION_RPS)
        chaos("notion")
        try:
            res = self.c.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise Retryable("NOTION_UNREACHABLE", f"Couldn't reach Notion ({e.__class__.__name__})")
        if res.status_code == 429:
            ra = res.headers.get("Retry-After")
            raise RateLimited("notion", "Notion returned 429", retry_after=float(ra) if ra else 0)
        if res.status_code in (409, 502, 503, 504) or res.status_code >= 500:
            raise Retryable("NOTION_UNAVAILABLE", f"Notion error {res.status_code}")
        data = res.json() if res.content else {}
        if res.status_code == 401:
            raise Fatal("NOTION_UNAUTHORIZED", "Notion token is invalid or was revoked - reconnect Notion in the extension")
        if res.status_code in (403, 404):
            raise Fatal("NOTION_NO_ACCESS", data.get("message") or "Integration can't access that Notion page - share it with the integration")
        if res.status_code >= 400:
            raise Fatal("NOTION_BAD_REQUEST", data.get("message") or f"Notion said {res.status_code}")
        return data

    def children_count(self, block_id):
        n, cursor = 0, None
        while True:
            q = {"page_size": 100}
            if cursor:
                q["start_cursor"] = cursor
            d = self.call("GET", f"/blocks/{block_id}/children", params=q)
            n += len(d.get("results", []))
            if not d.get("has_more"):
                return n
            cursor = d.get("next_cursor")

    def upload_jpeg(self, name, data):
        up = self.call("POST", "/file_uploads", json={"filename": name, "content_type": "image/jpeg"})
        self.call("POST", f"/file_uploads/{up['id']}/send", files={"file": (name, data, "image/jpeg")})
        return up["id"]


# ---------- picking where the page goes ----------

def resolve_parent(n, wanted):
    """returns a notion `parent` object"""
    pid = clean_id(wanted) or clean_id(config.NOTION_DEFAULT_PARENT_ID)
    if pid:
        # could be a page or a database
        try:
            n.call("GET", f"/pages/{pid}")
            return {"page_id": pid}
        except Fatal:
            pass
        try:
            db = n.call("GET", f"/databases/{pid}")
            title_prop = next(k for k, v in db.get("properties", {}).items() if v.get("type") == "title")
            return {"database_id": pid, "_title_prop": title_prop}
        except (Fatal, StopIteration):
            raise Fatal("NOTION_NO_ACCESS", "Can't open the Notion parent page/database - is it shared with the integration?")

    # default destination: a page called "Video Notes" (NOTION_DEFAULT_PAGE_TITLE) that was shared
    # with the integration during OAuth; if there isn't one, the first page the integration can see.
    d = n.call("POST", "/search", json={"query": config.NOTION_DEFAULT_PAGE_TITLE,
                                        "filter": {"property": "object", "value": "page"}, "page_size": 20})
    pages = d.get("results", [])
    for p in pages:
        if _title(p).strip().lower() == config.NOTION_DEFAULT_PAGE_TITLE.lower():
            return {"page_id": p["id"].replace("-", "")}
    if not pages:
        d = n.call("POST", "/search", json={"filter": {"property": "object", "value": "page"}, "page_size": 20})
        pages = [p for p in d.get("results", []) if p.get("parent", {}).get("type") in ("workspace", "page_id")]
    if pages:
        return {"page_id": pages[0]["id"].replace("-", "")}
    raise Fatal("NO_NOTION_DESTINATION",
                "The integration can't see any Notion page. Share a page (e.g. 'Video Notes') with it and try again.")


def _title(page):
    for v in page.get("properties", {}).values():
        if v.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in v.get("title", []))
    return ""


# ---------- blocks ----------

def rt(text, link=None, bold=False, italic=False, code=False, color=None):
    o = {"type": "text", "text": {"content": str(text)[:1900]}}
    if link:
        o["text"]["link"] = {"url": link}
    ann = {k: True for k, v in (("bold", bold), ("italic", italic), ("code", code)) if v}
    if color:
        ann["color"] = color
    if ann:
        o["annotations"] = ann
    return o


def blk(kind, rich, **extra):
    return {"object": "block", "type": kind, kind: {"rich_text": rich, **extra}}


def build_blocks(jid, job, notes, frames):
    """frames = list of (visual, upload_id). first block carries the job id - used to find the page after a crash"""
    vid = job["video_id"]
    url = f"https://www.youtube.com/watch?v={vid}"
    at = lambda s: f"{url}&t={int(s)}s"
    dur = int(float(job.get("duration_sec") or 0))

    out = [blk("callout", [rt("Notes from "), rt(job.get("video_title") or vid, link=url, bold=True),
                           rt(f"  ·  {mmss(dur)} long  ·  job {jid[:8]}", color="gray")],
               icon={"type": "emoji", "emoji": "🎬"}, color="gray_background")]

    if notes.get("overview"):
        out.append(blk("heading_2", [rt("Overview")]))
        out.append(blk("paragraph", [rt(notes["overview"])]))
    if notes.get("takeaways"):
        out.append(blk("heading_2", [rt("Key takeaways")]))
        out += [blk("bulleted_list_item", [rt(t)]) for t in notes["takeaways"]]

    out.append({"object": "block", "type": "divider", "divider": {}})

    secs = notes["sections"]
    for i, s in enumerate(secs):
        end = secs[i + 1]["start_sec"] if i + 1 < len(secs) else 10 ** 9
        out.append(blk("heading_2", [rt(f"{mmss(s['start_sec'])}  ", link=at(s["start_sec"]), color="gray"),
                                     rt(s["heading"])]))
        out += [blk("bulleted_list_item", [rt(b)]) for b in s["bullets"]]
        # frames that fall inside this section go right under its bullets
        for v, uid in frames:
            ts = v["timestamp_sec"]
            if s["start_sec"] <= ts < end or (i == 0 and ts < s["start_sec"]):
                cap = [rt(f"[{mmss(ts)}] ", link=at(ts), bold=True), rt(v["shows"])]
                if v.get("why"):
                    cap.append(rt(f"  ·  Why: {v['why']}", italic=True))
                out.append({"object": "block", "type": "image",
                            "image": {"type": "file_upload", "file_upload": {"id": uid}, "caption": cap}})

    out.append({"object": "block", "type": "divider", "divider": {}})
    out.append(blk("paragraph", [rt("Images are frames captured straight from the video at the linked timestamps.",
                                    italic=True, color="gray")]))
    return out


# ---------- publish ----------

def _find_existing(n, title, jid):
    # only used if we crashed right after creating the page but before saving its id
    d = n.call("POST", "/search", json={"query": title, "filter": {"property": "object", "value": "page"},
                                        "page_size": 20})
    for p in d.get("results", []):
        kids = n.call("GET", f"/blocks/{p['id']}/children", params={"page_size": 1}).get("results", [])
        if kids and kids[0].get("type") == "callout":
            txt = "".join(t.get("plain_text") or t.get("text", {}).get("content", "")
                          for t in kids[0]["callout"].get("rich_text", []))
            if f"job {jid[:8]}" in txt:
                return p["id"], p.get("url")
    return None, None


def publish(jid, job, notes, ok_frames, token, parent_hint, progress):
    """ok_frames = [(visual, jpeg_bytes)]. returns (page_url, frames_published)"""
    n = Notion(token)
    k = f"job:{jid}:notion"
    r = store.r

    # 1) upload images (each upload id is remembered, so a retry doesn't upload twice)
    frames = []
    for i, (v, img) in enumerate(ok_frames):
        ts = v["timestamp_sec"]
        uid = r.hget(f"{k}:uploads", ts)
        if not uid:
            progress(f"Uploading frame {i + 1} of {len(ok_frames)}")
            try:
                uid = n.upload_jpeg(f"{job['video_id']}_{ts}s.jpg", img)
            except Fatal as e:
                if e.code in ("NOTION_UNAUTHORIZED", "NOTION_NO_ACCESS"):
                    raise  # token problem, not a problem with this one image
                # remember the failure too, the block list has to come out the same on a retry
                uid = "fail"
                store.add_warning(jid, {"code": "FRAME_UPLOAD_FAILED", "timestamp_sec": ts, "message": e.msg})
            r.hset(f"{k}:uploads", ts, uid)
            r.expire(f"{k}:uploads", 3000)  # notion drops unattached uploads after ~1h
        if uid != "fail":
            frames.append((v, uid))

    title = f"Notes: {job.get('video_title') or job['video_id']}"[:190]
    blocks = build_blocks(jid, job, notes, frames)

    # 2) create the page once
    page_id, page_url = r.get(f"{k}:page"), r.get(f"{k}:url")
    if not page_id and r.get(f"{k}:creating"):
        page_id, page_url = _find_existing(n, title, jid)
        if page_id:
            log.info("job %s: found page from a previous attempt", jid)
            r.set(f"{k}:page", page_id, ex=config.ARTIFACT_TTL)
            r.set(f"{k}:url", page_url or "", ex=config.ARTIFACT_TTL)

    if not page_id:
        parent = store.load(jid, "parent") or resolve_parent(n, parent_hint)
        store.put(jid, "parent", parent)
        title_prop = parent.pop("_title_prop", None)
        props = {(title_prop or "title"): {"title": [{"text": {"content": title}}]}}
        r.set(f"{k}:creating", 1, ex=config.ARTIFACT_TTL)
        progress("Creating the Notion page")
        page = n.call("POST", "/pages", json={"parent": parent, "icon": {"type": "emoji", "emoji": "📝"},
                                              "properties": props, "children": blocks[:1]})
        page_id, page_url = page["id"], page.get("url")
        r.set(f"{k}:page", page_id, ex=config.ARTIFACT_TTL)
        r.set(f"{k}:url", page_url or "", ex=config.ARTIFACT_TTL)

    # 3) append whatever isn't there yet. notion itself is the source of truth for how far we got,
    #    so re-running after a crash continues instead of duplicating blocks.
    have = n.children_count(page_id)
    for i in range(have, len(blocks), 90):
        progress(f"Writing page content ({min(i + 90, len(blocks))}/{len(blocks)} blocks)")
        n.call("PATCH", f"/blocks/{page_id}/children", json={"children": blocks[i:i + 90]})

    if not page_url:
        page_url = n.call("GET", f"/pages/{page_id}").get("url")
        r.set(f"{k}:url", page_url, ex=config.ARTIFACT_TTL)
    return page_url, len(frames)

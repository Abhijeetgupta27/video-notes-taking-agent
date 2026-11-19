# Video Notes Agent

Cloud Computing - Project 2. Distributed, queue-based system that turns a YouTube video into study notes (summary + real frames of the important slides/diagrams) and publishes them to Notion. Everything is driven from a Chrome extension.

```
 Chrome extension                Gateway (FastAPI)            RabbitMQ                    Workers (N)
 ┌──────────────┐  POST /jobs    ┌──────────────┐  publish   ┌───────────────┐  consume  ┌─────────────────┐
 │ content.js   │ ─────────────> │ validate     │ ─────────> │ notes.jobs    │ ────────> │ transcribe      │
 │ background.js│  GET /jobs/:id │ create job   │            │ (quorum)      │ <──────── │ summarize (LLM) │
 │ popup        │ <───────────── │ report status│            │ notes.retry.* │  next     │ frames (ffmpeg) │
 └──────────────┘                └──────┬───────┘            │ notes.dlq     │  stage    │ publish (Notion)│
                                        │                    └───────────────┘           └────────┬────────┘
                                        └──────────────── Redis (job state, artifacts, ──────────┘
                                                          rate limiter, locks, heartbeats)
```

Four separate containers: **gateway**, **rabbitmq**, **worker** (2 replicas by default, scalable) + **redis** as the state store.

---

## Quick start

Needs Docker (Compose v2.24+) and Chrome.

### 1. Notion integration

1. Go to https://www.notion.so/profile/integrations → **New integration** → type **Public**.
2. Redirect URI: `https://midginncpkolhmplcmgljjlfgneoonpm.chromiumapp.org/`
   (the extension ID is fixed through the `key` in `manifest.json`, so this URI is always the same)
3. Capabilities: read content, update content, insert content.
4. Copy the OAuth client ID and secret into `.env` (next step).
5. In Notion create a page called **Video Notes** - when you connect the extension, tick this page on Notion's "select pages" screen.

No time for OAuth? Make an *internal* integration instead, share the "Video Notes" page with it, and paste its token in the extension (⚙ → "Use an internal integration token").

### 2. Configure and start

```bash
cp .env.example .env        # fill in LLM_API_KEY, NOTION_CLIENT_ID, NOTION_CLIENT_SECRET
docker compose up --build -d
curl localhost:8000/healthz
```

### 3. Load the extension

`chrome://extensions` → Developer mode → **Load unpacked** → select the `extension/` folder. No build step.

Click the extension icon → **Connect Notion** (one time, the token is stored in `chrome.storage`). Then open any YouTube video and press **Take Notes** in the player bar. Progress shows up in the popup.

---

## Base URL & API

Base URL: **`http://localhost:8000`** (can be changed in the popup settings if the gateway runs elsewhere).

| Method | Path | What |
|---|---|---|
| POST | `/jobs` | submit `{youtube_url, notion_token, notion_parent_id?}` → `202` + `Location: /jobs/{id}` |
| GET | `/jobs/{id}` | status, progress, title, duration, warnings, error, result. read only. `404` if unknown |
| GET | `/jobs/{id}/results` | final outcome. `409` while the job is still running |
| GET | `/healthz` | checks gateway + redis + rabbitmq, shows live workers and queue depth. `503` if something is down |
| GET | `/dlq` | dead-lettered jobs, for manual inspection |
| GET | `/jobs` | last 20 jobs (debugging) |
| GET/POST | `/oauth/notion/config`, `/oauth/notion/token` | used by the extension for the OAuth code exchange (the secret stays on the server, the token is *not* stored) |

Errors are always `{"error": {"code": "...", "message": "..."}}`.

RabbitMQ management UI: http://localhost:15672 (guest / guest) - the `notes.dlq` queue can be inspected there too.

## Default Notion destination

If `notion_parent_id` is not sent (and nothing is set in the popup settings), the worker picks the destination in this order:

1. the page Notion duplicated from the integration's template during OAuth (only if you set a template URL on the integration)
2. `NOTION_DEFAULT_PARENT_ID` from `.env`, if set
3. a page titled **"Video Notes"** (`NOTION_DEFAULT_PAGE_TITLE`) that was shared with the integration ← the normal case
4. otherwise the first page the integration has access to

If the integration can't see any page the job fails with `NO_NOTION_DESTINATION` and the popup says what to do. Each video becomes a new child page `Notes: <video title>`.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `LLM_API_KEY` | – | **required.** key for the LLM provider |
| `LLM_MODEL` | `openai/gpt-oss-120b` | model name |
| `LLM_BASE_URL` | `https://api.groq.com/openai/v1` | any OpenAI-compatible endpoint (Groq, OpenAI, Gemini's OpenAI endpoint...) |
| `LLM_RPM` | `25` | **global** LLM requests/minute across all workers |
| `LLM_TPM` | `10000` | **global** LLM tokens/minute across all workers (0 = off) |
| `LLM_MAX_TOKENS` | `4000` | max output tokens per call (reasoning models use part of this for thinking) |
| `LLM_REASONING_EFFORT` | – | `low`/`medium`/`high` for reasoning models like gpt-oss, leave empty for others |
| `CHUNK_CHARS` | `9000` | transcript chunk size per LLM call |
| `LLM_JSON_MODE` | `1` | send `response_format: json_object` (set 0 if the provider doesn't support it) |
| `NOTION_CLIENT_ID` / `NOTION_CLIENT_SECRET` | – | **required for OAuth.** from the public integration |
| `NOTION_DEFAULT_PARENT_ID` | – | optional fixed destination page/database |
| `NOTION_DEFAULT_PAGE_TITLE` | `Video Notes` | title of the default page to look for |
| `NOTION_RPS` | `3` | Notion requests/second per token (Notion's documented average) |
| `RABBITMQ_URL` | `amqp://guest:guest@rabbitmq:5672/` | broker connection string (set in compose) |
| `REDIS_URL` | `redis://redis:6379/0` | state store (set in compose) |
| `MAX_ATTEMPTS` | `5` | attempts per stage before the job goes to the DLQ |
| `RETRY_DELAYS` | `10,30,90` | backoff in seconds, last value repeats → 10, 30, 90, 90 |
| `CRASH_LIMIT` | `3` | how many times a message may crash a worker before it is dead-lettered |
| `MAX_VIDEO_SEC` | `1200` | constraint A (20 min) |
| `MAX_FRAMES` | `8` | max frames per page |
| `YT_RPM` | `30` | calls to YouTube per minute, global |
| `YT_PROXY` / `YT_COOKIES` | – | optional, only if YouTube starts blocking requests |
| `TOKEN_SECRET` | `change-me` | encrypts the Notion token while it sits inside queue messages |
| `CHAOS_429` | – | demo only, e.g. `llm:0.3,notion:0.2,youtube:0.1` = fake 429s with that probability |
| `FRAME_DELAY` | `0` | demo only, seconds to sleep before each frame (makes the kill demo easy to time) |

Set `LLM_RPM`/`LLM_TPM` a little **below** what your provider's dashboard says your limits are. Free tiers usually also have a daily token cap - that one I can't throttle around, so check it before a long demo.

---

## Job lifecycle

`QUEUED → TRANSCRIBING → SUMMARIZING → EXTRACTING_FRAMES → PUBLISHING → COMPLETED | COMPLETED_WITH_WARNINGS`
plus `FAILED` (non-retryable error) and `DEAD_LETTERED` (retry budget used up) from any non-terminal state.

Transitions are checked in `common/states.py` and applied atomically in Redis (`WATCH/MULTI`). A move backwards is ignored, and moving into the state you're already in is a no-op - that's what makes redelivery harmless.

While a stage is waiting for a retry the status does **not** change; the job gets a `waiting` object (`attempt`, `max_attempts`, `retry_at`) and the popup shows "Rate limited - retrying in 25s (attempt 2 of 5)" instead of an error.

## Failure handling (and how to demo it)

Helpers in `scripts/`: `submit.sh`, `watch.sh <job>`, `kill-worker.sh <job>`, `burst.sh`.

**429 from any API** - LLM, Notion and YouTube errors are classified as `RateLimited` and the stage message goes to a TTL "retry" queue (`notes.retry.10s`, `.30s`, `.90s`), which dead-letters it back into the main queue when the time is up. If the API sent `Retry-After`, the delay is never shorter than that.
Demo: put `CHAOS_429=llm:0.5` in `.env`, `docker compose up -d`, submit a video and `watch.sh` it.

**Worker crash** - messages are acked only *after* a stage is finished and the next stage message is confirmed by RabbitMQ. If a worker is SIGKILLed, RabbitMQ sees the connection drop and redelivers the unacked message to another worker straight away.
Demo: `FRAME_DELAY=4` in `.env`, submit, wait for "Extracting Frames", then `./scripts/kill-worker.sh <job_id>`. Another worker continues from the next uncaptured frame within ~HB_TTL seconds (6s default).

Why there are no duplicates:
- every stage output is checkpointed in Redis (transcript, each LLM chunk, each frame, each Notion upload id), a rerun skips what's already done
- a per-job lock in Redis makes sure two live workers never run the same job; a dead worker's lock is taken over as soon as its heartbeat key expires
- Notion: the page id is saved right after creation; blocks are appended starting from the number of children Notion already has, so a retry continues instead of appending twice. If a worker dies between creating the page and saving the id, the retry searches Notion for the page whose first block contains the job id and reuses it.

**Graceful degradation** - a frame that can't be captured is skipped with a `FRAME_EXTRACTION_FAILED` warning; if the stream can't be opened at all, the page goes out text-only. Both end as `COMPLETED_WITH_WARNINGS`.

**Dead-letter queue** - after `MAX_ATTEMPTS` failed attempts in a stage (or `CRASH_LIMIT` crashes) the message is published to `notes.dlq` and the job becomes `DEAD_LETTERED`. Submitting a private / deleted video is the easiest way to trigger it. With the defaults that takes about 10+30+90+90 s ≈ 4 min; for a quicker demo use `RETRY_DELAYS=3,6,12` and `MAX_ATTEMPTS=3`.

**Burst / scaling** - `docker compose up -d --scale worker=4`. The LLM limit is global (Redis), so more workers never means more LLM requests per minute - they just queue for a slot, and the popup shows "Waiting ~20s for an LLM slot".

## Hard constraints

**A - length.** The transcript stage gets the real duration from YouTube metadata *before* downloading anything else. Anything over `MAX_VIDEO_SEC` (1200s) fails right away with `VIDEO_TOO_LONG` (non-retryable, so it is not retried and never reaches the LLM).

**B - real frames only.** Frames are cut by `ffmpeg` straight from the video stream at the flagged second (`-ss <t> -i <stream> -frames:v 1`). If YouTube refuses that direct read (it often answers 403 now), the worker lets yt-dlp download a small ≤480p copy once and cuts the frames from that file instead. Nothing is generated or redrawn. A failed timestamp is simply left out. The LLM has to return a `shows` + `why` text for every timestamp it flags (visuals without it are dropped), and that becomes the image caption in Notion, together with a link to that second of the video.

## Project layout

```
common/     shared: config, states, broker topology, rate limiter, token encryption
gateway/    FastAPI app
worker/     main.py (consumer, retries, DLQ, locks), pipeline.py (the 4 stages),
            youtube.py, llm.py, notion.py, store.py (redis state)
extension/  manifest v3 extension (content script, service worker, popup)
scripts/    demo helpers
```

Design choices are explained in [DESIGN.md](DESIGN.md).

## Known limitations

- The LLM only sees the transcript, so a frame caption describes what is *probably* on screen based on what the speaker says at that moment.
- Videos without any captions fail with `NO_TRANSCRIPT` (no speech-to-text fallback).
- YouTube sometimes blocks automated requests (more likely from cloud IPs). Running locally is usually fine; otherwise set `YT_COOKIES` or `YT_PROXY`. Keeping yt-dlp up to date helps (`docker compose build --no-cache worker`).
- YouTube changes its player markup now and then. The button goes into `.ytp-right-controls`; if it ever stops appearing, that selector in `content.js` is the place to look.

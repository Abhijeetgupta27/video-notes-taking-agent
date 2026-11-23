# API gateway - only accepts jobs and reports status. no pipeline work happens here.
import asyncio
import base64
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager

import aio_pika
import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from common import broker, config, states
from common.jobview import job_view
from common.secret import seal

log = logging.getLogger("gateway")
logging.basicConfig(level=logging.INFO, format="%(asctime)s gateway %(levelname)s %(message)s")

VID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
HEX32 = re.compile(r"[0-9a-fA-F]{32}")

st = {}  # redis, rabbit connection, channel


def err(http, code, msg, **extra):
    return JSONResponse({"error": {"code": code, "message": msg, **extra}}, status_code=http)


def parse_video_id(url):
    """accepts the usual youtube url shapes, returns the 11 char id or None"""
    if not isinstance(url, str):
        return None
    url = url.strip()
    if VID_RE.match(url):
        return None  # want a url, not a bare id
    m = re.match(r"^(?:https?://)?(?:www\.|m\.|music\.)?(youtube\.com|youtu\.be)(/[^?#]*)?(\?[^#]*)?", url)
    if not m:
        return None
    host, path, qs = m.group(1), m.group(2) or "", m.group(3) or ""
    vid = None
    if host == "youtu.be":
        vid = path.strip("/").split("/")[0]
    elif path == "/watch":
        q = dict(p.split("=", 1) for p in qs.lstrip("?").split("&") if "=" in p)
        vid = q.get("v")
    else:
        m2 = re.match(r"^/(shorts|embed|live|v)/([^/]+)", path)
        if m2:
            vid = m2.group(2)
    return vid if vid and VID_RE.match(vid) else None


async def connect_rabbit():
    # rabbit can take a while on first boot, keep trying
    for i in range(30):
        try:
            conn = await aio_pika.connect_robust(config.RABBITMQ_URL)
            ch = await conn.channel(publisher_confirms=True)
            await broker.declare(ch)
            return conn, ch
        except Exception as e:
            log.warning("rabbit not ready (%s), retry %d", e, i + 1)
            await asyncio.sleep(2)
    raise RuntimeError("could not connect to rabbitmq")


@asynccontextmanager
async def lifespan(app):
    st["r"] = aioredis.from_url(config.REDIS_URL, decode_responses=True)
    st["conn"], st["ch"] = await connect_rabbit()
    log.info("gateway up")
    yield
    await st["conn"].close()
    await st["r"].aclose()


app = FastAPI(title="Video Notes Gateway", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
                   expose_headers=["Location"])


@app.exception_handler(RequestValidationError)
async def bad_body(req, exc):
    return err(400, "INVALID_REQUEST", "Request body is not valid JSON or has the wrong shape")


@app.exception_handler(404)
async def not_found(req, exc):
    return err(404, "NOT_FOUND", "No such endpoint")


@app.exception_handler(405)
async def bad_method(req, exc):
    return err(405, "METHOD_NOT_ALLOWED", "Method not allowed here")


# ---------- jobs ----------

@app.post("/jobs")
async def create_job(req: Request):
    try:
        body = await req.json()
    except Exception:
        return err(400, "INVALID_JSON", "Body must be JSON")
    if not isinstance(body, dict):
        return err(400, "INVALID_JSON", "Body must be a JSON object")

    vid = parse_video_id(body.get("youtube_url"))
    if not vid:
        return err(400, "INVALID_YOUTUBE_URL", "youtube_url must be a valid YouTube video link")

    token = body.get("notion_token")
    if not isinstance(token, str) or len(token.strip()) < 10:
        return err(400, "MISSING_NOTION_TOKEN", "notion_token is required (connect Notion in the extension first)")

    parent = body.get("notion_parent_id") or ""
    if parent:
        m = HEX32.findall(str(parent).replace("-", ""))
        if not m:
            return err(400, "INVALID_PARENT_ID", "notion_parent_id doesn't look like a Notion page/database id")
        parent = m[-1].lower()

    jid = str(uuid.uuid4())
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    r = st["r"]
    await r.hset(f"job:{jid}", mapping={
        "job_id": jid,
        "video_id": vid,
        "youtube_url": f"https://www.youtube.com/watch?v={vid}",
        "status": states.QUEUED,
        "progress": 0,
        "warnings": "[]",
        "created_at": now,
        "updated_at": now,
        "parent_id": parent,
        "detail": "Waiting for a free worker",
    })
    await r.zadd("jobs:recent", {jid: time.time()})

    msg = {"job_id": jid, "stage": "transcribe", "token": seal(token.strip()), "parent_id": parent}
    try:
        await broker.publish(st["ch"], config.JOBS_Q, msg, headers={"x-attempt": 0})
    except Exception as e:
        log.error("publish failed: %s", e)
        await r.delete(f"job:{jid}")
        await r.zrem("jobs:recent", jid)
        return err(503, "BROKER_UNAVAILABLE", "Queue is not reachable right now, try again in a moment")

    log.info("job %s queued for video %s", jid, vid)
    return JSONResponse({"job_id": jid, "status": states.QUEUED, "video_id": vid},
                        status_code=202, headers={"Location": f"/jobs/{jid}"})


async def queue_depth(name):
    # fresh channel each time - the robust channel caches passive declares (stale counts)
    async with st["conn"].channel() as tmp:
        q = await tmp.declare_queue(name, passive=True)
        return q.declaration_result.message_count


async def load(jid):
    try:
        uuid.UUID(jid)
    except ValueError:
        return None
    h = await st["r"].hgetall(f"job:{jid}")
    return h or None


@app.get("/jobs/{jid}")
async def get_job(jid: str):
    h = await load(jid)
    if not h:
        return err(404, "JOB_NOT_FOUND", f"No job with id {jid}")
    return job_view(h)


@app.get("/jobs/{jid}/results")
async def get_results(jid: str):
    h = await load(jid)
    if not h:
        return err(404, "JOB_NOT_FOUND", f"No job with id {jid}")
    v = job_view(h)
    if v["status"] not in states.TERMINAL:
        return err(409, "JOB_NOT_FINISHED", "Job is still running, poll GET /jobs/{id} until it finishes",
                   status=v["status"])
    return {"status": v["status"], "result": v["result"], "warnings": v["warnings"], "error": v["error"]}


@app.get("/jobs")
async def list_jobs(limit: int = 20):
    ids = await st["r"].zrevrange("jobs:recent", 0, max(1, min(limit, 100)) - 1)
    out = []
    for jid in ids:
        h = await st["r"].hgetall(f"job:{jid}")
        if h:
            v = job_view(h)
            out.append({k: v[k] for k in ("job_id", "status", "progress", "video_id", "video_title", "worker")})
    return {"jobs": out}


@app.get("/dlq")
async def dead_letters():
    # quick view for manual inspection (the raw messages are also visible in the rabbit UI, :15672)
    ids = await st["r"].smembers("dlq:jobs")
    out = []
    for jid in ids:
        h = await st["r"].hgetall(f"job:{jid}")
        if h:
            v = job_view(h)
            out.append({"job_id": jid, "video_id": v["video_id"], "error": v["error"], "updated_at": v["updated_at"]})
    return {"messages_in_dlq": await queue_depth(config.DLQ), "jobs": out}


# ---------- health ----------

@app.get("/healthz")
async def healthz():
    out = {"gateway": "ok"}
    ok = True
    try:
        await st["r"].ping()
        out["redis"] = "ok"
        out["workers_alive"] = len([k async for k in st["r"].scan_iter("worker:*")])
    except Exception as e:
        ok = False
        out["redis"] = f"down: {e}"
    try:
        out["queue_depth"] = await queue_depth(config.JOBS_Q)
        out["broker"] = "ok"
    except Exception as e:
        ok = False
        out["broker"] = f"down: {e}"
    out["status"] = "ok" if ok else "degraded"
    return JSONResponse(out, status_code=200 if ok else 503)


# ---------- notion oauth ----------
# the extension does the browser part (chrome.identity), but the code -> token exchange needs
# the client secret, so it goes through here. the token is handed back and NOT stored.

@app.get("/oauth/notion/config")
async def oauth_config():
    if not config.NOTION_CLIENT_ID:
        return err(501, "OAUTH_NOT_CONFIGURED", "NOTION_CLIENT_ID is not set on the gateway")
    return {"client_id": config.NOTION_CLIENT_ID,
            "authorize_url": "https://api.notion.com/v1/oauth/authorize"}


@app.post("/oauth/notion/token")
async def oauth_token(req: Request):
    if not (config.NOTION_CLIENT_ID and config.NOTION_CLIENT_SECRET):
        return err(501, "OAUTH_NOT_CONFIGURED", "Notion OAuth client id/secret not set on the gateway")
    try:
        body = await req.json()
        code, redirect = body["code"], body["redirect_uri"]
    except Exception:
        return err(400, "INVALID_REQUEST", "need code and redirect_uri")

    basic = base64.b64encode(f"{config.NOTION_CLIENT_ID}:{config.NOTION_CLIENT_SECRET}".encode()).decode()
    async with httpx.AsyncClient(timeout=20) as c:
        res = await c.post(f"{config.NOTION_API}/oauth/token",
                           headers={"Authorization": f"Basic {basic}", "Notion-Version": config.NOTION_VERSION},
                           json={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect})
    data = res.json() if res.content else {}
    if res.status_code != 200:
        return err(400, "OAUTH_EXCHANGE_FAILED", data.get("error_description") or data.get("message") or "Notion rejected the code")
    return {k: data.get(k) for k in ("access_token", "workspace_name", "workspace_icon", "workspace_id",
                                     "bot_id", "duplicated_template_id")}

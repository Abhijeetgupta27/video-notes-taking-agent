# all settings come from env vars (see .env.example / README)
import os
import socket


def _int(name, default):
    return int(os.getenv(name, str(default)))


def _float(name, default):
    return float(os.getenv(name, str(default)))


def _ints(name, default):
    raw = os.getenv(name, default)
    return [int(x) for x in raw.split(",") if x.strip()]


RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# queue names
JOBS_Q = "notes.jobs"
DLQ = "notes.dlq"

# retry / backoff
MAX_ATTEMPTS = _int("MAX_ATTEMPTS", 5)             # per stage, then -> DLQ
RETRY_DELAYS = _ints("RETRY_DELAYS", "10,30,90")   # seconds, last value repeats
CRASH_LIMIT = _int("CRASH_LIMIT", 3)               # redeliveries after worker crashes before DLQ
LOCK_WAIT = _int("LOCK_WAIT", 5)                   # delay when another live worker holds the job

# LLM (any OpenAI compatible endpoint - I used Groq)
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")
LLM_RPM = _int("LLM_RPM", 25)          # requests / minute, shared by ALL workers
LLM_TPM = _int("LLM_TPM", 10000)       # tokens / minute, shared by ALL workers (0 = off)
LLM_MAX_TOKENS = _int("LLM_MAX_TOKENS", 4000)
LLM_JSON_MODE = os.getenv("LLM_JSON_MODE", "1") == "1"
# only for reasoning models like openai/gpt-oss-120b (low / medium / high). leave empty otherwise
LLM_REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "")
CHUNK_CHARS = _int("CHUNK_CHARS", 9000)

# notion
NOTION_API = os.getenv("NOTION_API_URL", "https://api.notion.com/v1").rstrip("/")
NOTION_VERSION = os.getenv("NOTION_VERSION", "2022-06-28")
NOTION_CLIENT_ID = os.getenv("NOTION_CLIENT_ID", "")
NOTION_CLIENT_SECRET = os.getenv("NOTION_CLIENT_SECRET", "")
NOTION_DEFAULT_PARENT_ID = os.getenv("NOTION_DEFAULT_PARENT_ID", "")
NOTION_DEFAULT_PAGE_TITLE = os.getenv("NOTION_DEFAULT_PAGE_TITLE", "Video Notes")
NOTION_RPS = _int("NOTION_RPS", 3)

# youtube
YT_RPM = _int("YT_RPM", 30)
YT_PROXY = os.getenv("YT_PROXY", "")
YT_COOKIES = os.getenv("YT_COOKIES", "")   # optional cookies.txt path, helps if youtube asks "are you a bot"

# hard constraints / pipeline knobs
MAX_VIDEO_SEC = _int("MAX_VIDEO_SEC", 1200)   # 20 min
MAX_FRAMES = _int("MAX_FRAMES", 8)
FRAME_TIMEOUT = _int("FRAME_TIMEOUT", 60)

# used to encrypt the notion token while it sits inside queue messages
TOKEN_SECRET = os.getenv("TOKEN_SECRET", "change-me")

# worker liveness
WORKER_ID = os.getenv("WORKER_ID") or f"{socket.gethostname()}:{os.getpid()}"
HB_TTL = _int("HB_TTL", 6)
LOCK_TTL = _int("LOCK_TTL", 60)

# artifacts (transcript, frames...) expire after this
ARTIFACT_TTL = _int("ARTIFACT_TTL", 86400)

# demo knobs - fake failures so the grader can see the retry paths
# e.g. CHAOS_429="llm:0.3,notion:0.2,youtube:0.1"
CHAOS_429 = {}
for part in os.getenv("CHAOS_429", "").split(","):
    if ":" in part:
        k, v = part.split(":", 1)
        CHAOS_429[k.strip()] = float(v)
FRAME_DELAY = _float("FRAME_DELAY", 0)   # sleep before each frame, makes "kill during frames" easy

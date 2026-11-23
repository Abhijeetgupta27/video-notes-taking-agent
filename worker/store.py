# job state + stage artifacts in redis
import json
import time

import redis

from common import config, states

r = redis.Redis.from_url(config.REDIS_URL, decode_responses=True)
rb = redis.Redis.from_url(config.REDIS_URL)  # raw bytes, for frame images

TTL = config.ARTIFACT_TTL


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _enc(v):
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    return v


def get(jid):
    return r.hgetall(f"job:{jid}")


def update(jid, **fields):
    if not jid:
        return
    fields["updated_at"] = now()
    r.hset(f"job:{jid}", mapping={k: _enc(v) for k, v in fields.items()})


def move(jid, status, **fields):
    """state machine transition. same state again = fine (redelivery). illegal move = ignored."""
    key = f"job:{jid}"
    with r.pipeline() as p:
        while True:
            try:
                p.watch(key)
                cur = p.hget(key, "status")
                if cur != status and not states.can_move(cur, status):
                    p.unwatch()
                    return False
                p.multi()
                fields.update(status=status, updated_at=now())
                p.hset(key, mapping={k: _enc(v) for k, v in fields.items()})
                p.execute()
                return True
            except redis.WatchError:
                continue


def add_warning(jid, w):
    key = f"job:{jid}"
    with r.pipeline() as p:
        while True:
            try:
                p.watch(key)
                ws = json.loads(p.hget(key, "warnings") or "[]")
                same = [x for x in ws if x.get("code") == w.get("code") and x.get("timestamp_sec") == w.get("timestamp_sec")]
                p.multi()
                if not same:
                    ws.append(w)
                    p.hset(key, "warnings", json.dumps(ws))
                p.execute()
                return
            except redis.WatchError:
                continue


def warnings(jid):
    return json.loads(r.hget(f"job:{jid}", "warnings") or "[]")


# --- artifacts (one key per thing so a redelivered stage can pick up where it left off)

def put(jid, name, obj):
    r.set(f"job:{jid}:a:{name}", json.dumps(obj), ex=TTL)


def load(jid, name):
    v = r.get(f"job:{jid}:a:{name}")
    return json.loads(v) if v else None


def stage_done(jid, stage):
    return r.sismember(f"job:{jid}:done", stage)


def mark_done(jid, stage):
    r.sadd(f"job:{jid}:done", stage)
    r.expire(f"job:{jid}:done", TTL)


def forwarded(jid, stage):
    return r.sismember(f"job:{jid}:fwd", stage)


def mark_forwarded(jid, stage):
    r.sadd(f"job:{jid}:fwd", stage)
    r.expire(f"job:{jid}:fwd", TTL)


# frames: hash ts -> "ok" / "fail:<reason>", images under their own keys
def frame_states(jid):
    return r.hgetall(f"job:{jid}:frames")


def frame_ok(jid, ts, img):
    rb.set(f"job:{jid}:img:{ts}", img, ex=TTL)
    r.hset(f"job:{jid}:frames", ts, "ok")
    r.expire(f"job:{jid}:frames", TTL)


def frame_fail(jid, ts, why):
    r.hset(f"job:{jid}:frames", ts, f"fail:{why}")
    r.expire(f"job:{jid}:frames", TTL)


def frame_img(jid, ts):
    return rb.get(f"job:{jid}:img:{ts}")


# --- worker liveness + per job lock

def heartbeat(job=None):
    r.set(f"worker:{config.WORKER_ID}", job or "idle", ex=config.HB_TTL)


# take the lock if it's free, ours, or held by a worker whose heartbeat is gone (crashed)
_LOCK = r.register_script("""
local cur = redis.call('GET', KEYS[1])
if (not cur) or cur == ARGV[1] or redis.call('EXISTS', 'worker:' .. cur) == 0 then
  redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
  return 1
end
return 0
""")

_UNLOCK = r.register_script("""
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
""")


def lock(jid):
    return _LOCK(keys=[f"lock:{jid}"], args=[config.WORKER_ID, config.LOCK_TTL]) == 1


def unlock(jid):
    _UNLOCK(keys=[f"lock:{jid}"], args=[config.WORKER_ID])

# global rate limiter shared by every worker process, lives in redis.
#
# sliding window log: for the last `window` seconds we keep every request (and its token cost)
# in a sorted set. a request is let through only if both the count and the token sum stay under
# the limit for that window. this is stricter than a token bucket (no burst at the window edge),
# which is the point - a burst of jobs must never push the LLM provider into 429s.
import time
import uuid

_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local win = tonumber(ARGV[1])
local max_n = tonumber(ARGV[2])
local max_cost = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local id = ARGV[5]

redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, now - win)
redis.call('ZREMRANGEBYSCORE', KEYS[2], 0, now - win)

local wait = 0
if max_n > 0 then
  local n = redis.call('ZCARD', KEYS[1])
  if n >= max_n then
    local first = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
    wait = math.max(wait, tonumber(first[2]) + win - now)
  end
end

if max_cost > 0 then
  local items = redis.call('ZRANGE', KEYS[2], 0, -1, 'WITHSCORES')
  local used = 0
  for i = 1, #items, 2 do
    used = used + tonumber(string.match(items[i], '^(%d+):'))
  end
  if used + cost > max_cost then
    local need = used + cost - max_cost
    for i = 1, #items, 2 do
      need = need - tonumber(string.match(items[i], '^(%d+):'))
      if need <= 0 then
        wait = math.max(wait, tonumber(items[i + 1]) + win - now)
        break
      end
    end
  end
end

if wait > 0 then
  return tostring(wait)
end

redis.call('ZADD', KEYS[1], now, id)
redis.call('ZADD', KEYS[2], now, cost .. ':' .. id)
redis.call('EXPIRE', KEYS[1], math.ceil(win) + 5)
redis.call('EXPIRE', KEYS[2], math.ceil(win) + 5)
return '0'
"""


# after a call we know the real token count -> swap the reserved estimate for it
_SETTLE = """
local s = redis.call('ZSCORE', KEYS[1], ARGV[1])
if s then
  redis.call('ZREM', KEYS[1], ARGV[1])
  redis.call('ZADD', KEYS[1], s, ARGV[2])
end
return 1
"""


class Limiter:
    def __init__(self, r):
        self.r = r
        self.script = r.register_script(_LUA)
        self.settle_script = r.register_script(_SETTLE)

    def try_take(self, name, window, max_n=0, max_cost=0, cost=0, rid=None):
        """returns 0 if allowed, else how many seconds to wait"""
        if max_cost:
            cost = min(int(cost), max_cost)
        res = self.script(
            keys=[f"rl:{name}:n", f"rl:{name}:c"],
            args=[window, max_n, max_cost, int(cost), rid or uuid.uuid4().hex[:12]],
        )
        if isinstance(res, bytes):
            res = res.decode()
        return float(res)

    def take(self, name, window, max_n=0, max_cost=0, cost=0, on_wait=None, give_up=600):
        """block until we get a slot. returns a ticket for settle()"""
        if max_cost:
            cost = min(int(cost), max_cost)
        rid = uuid.uuid4().hex[:12]
        start = time.time()
        while True:
            w = self.try_take(name, window, max_n, max_cost, cost, rid)
            if w <= 0:
                return (name, rid, int(cost))
            if on_wait and w > 1.5:
                on_wait(w)
            if time.time() - start > give_up:
                raise TimeoutError(f"rate limiter {name}: waited too long")
            time.sleep(min(w, 5) + 0.05)

    def settle(self, ticket, actual):
        """replace the reserved estimate with what the call really used"""
        name, rid, est = ticket
        if actual is None or int(actual) == est:
            return
        self.settle_script(keys=[f"rl:{name}:c"], args=[f"{est}:{rid}", f"{int(actual)}:{rid}"])

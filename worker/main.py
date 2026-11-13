# worker process: pulls stage messages off notes.jobs and runs them.
#
# message = {job_id, stage, token, parent_id}, header x-attempt = retries used for this stage.
# order of things for one message:
#   run stage -> checkpoint "done" -> publish next stage (confirmed) -> mark forwarded -> ack
# so if we get SIGKILLed anywhere in there, rabbit redelivers and the rerun is a no-op / continues.
import asyncio
import json
import logging
import signal
import time

import aio_pika

from common import broker, config
from common import states as S

from . import pipeline, store
from .errors import Fatal, RateLimited, StageError

logging.basicConfig(level=logging.INFO, format="%(asctime)s worker %(levelname)s %(message)s")
log = logging.getLogger("worker")
logging.getLogger("aio_pika").setLevel(logging.WARNING)
logging.getLogger("aiormq").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

current = {"job": None}


def backoff(attempt, retry_after=0):
    """attempt is 1 based. picks from RETRY_DELAYS, but never shorter than what the API asked for"""
    ds = config.RETRY_DELAYS
    d = ds[min(attempt - 1, len(ds) - 1)]
    if retry_after > d:
        bigger = [x for x in broker.retry_delays() if x >= retry_after]
        d = bigger[0] if bigger else max(broker.retry_delays())
    return d


async def heartbeat_loop():
    while True:
        try:
            store.heartbeat(current["job"])
            if current["job"]:
                store.r.expire(f"lock:{current['job']}", config.LOCK_TTL)
        except Exception as e:
            log.warning("heartbeat failed: %s", e)
        await asyncio.sleep(max(1, config.HB_TTL // 3))


async def dead_letter(ch, data, jid, code, msg, attempts):
    body = {k: v for k, v in data.items() if k != "token"}
    body.update(reason=code, message=msg, attempts=attempts, worker=config.WORKER_ID,
                at=store.now())
    # keep the sealed token in there so the message can be replayed by hand if needed
    body["token"] = data.get("token")
    await broker.publish(ch, config.DLQ, body, headers={"x-reason": code})
    store.move(jid, S.DEAD_LETTERED, waiting="", detail="Moved to dead-letter queue",
               error={"code": code, "message": msg, "attempts": attempts, "stage": data.get("stage")})
    store.r.sadd("dlq:jobs", jid)
    log.warning("job %s dead-lettered at stage %s: %s", jid, data.get("stage"), msg)


async def handle(ch, msg):
    try:
        data = json.loads(msg.body)
        jid, stage = data["job_id"], data["stage"]
    except Exception:
        log.error("bad message, dropping: %r", msg.body[:200])
        await msg.ack()
        return

    hdr = msg.headers or {}
    attempt = int(hdr.get("x-attempt", 0))
    # crashes seen so far: rabbit's own redelivery count + what we carried over when re-queueing
    crashes = int(hdr.get("x-delivery-count", 1 if msg.redelivered else 0)) + int(hdr.get("x-crashes", 0))

    job = store.get(jid)
    if not job or job.get("status") in S.TERMINAL:
        await msg.ack()
        return

    nxt = S.next_stage(stage)

    # already done earlier (duplicate / redelivered after the work was saved)
    if store.stage_done(jid, stage):
        if nxt and not store.forwarded(jid, stage):
            await broker.publish(ch, config.JOBS_Q, {**data, "stage": nxt}, headers={"x-attempt": 0})
            store.mark_forwarded(jid, stage)
        await msg.ack()
        return

    # a worker died on this message too many times -> poison message, park it
    if crashes > config.CRASH_LIMIT:
        await dead_letter(ch, data, jid, "WORKER_CRASH_LOOP",
                          f"Stage '{stage}' crashed a worker {crashes} times", crashes)
        await msg.ack()
        return

    # make sure no other live worker is running this job right now
    # (if the holder just got killed, its heartbeat expires within HB_TTL and we can take over)
    got = store.lock(jid)
    deadline = time.time() + config.HB_TTL + 1
    while not got and time.time() < deadline:
        await asyncio.sleep(0.25)
        got = store.lock(jid)
    if not got:
        await broker.publish(ch, broker.retry_queue(config.LOCK_WAIT), data,
                             headers={"x-attempt": attempt, "x-crashes": crashes})
        await msg.ack()
        return

    if crashes:
        log.info("job %s stage %s redelivered (delivery count %d) - resuming", jid, stage, crashes)
    current["job"] = jid
    store.update(jid, worker=config.WORKER_ID)
    t0 = time.time()
    try:
        await asyncio.to_thread(pipeline.run, stage, jid, data)
        store.mark_done(jid, stage)
        store.unlock(jid)
        if nxt:
            await broker.publish(ch, config.JOBS_Q, {**data, "stage": nxt}, headers={"x-attempt": 0})
        store.mark_forwarded(jid, stage)
        await msg.ack()
        log.info("job %s stage %s ok (%.1fs)", jid, stage, time.time() - t0)

    except Fatal as e:
        log.warning("job %s failed at %s: %s %s", jid, stage, e.code, e.msg)
        store.move(jid, S.FAILED, waiting="", detail="Failed",
                   error={"code": e.code, "message": e.msg, "stage": stage})
        await msg.ack()

    except Exception as e:
        if not isinstance(e, StageError):
            log.exception("unexpected error in job %s stage %s", jid, stage)
            e = StageError("INTERNAL_ERROR", f"{e.__class__.__name__}: {str(e)[:200]}")
        attempt += 1
        if attempt >= config.MAX_ATTEMPTS:
            await dead_letter(ch, data, jid, "RETRIES_EXHAUSTED",
                              f"Gave up after {attempt} attempts. Last error: {e.msg}", attempt)
        else:
            delay = backoff(attempt, e.retry_after)
            kind = "Rate limited" if isinstance(e, RateLimited) else "Temporary problem"
            store.update(jid, detail=f"{kind}: {e.msg}. Retrying in {delay}s",
                         waiting={"code": e.code, "reason": e.msg, "attempt": attempt,
                                  "max_attempts": config.MAX_ATTEMPTS, "retry_in": delay,
                                  "retry_at": time.time() + delay})
            await broker.publish(ch, broker.retry_queue(delay), data,
                                 headers={"x-attempt": attempt, "x-crashes": crashes})
            log.info("job %s stage %s: %s -> retry %d/%d in %ds", jid, stage, e.code, attempt,
                     config.MAX_ATTEMPTS, delay)
        await msg.ack()
    finally:
        current["job"] = None
        store.unlock(jid)


async def main():
    store.heartbeat()
    conn = None
    for i in range(30):
        try:
            conn = await aio_pika.connect_robust(config.RABBITMQ_URL, heartbeat=30)
            break
        except Exception as e:
            log.warning("rabbit not ready (%s), retry %d", e, i + 1)
            await asyncio.sleep(2)
    if conn is None:
        raise SystemExit("no rabbitmq")

    ch = await conn.channel(publisher_confirms=True)
    await ch.set_qos(prefetch_count=1)  # one job at a time per worker
    q = await broker.declare(ch)

    hb = asyncio.create_task(heartbeat_loop())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    log.info("worker %s ready, llm=%s rpm=%d tpm=%d, retries=%d %s",
             config.WORKER_ID, config.LLM_MODEL, config.LLM_RPM, config.LLM_TPM,
             config.MAX_ATTEMPTS, config.RETRY_DELAYS)

    # no_ack=False -> manual ack. anything unacked when we die goes back to the queue
    async with q.iterator(no_ack=False) as it:
        getter = asyncio.ensure_future(it.__anext__())
        stopper = asyncio.ensure_future(stop.wait())
        while True:
            done, _ = await asyncio.wait({getter, stopper}, return_when=asyncio.FIRST_COMPLETED)
            if stopper in done:
                break
            await handle(ch, getter.result())
            if stop.is_set():
                break
            getter = asyncio.ensure_future(it.__anext__())
        getter.cancel()

    hb.cancel()
    store.r.delete(f"worker:{config.WORKER_ID}")
    await conn.close()
    log.info("worker %s stopped", config.WORKER_ID)


if __name__ == "__main__":
    asyncio.run(main())

# rabbitmq topology, declared by both gateway and worker (declares are idempotent)
#
#   notes.jobs        quorum queue, every stage message goes here
#   notes.retry.<N>s  holding queues with a TTL, expired msgs dead-letter back into notes.jobs
#   notes.dlq         dead letters, for manual inspection
import json

import aio_pika

from . import config


def retry_queue(sec):
    return f"notes.retry.{sec}s"


def retry_delays():
    return sorted(set(config.RETRY_DELAYS + [config.LOCK_WAIT]))


async def declare(ch):
    await ch.declare_queue(config.DLQ, durable=True)
    q = await ch.declare_queue(
        config.JOBS_Q,
        durable=True,
        arguments={
            "x-queue-type": "quorum",
            # safety net only, the worker dead-letters itself before this (see CRASH_LIMIT)
            "x-delivery-limit": config.CRASH_LIMIT + 3,
            "x-dead-letter-exchange": "",
            "x-dead-letter-routing-key": config.DLQ,
        },
    )
    for sec in retry_delays():
        await ch.declare_queue(
            retry_queue(sec),
            durable=True,
            arguments={
                "x-message-ttl": sec * 1000,
                "x-dead-letter-exchange": "",
                "x-dead-letter-routing-key": config.JOBS_Q,
            },
        )
    return q


async def publish(ch, routing_key, body, headers=None):
    # channel is opened with publisher confirms, so this returns only once rabbit has it on disk
    msg = aio_pika.Message(
        json.dumps(body).encode(),
        content_type="application/json",
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        headers=headers or {},
    )
    await ch.default_exchange.publish(msg, routing_key=routing_key)

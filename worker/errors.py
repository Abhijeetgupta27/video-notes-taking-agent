import random

from common import config


class StageError(Exception):
    retry = True

    def __init__(self, code, msg, retry_after=0):
        super().__init__(msg)
        self.code = code
        self.msg = msg
        self.retry_after = retry_after or 0


class Retryable(StageError):
    """transient - goes back on the queue with backoff, counts against MAX_ATTEMPTS"""


class RateLimited(Retryable):
    def __init__(self, service, msg, retry_after=0):
        super().__init__("RATE_LIMITED", msg, retry_after)
        self.service = service


class Fatal(StageError):
    """no point retrying (video too long, bad token...) -> FAILED"""
    retry = False


def chaos(service):
    # CHAOS_429 env var, only for demoing the backoff path
    p = config.CHAOS_429.get(service, 0)
    if p and random.random() < p:
        raise RateLimited(service, f"{service} returned 429 (simulated by CHAOS_429)", retry_after=0)

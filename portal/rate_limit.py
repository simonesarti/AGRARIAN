"""
Rate limiting for the two endpoints anyone on the internet can reach.

`/login` is a password oracle and `/register` creates accounts; both are
anonymous by construction — a sign-in form cannot require a session — so neither
can be protected by a credential. Counting is the only thing left.

Where the counters live
-----------------------
Redis, which the hub already runs for ws-server's alert fan-out. Not process
memory: the portal is replicated, and N replicas each holding their own counter
is a limit of N × the number written down. That is the same statelessness
argument the session makes, arriving at the opposite answer — a session can live
in a signed cookie because the client can carry it, and a rate limit cannot,
because the client is the thing being limited.

A separate logical database (`/1` by default) from ws-server's fan-out. Nothing
would collide — these are `INCR` keys, those are pub/sub channels — but a
`FLUSHDB` aimed at one should not reach the other.

Two counters per login attempt
------------------------------
Per-IP alone is evaded by a botnet: a thousand hosts trying ten passwords each.
Per-account alone is evaded by spraying one common password across many accounts
from one host. Neither bound implies the other, so both are kept.

Failures are counted and successes are not, so a busy legitimate user is never
locked out by their own activity. On success the *account* counter is cleared and
the IP counter deliberately is not: an attacker who holds one valid account would
otherwise reset their own IP budget whenever they liked.

Fixed windows, not sliding
--------------------------
A fixed window permits a burst of up to 2× the limit across a window boundary,
and the counting is check-then-increment, so simultaneous requests can overshoot
by a few. Both are accepted. The purpose here is to turn an unbounded guessing
rate into a bounded one, and an attacker who gets 21 attempts instead of 20 has
gained nothing that matters — unlike the stream cap in §4, where an overshoot is
a GPU container somebody pays for, and which therefore takes a row lock.

Failure is open, on purpose
---------------------------
If Redis cannot be reached the request is allowed and the failure logged loudly.
A rate limiter that turns a Redis outage into "nobody can sign in" has become a
worse outage than the attack it prevents. The configuration is required at
startup so the limiter cannot be silently absent; only the runtime failure is
tolerated.
"""

import hashlib
import logging
from typing import Optional, Sequence, Tuple

import redis.asyncio as redis_async
from redis.exceptions import RedisError

logger = logging.getLogger("portal.rate_limit")


def account_key(email: str) -> str:
    """
    A bucket for one account, named without storing the address.

    Normalised exactly as db-writer normalises it before storing, or `Alice@` and
    `alice@` would be two buckets for one account and the per-account limit would
    be bypassed by pressing shift.

    Hashed because these keys are the only place the portal would otherwise hold
    a list of user email addresses, and it has no reason to hold one.
    """
    normalised = email.strip().lower()
    return "rl:login:acct:" + hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:32]


def ip_key(kind: str, ip: str) -> str:
    return f"rl:{kind}:ip:{ip}"


def client_ip(peer: Optional[str], forwarded_for: Optional[str], trusted_hops: int) -> str:
    """
    The address to count against, given how many proxies sit in front of us.

    `X-Forwarded-For` is appended to by each proxy, so with `trusted_hops`
    proxies between the client and here, the client is the entry `trusted_hops`
    from the RIGHT. Everything further left was supplied by the client and is
    forgeable.

    Counting from the right is the whole safety of this function. Taking the
    leftmost entry — the common shortcut — lets a client name its own bucket,
    which does not merely evade the limit: it lets one client push another
    client's bucket to the limit and lock them out.

    trusted_hops = 0 means nothing is trusted and the peer address is used, which
    is right for a portal reached directly and is the default. A header shorter
    than the configured hop count means the request did not arrive through the
    expected proxy chain, so the peer address is used then too.
    """
    if trusted_hops > 0 and forwarded_for:
        parts = [p.strip() for p in forwarded_for.split(",") if p.strip()]
        if len(parts) >= trusted_hops:
            return parts[-trusted_hops]
        logger.warning(
            f"X-Forwarded-For has {len(parts)} entries but {trusted_hops} hops are "
            f"trusted; falling back to the peer address")
    return peer or "unknown"


class RateLimiter:
    """Fixed-window counters in Redis. One window length for every counter."""

    def __init__(self, url: str, window_s: int):
        self._window_s = window_s
        # Lazily connected: from_url opens nothing, so building this at import
        # time cannot delay or fail startup on a Redis that is still coming up.
        self._redis = redis_async.from_url(url, decode_responses=True)

    @property
    def window_s(self) -> int:
        return self._window_s

    async def blocked_for(self, buckets: Sequence[Tuple[str, int]]) -> Optional[int]:
        """
        Seconds until the caller may retry, or None if it may proceed now.

        Takes every bucket at once and reports the longest wait, so a caller over
        two limits is told the truth rather than being let through by the looser
        one. Reads only — nothing is counted here, because the thing worth
        counting is the outcome, and that is not known yet.
        """
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                for key, _ in buckets:
                    pipe.get(key)
                    pipe.ttl(key)
                values = await pipe.execute()
        except RedisError as e:
            logger.error(f"Rate limiter unavailable, allowing the request: {e}")
            return None

        worst = None
        for (key, limit), count, ttl in zip(buckets, values[0::2], values[1::2]):
            if count is None or int(count) < limit:
                continue
            # -1 is a key with no expiry and -2 is one that vanished between the
            # GET and the TTL; neither should happen, and neither is a reason to
            # answer "wait forever".
            wait = ttl if ttl and ttl > 0 else self._window_s
            logger.warning(f"Rate limit reached on {key}: {count}/{limit}, {wait}s to go")
            worst = wait if worst is None else max(worst, wait)
        return worst

    async def record(self, *keys: str) -> None:
        """
        Count one attempt against each bucket.

        EXPIRE only when INCR returns 1 — that is what makes the window fixed.
        Setting it on every increment would slide the deadline forward with each
        attempt, so a steady attacker would never see a counter reset and, worse,
        would keep an unrelated client sharing that NAT locked out indefinitely.
        """
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                for key in keys:
                    pipe.incr(key)
                counts = await pipe.execute()
            fresh = [k for k, c in zip(keys, counts) if c == 1]
            if fresh:
                async with self._redis.pipeline(transaction=False) as pipe:
                    for key in fresh:
                        pipe.expire(key, self._window_s)
                    await pipe.execute()
        except RedisError as e:
            logger.error(f"Rate limiter unavailable, attempt not counted: {e}")

    async def forget(self, *keys: str) -> None:
        """Clear a bucket. Used on a successful login, for the account only."""
        try:
            await self._redis.delete(*keys)
        except RedisError as e:
            logger.error(f"Rate limiter unavailable, counter not cleared: {e}")

    async def close(self) -> None:
        try:
            await self._redis.aclose()
        except RedisError:
            pass


def retry_message(seconds: int) -> str:
    """A wait a person can act on. Rounded up — 'try again in 0 minutes' is not advice."""
    minutes = max(1, (seconds + 59) // 60)
    unit = "minute" if minutes == 1 else "minutes"
    return f"Too many attempts. Try again in about {minutes} {unit}."

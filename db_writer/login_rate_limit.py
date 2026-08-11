"""
A brake on password guessing at db-writer's own /login.

WHY THIS EXISTS SEPARATELY FROM THE PORTAL'S LIMITER
----------------------------------------------------
CLOUD_ARCHITECTURE.md §4 bounds the *public* door: the portal counts failures per
account and per source address in Redis before it ever calls db-writer, so an
over-limit attempt costs no bcrypt verification. §9 then records what that leaves:

    "db-writer's own /login is still unrated. The public door is now bounded, but
     the endpoint behind it is not: anything that can reach db-writer directly can
     still guess passwords at bcrypt's pace."

That is a note rather than a hole only because §8 keeps db-writer unroutable from
outside. It becomes a hole the moment something else on the private network is
compromised, or the day db-writer is routed somewhere it should not be. This is
defence in depth for exactly that, and it is deliberately NOT a copy of the
portal's limiter.

ONE COUNTER, NOT TWO, AND THAT IS THE DESIGN
--------------------------------------------
The portal keeps two counters because neither bound implies the other: per-address
alone is evaded by a botnet, per-account alone by spraying one password across many
accounts from one host.

Here the second one would be actively harmful. Every request db-writer sees comes
from the portal, so a per-address counter would put every tenant in the world into
one bucket and the first attacker would lock out everybody — which is precisely the
failure §8 describes for PORTAL_TRUSTED_PROXY_HOPS set too low, arriving by a
different route. So this counts per account only, and the address half stays where
it can see real addresses.

The limit is deliberately looser than the portal's ten. This sits BEHIND that one,
so in normal operation it should never be the binding constraint — if it fires,
either the portal is bypassed or something is wrong. Making it tight would mean the
inner door rejecting traffic the outer door already approved.

FAILS OPEN, for the reason the portal's does
--------------------------------------------
If Redis cannot be reached the attempt is allowed and the failure logged. A limiter
that turns a Redis outage into "nobody can sign in" is a worse outage than the
attack it prevents. REDIS_URL is nonetheless optional here rather than required:
db-writer ran without Redis until now, and a deployment that has not been updated
should keep working exactly as it did rather than refusing to start. Absent
configuration is logged once at startup, so it cannot be silently missing.
"""

import hashlib
import logging
import os
from typing import Optional

logger = logging.getLogger("db_writer.login_rate_limit")

# Same window and shape as the portal's account counter, an order of magnitude
# looser. See the module docstring for why loose is correct for an inner door.
WINDOW_S = int(os.getenv("LOGIN_RATE_WINDOW_S", "900"))
MAX_FAILURES = int(os.getenv("LOGIN_RATE_MAX_FAILURES", "100"))

_redis = None
_enabled = False


def init() -> None:
    """Connect if REDIS_URL is set. Called once at startup; never raises."""
    global _redis, _enabled
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        logger.warning(
            "REDIS_URL is not set — db-writer's /login is UNRATED. This is safe only "
            "while db-writer is unroutable from outside (CLOUD_ARCHITECTURE.md §8).")
        return
    try:
        import redis
        _redis = redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
        _redis.ping()
        _enabled = True
        logger.info(f"/login rate limiting active: {MAX_FAILURES} failures / {WINDOW_S}s per account")
    except Exception as e:
        logger.error(f"Could not reach Redis for /login rate limiting: {e} — failing open")


def _key(email: str) -> str:
    """
    Hashed, because these keys would otherwise be the one place db-writer keeps a
    plain list of registered email addresses, and it has no reason to keep one.
    Normalised first: without it `Alice@` and `alice@` are two buckets for one
    account and the limit is bypassed by pressing shift — the same defect §3
    describes for the unique constraint.
    """
    return "dbw:login:acct:" + hashlib.sha256(email.strip().lower().encode()).hexdigest()


def blocked(email: str) -> Optional[int]:
    """Seconds to wait if this account is over its budget, else None."""
    if not _enabled:
        return None
    try:
        key = _key(email)
        count = _redis.get(key)
        if count is not None and int(count) >= MAX_FAILURES:
            ttl = _redis.ttl(key)
            return ttl if ttl and ttl > 0 else WINDOW_S
    except Exception as e:
        logger.error(f"Rate-limit check failed, allowing the request: {e}")
    return None


def record_failure(email: str) -> None:
    """Count one failed attempt. Fixed window: the TTL is set on first increment."""
    if not _enabled:
        return
    try:
        key = _key(email)
        pipe = _redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, WINDOW_S, nx=True)
        pipe.execute()
    except Exception as e:
        logger.error(f"Could not record a login failure: {e}")


def clear(email: str) -> None:
    """
    Forget an account's failures after a success.

    Only the account counter exists here, so unlike the portal there is no second
    bucket that must deliberately survive. A correct password is evidence the
    account is not being guessed at.
    """
    if not _enabled:
        return
    try:
        _redis.delete(_key(email))
    except Exception as e:
        logger.error(f"Could not clear login failures: {e}")

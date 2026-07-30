"""
Token minting and publisher verification for db-writer.

db-writer is the only service that mints tokens, because it is the only one that
authenticates users. ws-server merely validates what it signed, which is why both
carry the same SESSION_JWT_SECRET and neither needs to call the other.

Two kinds of token, distinguished by their `scope` claim:

  view     — issued to a browser, grants reading one flight's alert stream
  publish  — issued to an app container, grants writing alerts to one flight

They are signed with the same key, so the scope claim is load-bearing: without
checking it, a viewer token would be a valid publisher token for the same flight
and anyone watching could inject alerts into what they were watching.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt

from constants import (
    JWT_ALGORITHM,
    PUBLISHER_TOKEN_TTL_S,
    TOKEN_SCOPE_PUBLISH,
    TOKEN_SCOPE_VIEW,
    VIEWER_TOKEN_TTL_S,
)

logger = logging.getLogger("db_writer.auth")


class AuthError(Exception):
    """Raised when a credential is missing, malformed, expired, or out of scope."""


# Fail fast at import: a db-writer running without this would mint tokens nothing
# could verify, and accept none.
_JWT_SECRET = os.environ["SESSION_JWT_SECRET"]

_VIEWER_TTL_S = int(os.environ.get("VIEWER_TOKEN_TTL_S", VIEWER_TOKEN_TTL_S))
_PUBLISHER_TTL_S = int(os.environ.get("PUBLISHER_TOKEN_TTL_S", PUBLISHER_TOKEN_TTL_S))


def _mint(flight_id: int, scope: str, ttl_s: int, subject: Optional[str] = None) -> str:
    now = datetime.now(timezone.utc)
    claims = {
        "flight_id": flight_id,
        "scope": scope,
        "iat": now,
        "exp": now + timedelta(seconds=ttl_s),
    }
    if subject is not None:
        claims["sub"] = subject
    return jwt.encode(claims, _JWT_SECRET, algorithm=JWT_ALGORITHM)


def mint_viewer_token(flight_id: int, user_id: int) -> str:
    """
    Sign a token granting read access to one flight's alert stream.

    ws-server validates this offline, so no replica of it needs to reach the DB to
    authorise a viewer. flight_id is an autoincrement PK and therefore guessable —
    the signature, not the id, is what carries authority.
    """
    return _mint(flight_id, TOKEN_SCOPE_VIEW, _VIEWER_TTL_S, subject=str(user_id))


def mint_publisher_token(flight_id: int) -> str:
    """
    Sign a token granting alert-write access to exactly one flight.

    This replaces a single shared secret held by every app container. The point of
    the change is blast radius: a leaked token now writes to one flight that is
    already in progress, instead of to any flight_id the holder cares to guess.
    """
    return _mint(flight_id, TOKEN_SCOPE_PUBLISH, _PUBLISHER_TTL_S)


def flight_id_from_credential(token: Optional[str], expected_scope: str) -> int:
    """
    Validate a bare token and return the flight it is scoped to.

    Deliberately not named flight_id_from_token: ws_server/auth.py has a function by
    that name which takes one argument and implies the view scope. Two same-named
    functions with different signatures across the two services would be a trap.

    Takes the token itself rather than an Authorization header, because the MediaMTX
    auth hook does not deliver one — depending on protocol the credential arrives as
    a password, a bearer token or a query parameter, all of which are unwrapped by
    the caller before they reach here.

    Both checks matter: the signature proves we issued the token, the scope claim
    proves it was issued for the kind of access being attempted.
    """
    if not token:
        raise AuthError("missing token")

    try:
        claims = jwt.decode(token, _JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise AuthError("token expired")
    except jwt.InvalidTokenError as e:
        raise AuthError(f"invalid token: {e}")

    if claims.get("scope") != expected_scope:
        raise AuthError(f"token scope is not '{expected_scope}'")

    claimed = claims.get("flight_id")
    # bool is an int subclass — exclude it so True cannot pass as flight 1.
    if not isinstance(claimed, int) or isinstance(claimed, bool):
        raise AuthError("token carries no integer flight_id claim")

    return claimed


def verify_publisher(authorization: Optional[str], flight_id: int) -> None:
    """
    Check that the bearer token authorises writing to THIS flight.

    flight_id comes from the URL and is compared against the claim, so a valid token
    for flight 7 cannot be replayed against flight 8.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise AuthError("missing bearer token")

    claimed = flight_id_from_credential(authorization[len("Bearer "):], TOKEN_SCOPE_PUBLISH)

    if claimed != flight_id:
        raise AuthError(f"token is for flight {claimed}, not {flight_id}")

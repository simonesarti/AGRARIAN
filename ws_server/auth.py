"""
Authentication for the two trust boundaries ws_server sits on.

Both credentials are JWTs minted by db-writer and signed with the shared
SESSION_JWT_SECRET, so ws_server validates them offline — signature, expiry and
claims only, never a DB or network call. Any replica can therefore authorise any
client, which is what lets the service scale horizontally behind a plain
round-robin load balancer with no session affinity.

Viewers are external: they reach the WebSocket port through the reverse proxy and
present a token scoped to one flight.

Publishers are internal: app containers POST alerts on the API port, which is
never routed from outside the cluster network, and present a token scoped to the
one flight they were spawned to process.

The two are told apart by their `scope` claim, and that check is load-bearing.
Because both are signed with the same key, omitting it would make a viewer token a
valid publisher token for the same flight — letting anyone watching a flight inject
alerts into it.
"""

import logging
import os
from typing import Optional

import jwt

from constants import JWT_ALGORITHM, TOKEN_SCOPE_PUBLISH, TOKEN_SCOPE_VIEW

logger = logging.getLogger("ws_server.auth")


class AuthError(Exception):
    """Raised when a credential is missing, malformed, expired, or out of scope."""


# Fail fast at import: a ws_server running without this would have no way to tell a
# forged token from a real one.
_JWT_SECRET = os.environ["SESSION_JWT_SECRET"]


def _decode(token: str, expected_scope: str) -> dict:
    """Validate signature, expiry and scope; return the claims."""
    try:
        claims = jwt.decode(token, _JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise AuthError("token expired")
    except jwt.InvalidTokenError as e:
        raise AuthError(f"invalid token: {e}")

    if claims.get("scope") != expected_scope:
        raise AuthError(f"token scope is not '{expected_scope}'")

    return claims


def _flight_id_claim(claims: dict) -> int:
    flight_id = claims.get("flight_id")
    # bool is an int subclass — exclude it explicitly so True cannot pass as flight 1.
    if not isinstance(flight_id, int) or isinstance(flight_id, bool):
        raise AuthError("token carries no integer flight_id claim")
    return flight_id


def flight_id_from_token(token: Optional[str]) -> int:
    """
    Validate a viewer's JWT and return the flight it grants read access to.

    Raises AuthError on anything short of a valid, unexpired, view-scoped token
    carrying an integer flight_id claim.
    """
    if not token:
        raise AuthError("missing token")

    return _flight_id_claim(_decode(token, TOKEN_SCOPE_VIEW))


def verify_publisher(authorization: Optional[str], flight_id: int) -> None:
    """
    Check that the bearer token authorises publishing to THIS flight.

    flight_id comes from the URL and is compared against the claim, so a token
    issued for flight 7 cannot be replayed against flight 8. This is the difference
    from the shared secret it replaces: that one authorised writing to every flight
    at once, making it a network-boundary check rather than tenant isolation.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise AuthError("missing bearer token")

    claims = _decode(authorization[len("Bearer "):], TOKEN_SCOPE_PUBLISH)
    claimed = _flight_id_claim(claims)

    if claimed != flight_id:
        raise AuthError(f"token is for flight {claimed}, not {flight_id}")

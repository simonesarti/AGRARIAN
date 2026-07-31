"""
Session tokens: the portal's credential, and its separation from the other two.

All three kinds are signed with the same SESSION_JWT_SECRET, so the scope claim is
the only thing keeping them apart. That matters most here, because a viewer token
already carries a `sub` claim naming its user — so if the scope check were missing,
a token issued to watch one flight would BE a full account credential.

Run:  see README.md
"""
import os
import sys
from datetime import datetime, timedelta, timezone

os.environ.setdefault("SESSION_JWT_SECRET", "x" * 64)

# db_writer is not a package on the path; point at it explicitly so this runs from
# anywhere. DB_WRITER_DIR overrides for a container mount.
sys.path.insert(0, os.environ.get(
    "DB_WRITER_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "db_writer")))

import jwt

from auth import (
    AuthError,
    flight_id_from_credential,
    mint_publisher_token,
    mint_session_token,
    mint_viewer_token,
    user_id_from_session,
)
from constants import TOKEN_SCOPE_PUBLISH, TOKEN_SCOPE_SESSION, TOKEN_SCOPE_VIEW

SECRET = os.environ["SESSION_JWT_SECRET"]
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""))


def raises(fn, *a):
    """The AuthError message, or None if the call succeeded."""
    try:
        fn(*a)
        return None
    except AuthError as e:
        return str(e)


def bearer(t):
    return f"Bearer {t}"


def payload(t):
    return jwt.decode(t, SECRET, algorithms=["HS256"])


session3 = mint_session_token(3)
view7 = mint_viewer_token(7, user_id=3)
pub7 = mint_publisher_token(7)

# ── happy path ───────────────────────────────────────────────────────────────
check("a session token resolves to its user", user_id_from_session(bearer(session3)) == 3)
check("sub is a string, per RFC 7519", isinstance(payload(session3)["sub"], str))
check("scope is 'session'", payload(session3)["scope"] == TOKEN_SCOPE_SESSION)

# ── the escalation: one secret signs all three ───────────────────────────────
# A viewer token carries `sub` too, so the scope claim is the ONLY difference.
check("a viewer token carries a sub claim (so scope is what separates them)",
      payload(view7).get("sub") == "3")
e = raises(user_id_from_session, bearer(view7))
check("viewer token REJECTED as a session token", e is not None, e)
e = raises(user_id_from_session, bearer(pub7))
check("publisher token REJECTED as a session token", e is not None, e)

# ...and the other direction. A session token names no flight, so it cannot answer
# "which flight" even if some future caller forgets to check the scope.
check("a session token carries NO flight_id claim", "flight_id" not in payload(session3),
      str(sorted(payload(session3))))
e = raises(flight_id_from_credential, session3, TOKEN_SCOPE_VIEW)
check("session token REJECTED as a viewer token", e is not None, e)
e = raises(flight_id_from_credential, session3, TOKEN_SCOPE_PUBLISH)
check("session token REJECTED as a publisher token", e is not None, e)

# ── forgery and tampering ────────────────────────────────────────────────────
now = datetime.now(timezone.utc)


def mk(claims, ttl=300, secret=SECRET, alg="HS256"):
    return jwt.encode({**claims, "iat": now, "exp": now + timedelta(seconds=ttl)},
                      secret, algorithm=alg)


forged = mk({"scope": TOKEN_SCOPE_SESSION, "sub": "3"}, secret="w" * 64)
e = raises(user_id_from_session, bearer(forged))
check("a token signed with the wrong secret is rejected", e is not None, e)

expired = mk({"scope": TOKEN_SCOPE_SESSION, "sub": "3"}, ttl=-60)
e = raises(user_id_from_session, bearer(expired))
check("an expired session token is rejected", e is not None, e)

# alg:none is the classic JWT bypass — a token with no signature at all.
unsigned = jwt.encode({"scope": TOKEN_SCOPE_SESSION, "sub": "3"}, key="", algorithm="none")
e = raises(user_id_from_session, bearer(unsigned))
check("an unsigned (alg:none) token is rejected", e is not None, e)

e = raises(user_id_from_session, bearer(mk({"sub": "3"})))
check("a token with no scope is rejected", e is not None, e)

e = raises(user_id_from_session, bearer(mk({"scope": TOKEN_SCOPE_SESSION})))
check("a session token with no subject is rejected", e is not None, e)

e = raises(user_id_from_session, bearer(mk({"scope": TOKEN_SCOPE_SESSION, "sub": "root"})))
check("a non-numeric subject is rejected", e is not None, e)

# ── header handling ──────────────────────────────────────────────────────────
for bad, label in ((None, "missing header"),
                   ("", "empty header"),
                   (session3, "bare token with no Bearer prefix"),
                   ("Bearer ", "Bearer with no token"),
                   ("Basic " + session3, "wrong auth scheme")):
    e = raises(user_id_from_session, bad)
    check(f"rejected: {label}", e is not None, e)

# A valid token for a DIFFERENT user must resolve to that user and not to ours —
# the credential names the account, so there is nothing else to compare it against.
check("a session token for another user resolves to THAT user",
      user_id_from_session(bearer(mint_session_token(99))) == 99)

print()
print("=" * 60)
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)

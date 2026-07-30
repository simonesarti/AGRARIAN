"""
The MediaMTX auth decision, tested directly against a stub directory.

This is the endpoint every publish and every read passes through, so it is the
single point where a mistake exposes another tenant's video. It replaces nothing —
before this, MediaMTX served the annotated stream to anyone who knew the path.

No database and no HTTP server: media_auth.authorize() is deliberately free of both
so the decision can be exercised in isolation. The live end-to-end check against a
real MediaMTX lives in run_mediamtx_auth.sh.

Needs: pyjwt only. See README.md.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

# SESSION_JWT_SECRET must exist before db_writer.auth is imported — it reads it at
# import time on purpose, so a service without one refuses to start.
os.environ.setdefault("SESSION_JWT_SECRET", "test-secret-for-mediamtx-auth-checks")

sys.path.insert(0, os.environ.get(
    "DB_WRITER_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "db_writer"),
))

import jwt
from media_auth import Denied, authorize, credential_from

SECRET = os.environ["SESSION_JWT_SECRET"]

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""))


def token(flight_id, scope, ttl=300, secret=SECRET):
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"flight_id": flight_id, "scope": scope, "iat": now,
         "exp": now + timedelta(seconds=ttl)},
        secret, algorithm="HS256")


# ── Stub directory ────────────────────────────────────────────────────────────
# Two tenants. Alice owns stream 1 and flight 1; Bob owns stream 2 and flight 2.
# Stream 3 is revoked and belongs to nobody who matters.

KEY_ALICE   = "abcdefghjkmnpqrs"
KEY_BOB     = "0123456789abcdef"
KEY_REVOKED = "zzzzzzzzzzzzzzzz"

UUID_ALICE = "11111111-2222-3333-4444-555555555555"
UUID_BOB   = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class StubDirectory:
    def resolve_stream_key(self, key):
        return {
            KEY_ALICE: {"stream_id": 1, "user_id": 10, "label": "alice"},
            KEY_BOB:   {"stream_id": 2, "user_id": 20, "label": "bob"},
        }.get(key)   # revoked and unknown keys both resolve to None

    def resolve_public_uuid(self, public_uuid):
        return {UUID_ALICE: 1, UUID_BOB: 2}.get(public_uuid)

    def flight_stream_id(self, flight_id):
        return {1: 1, 2: 2}.get(flight_id)


D = StubDirectory()


def allowed(action, path, credential=None):
    try:
        authorize(action, path, credential, D)
        return True, ""
    except Denied as e:
        return False, str(e)


# ── Credential extraction ─────────────────────────────────────────────────────
# MediaMTX puts the credential in a different field per protocol and populates none
# of them reliably, so all of them are tried.

check("credential from bearer token field (WebRTC/HLS)",
      credential_from("", "", "tok-a", "") == "tok-a")
check("credential from password field (RTSP/RTMP url)",
      credential_from("u", "tok-b", "", "") == "tok-b")
check("credential from ?token= query (RTSP/RTMP)",
      credential_from("", "", "", "token=tok-c&x=1") == "tok-c")
check("credential from ?pass= query",
      credential_from("", "", "", "user=u&pass=tok-d") == "tok-d")
check("query with leading ? still parses",
      credential_from("", "", "", "?token=tok-e") == "tok-e")
check("token field wins over password",
      credential_from("", "pw", "tok-f", "") == "tok-f")
check("no credential anywhere -> None",
      credential_from("", "", "", "") is None)
check("username alone is never a credential",
      credential_from("alice", "", "", "") is None)

# ── Publish on the ingest path: the key IS the credential ─────────────────────

ok, why = allowed("publish", f"in/{KEY_ALICE}")
check("drone publishes on a live key, no other credential", ok, why)

ok, why = allowed("publish", f"in/{KEY_REVOKED}")
check("revoked/unknown key cannot publish", not ok, why)

ok, why = allowed("publish", "in/SHORT")
check("malformed key rejected by the path pattern", not ok, why)

ok, why = allowed("publish", f"in/{KEY_ALICE.upper()}")
check("uppercase key rejected (alphabet is lowercase)", not ok, why)

ok, why = allowed("publish", f"in/{KEY_ALICE}/extra")
check("path suffix cannot be smuggled past the anchor", not ok, why)

ok, why = allowed("publish", f"prefix/in/{KEY_ALICE}")
check("path prefix cannot be smuggled past the anchor", not ok, why)

# ── Publish on the output path: app container republishing ────────────────────

ok, why = allowed("publish", f"out/{UUID_ALICE}", token(1, "publish"))
check("app publishes annotated video to its own flight", ok, why)

ok, why = allowed("publish", f"out/{UUID_BOB}", token(1, "publish"))
check("app CANNOT publish into another flight's output path", not ok, why)

ok, why = allowed("publish", f"out/{UUID_ALICE}", None)
check("no credential cannot publish to an output path", not ok, why)

ok, why = allowed("publish", f"out/{UUID_ALICE}", token(1, "view"))
check("VIEWER token rejected as publisher on output path (scope)", not ok, why)

ok, why = allowed("publish", "out/99999999-9999-9999-9999-999999999999", token(1, "publish"))
check("unknown output uuid rejected even with a valid token", not ok, why)

# ── Read on the output path: the viewer ───────────────────────────────────────

ok, why = allowed("read", f"out/{UUID_ALICE}", token(1, "view"))
check("viewer reads the flight its token names", ok, why)

ok, why = allowed("read", f"out/{UUID_BOB}", token(1, "view"))
check("viewer CANNOT read another tenant's video", not ok, why)

ok, why = allowed("read", f"out/{UUID_ALICE}", None)
check("video is NOT readable without a token (the gap this closes)", not ok, why)

ok, why = allowed("read", f"out/{UUID_ALICE}", token(1, "publish"))
check("PUBLISHER token rejected as viewer on output path (scope)", not ok, why)

ok, why = allowed("read", f"out/{UUID_ALICE}", token(1, "view", secret="wrong-secret-entirely"))
check("forged signature rejected", not ok, why)

ok, why = allowed("read", f"out/{UUID_ALICE}", token(1, "view", ttl=-10))
check("expired viewer token rejected", not ok, why)

ok, why = allowed("read", f"out/{UUID_ALICE}", "not-a-jwt-at-all")
check("garbage credential rejected", not ok, why)

# ── Read on the ingest path: the app pulling raw video ────────────────────────

ok, why = allowed("read", f"in/{KEY_ALICE}", token(1, "publish"))
check("app reads the raw feed of the stream its flight runs on", ok, why)

ok, why = allowed("read", f"in/{KEY_BOB}", token(1, "publish"))
check("app CANNOT read another tenant's raw drone feed", not ok, why)

ok, why = allowed("read", f"in/{KEY_ALICE}", None)
check("raw feed not readable without a token", not ok, why)

ok, why = allowed("read", f"in/{KEY_ALICE}", token(1, "view"))
check("viewer token cannot open a raw ingest feed", not ok, why)

ok, why = allowed("read", f"in/{KEY_REVOKED}", token(1, "publish"))
check("revoked key cannot be read either", not ok, why)

# ── Scope confusion is the load-bearing check ─────────────────────────────────
# Both token kinds are signed with the same secret. Without the scope claim a
# viewer token would be a valid publisher token for the flight being watched.

viewer = token(1, "view")
publisher = token(1, "publish")
both_ways = (
    not allowed("publish", f"out/{UUID_ALICE}", viewer)[0]
    and not allowed("read", f"out/{UUID_ALICE}", publisher)[0]
)
check("scope separation holds in BOTH directions on one path", both_ways)

# ── Playback is gated exactly like a read ─────────────────────────────────────

ok, why = allowed("playback", f"out/{UUID_ALICE}", token(1, "view"))
check("playback allowed with a matching viewer token", ok, why)

ok, why = allowed("playback", f"out/{UUID_BOB}", token(1, "view"))
check("playback CANNOT reach another tenant's recording", not ok, why)

ok, why = allowed("playback", f"out/{UUID_ALICE}", None)
check("playback refused without a token", not ok, why)

# ── Everything else is denied ─────────────────────────────────────────────────

ok, why = allowed("read", "annot", token(1, "view"))
check("legacy 'annot' path no longer resolves", not ok, why)

ok, why = allowed("publish", "drone")
check("legacy 'drone' path no longer resolves", not ok, why)

ok, why = allowed("read", "", token(1, "view"))
check("empty path denied", not ok, why)

ok, why = allowed("api", "", None)
check("unknown action denied (new MediaMTX actions arrive closed)", not ok, why)

ok, why = allowed("", f"out/{UUID_ALICE}", token(1, "view"))
check("empty action denied", not ok, why)

# ── Summary ───────────────────────────────────────────────────────────────────

passed = sum(1 for _, ok, _ in results if ok)
print("\n" + "=" * 58)
print(f"{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)

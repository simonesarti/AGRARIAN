"""
The Mosquitto auth decision, tested directly against a stub directory.

This is the endpoint every CONNECT, PUBLISH and SUBSCRIBE on the telemetry
plane passes through, so it is the single point where a mistake lets one
tenant's drone hear (or spoof) another's telemetry. It replaces nothing there
was before: Mosquitto ran with allow_anonymous true and no ACLs — every client
could read and write every topic.

No database and no HTTP server, no Mosquitto: mqtt_auth.identify()/authorize()
are deliberately free of both so the decision can be exercised in isolation.
The live end-to-end check against a real mosquitto-go-auth container lives in
run_mqtt_auth.sh.

Needs: pyjwt only. See README.md.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

# SESSION_JWT_SECRET must exist before db_writer.auth is imported — it reads it at
# import time on purpose, so a service without one refuses to start.
os.environ.setdefault("SESSION_JWT_SECRET", "test-secret-for-mqtt-auth-checks")

sys.path.insert(0, os.environ.get(
    "DB_WRITER_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "db_writer"),
))

import jwt
from mqtt_auth import MQTT_ACC_READ, MQTT_ACC_SUBSCRIBE, MQTT_ACC_WRITE, Denied, authorize, identify

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
# Two tenants, mirroring test_mediamtx_auth.py's Alice/Bob. Alice's drone holds
# stream key KEY_ALICE and her flight (flight_id=1) was opened on it; same for
# Bob. Stream 3 is revoked and belongs to nobody who matters.

KEY_ALICE   = "abcdefghjkmnpqrs"
KEY_BOB     = "0123456789abcdef"
KEY_REVOKED = "zzzzzzzzzzzzzzzz"


class StubDirectory:
    def resolve_stream_key(self, key):
        return {
            KEY_ALICE: {"stream_id": 1, "user_id": 10, "label": "alice"},
            KEY_BOB:   {"stream_id": 2, "user_id": 20, "label": "bob"},
        }.get(key)   # revoked and unknown keys both resolve to None

    def flight_stream_id(self, flight_id):
        return {1: 1, 2: 2}.get(flight_id)


D = StubDirectory()


def allowed(username, topic, acc):
    try:
        authorize(username, topic, acc, D)
        return True, ""
    except Denied as e:
        return False, str(e)


# ── identify(): does CONNECT get past the door at all? ────────────────────────

check("a live stream key identifies (the drone)", identify(KEY_ALICE, D))
check("a revoked/unknown stream key does not identify", not identify(KEY_REVOKED, D))
check("a valid publisher token identifies (the app container)",
      identify(token(1, "publish"), D))
check("a viewer token does NOT identify (wrong scope for this plane)",
      not identify(token(1, "view"), D))
check("garbage identifies as neither a key nor a token", not identify("not-a-jwt-at-all", D))
check("empty username identifies nothing", not identify("", D))

# ── Publish (acc=2): the drone, topic key IS the credential ───────────────────

ok, why = allowed(KEY_ALICE, f"telemetry/{KEY_ALICE}/latitude", MQTT_ACC_WRITE)
check("drone publishes under its own key", ok, why)

ok, why = allowed(KEY_ALICE, f"telemetry/{KEY_BOB}/latitude", MQTT_ACC_WRITE)
check("drone CANNOT publish under another tenant's key", not ok, why)

ok, why = allowed(KEY_REVOKED, f"telemetry/{KEY_REVOKED}/latitude", MQTT_ACC_WRITE)
check("a revoked key cannot publish even under its own name", not ok, why)

ok, why = allowed(KEY_ALICE, f"telemetry/{KEY_ALICE}/rel_alt", MQTT_ACC_WRITE)
check("all four telemetry fields are valid publish topics", ok, why)

ok, why = allowed(KEY_ALICE, f"telemetry/{KEY_ALICE}/not_a_field", MQTT_ACC_WRITE)
check("an unrecognised field name is not a telemetry topic", not ok, why)

ok, why = allowed(KEY_ALICE, f"telemetry/{KEY_ALICE.upper()}/latitude", MQTT_ACC_WRITE)
check("uppercase key rejected (alphabet is lowercase)", not ok, why)

ok, why = allowed(KEY_ALICE, f"telemetry/{KEY_ALICE}/latitude/extra", MQTT_ACC_WRITE)
check("topic suffix cannot be smuggled past the anchor", not ok, why)

ok, why = allowed(KEY_ALICE, f"prefix/telemetry/{KEY_ALICE}/latitude", MQTT_ACC_WRITE)
check("topic prefix cannot be smuggled past the anchor", not ok, why)

ok, why = allowed(token(1, "publish"), f"telemetry/{KEY_ALICE}/latitude", MQTT_ACC_WRITE)
check("a publisher token cannot WRITE (that's the drone's credential, not the app's)",
      not ok, why)

# ── Subscribe / read (acc=4 / acc=1): the app container ───────────────────────

ok, why = allowed(token(1, "publish"), f"telemetry/{KEY_ALICE}/latitude", MQTT_ACC_SUBSCRIBE)
check("app subscribes to the telemetry of the stream its flight runs on", ok, why)

ok, why = allowed(token(1, "publish"), f"telemetry/{KEY_ALICE}/latitude", MQTT_ACC_READ)
check("app receives a delivered message on that same topic", ok, why)

ok, why = allowed(token(1, "publish"), f"telemetry/{KEY_BOB}/latitude", MQTT_ACC_SUBSCRIBE)
check("app CANNOT subscribe to another tenant's telemetry", not ok, why)

ok, why = allowed(token(2, "publish"), f"telemetry/{KEY_ALICE}/latitude", MQTT_ACC_SUBSCRIBE)
check("flight 2's token cannot read flight 1's telemetry either", not ok, why)

ok, why = allowed(KEY_ALICE, f"telemetry/{KEY_ALICE}/latitude", MQTT_ACC_SUBSCRIBE)
check("the drone's own key cannot subscribe (write-only credential)", not ok, why)

ok, why = allowed(token(1, "view"), f"telemetry/{KEY_ALICE}/latitude", MQTT_ACC_SUBSCRIBE)
check("a VIEWER token cannot subscribe to telemetry (scope)", not ok, why)

ok, why = allowed(token(1, "publish", secret="wrong-secret-entirely"),
                   f"telemetry/{KEY_ALICE}/latitude", MQTT_ACC_SUBSCRIBE)
check("forged signature rejected", not ok, why)

ok, why = allowed(token(1, "publish", ttl=-10), f"telemetry/{KEY_ALICE}/latitude", MQTT_ACC_SUBSCRIBE)
check("expired publisher token rejected", not ok, why)

ok, why = allowed(token(1, "publish"), f"telemetry/{KEY_REVOKED}/latitude", MQTT_ACC_SUBSCRIBE)
check("a revoked stream's topic cannot be subscribed to either", not ok, why)

# ── Everything else is denied ─────────────────────────────────────────────────

ok, why = allowed(token(1, "publish"), f"telemetry/{KEY_ALICE}/latitude", 3)
check("acc=3 (readwrite) is not a recognised access level here", not ok, why)

ok, why = allowed(KEY_ALICE, "telemetry/latitude", MQTT_ACC_WRITE)
check("the old flat (unscoped) topic no longer resolves", not ok, why)

ok, why = allowed(KEY_ALICE, "", MQTT_ACC_WRITE)
check("empty topic denied", not ok, why)

# ── Summary ───────────────────────────────────────────────────────────────────

passed = sum(1 for _, ok, _ in results if ok)
print("\n" + "=" * 58)
print(f"{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)

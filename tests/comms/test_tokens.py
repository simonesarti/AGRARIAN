"""Per-flight publisher token: scope separation and cross-flight replay."""
import os, sys
os.environ.setdefault("SESSION_JWT_SECRET", "x" * 64)

# ws_server is not a package on the path; point at it explicitly so this runs from
# anywhere. WS_SERVER_DIR overrides for a container mount.
sys.path.insert(0, os.environ.get(
    "WS_SERVER_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "ws_server")))

from auth import AuthError, flight_id_from_token, verify_publisher
import jwt
from datetime import datetime, timedelta, timezone

SECRET = os.environ["SESSION_JWT_SECRET"]
results = []
def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""))

def mk(flight_id, scope, ttl=300, secret=SECRET):
    now = datetime.now(timezone.utc)
    return jwt.encode({"flight_id": flight_id, "scope": scope, "iat": now,
                       "exp": now + timedelta(seconds=ttl)}, secret, algorithm="HS256")

def raises(fn, *a):
    try:
        fn(*a); return None
    except AuthError as e:
        return str(e)

pub7  = mk(7, "publish")
view7 = mk(7, "view")

# --- happy paths
check("publisher token authorises its own flight", raises(verify_publisher, f"Bearer {pub7}", 7) is None)
check("viewer token resolves its own flight", flight_id_from_token(view7) == 7)

# --- THE ESCALATION: same secret signs both kinds
e = raises(verify_publisher, f"Bearer {view7}", 7)
check("viewer token REJECTED as publisher (scope)", e is not None, e)
e = raises(flight_id_from_token, pub7)
check("publisher token REJECTED as viewer (scope)", e is not None, e)

# --- THE REPLAY: valid token, wrong flight
e = raises(verify_publisher, f"Bearer {pub7}", 8)
check("publisher token for flight 7 REJECTED on flight 8", e is not None, e)

# --- forged / expired / malformed
e = raises(verify_publisher, f"Bearer {mk(7,'publish',secret='wrong-secret-here')}", 7)
check("forged signature rejected", e is not None, e)
e = raises(verify_publisher, f"Bearer {mk(7,'publish',ttl=-10)}", 7)
check("expired token rejected", e is not None, e)
check("missing header rejected", raises(verify_publisher, None, 7) is not None)
check("non-bearer header rejected", raises(verify_publisher, pub7, 7) is not None)
check("garbage token rejected", raises(verify_publisher, "Bearer not.a.jwt", 7) is not None)

# --- no scope claim at all (old-format token)
now = datetime.now(timezone.utc)
legacy = jwt.encode({"flight_id": 7, "exp": now + timedelta(seconds=300)}, SECRET, algorithm="HS256")
check("scopeless legacy token rejected as publisher", raises(verify_publisher, f"Bearer {legacy}", 7) is not None)
check("scopeless legacy token rejected as viewer", raises(flight_id_from_token, legacy) is not None)

# --- bool is an int subclass
b = mk(True, "publish")
check("bool flight_id not accepted as flight 1", raises(verify_publisher, f"Bearer {b}", 1) is not None)

print("\n" + "="*58)
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)

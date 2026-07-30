"""db-writer replica safety: a flight opened on one replica must be writable on another."""
import json, os, sys, urllib.request, urllib.error

R1, R2 = "http://dbw-1:8000", "http://dbw-2:8000"
STREAM_KEY = os.environ["STREAM_KEY"]
results = []
def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""))

def post(url, body, token=None, method="POST"):
    h = {"Content-Type": "application/json"}
    if token: h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}

# 1. Open the flight on REPLICA 1 — this is the orchestrator's call, keyed by the
# stream key rather than end-user credentials.
st, body = post(f"{R1}/flight/open", {"stream_key": STREAM_KEY})
check("flight opened on replica 1", st == 200, f"status={st}")
flight_id = body["flight_id"]; tok = body["publisher_token"]
print(f"      flight_id={flight_id} public_uuid={body['public_uuid']}")

alert = {"frame_id": 1, "alert_msg": "written-via-replica-1", "timestamp": 1.0,
         "datetime": "2026-07-29T12:00:00", "image_data": None,
         "image_width": 640, "image_height": 480}

# 2. Write via replica 1 — worked before too
st, _ = post(f"{R1}/session/{flight_id}/alert", alert, tok)
check("alert accepted by replica 1 (the opener)", st == 200, f"status={st}")

# 3. THE TEST: write the same flight via REPLICA 2, which never saw /flight/open
alert2 = dict(alert, frame_id=2, alert_msg="written-via-replica-2")
st, _ = post(f"{R2}/session/{flight_id}/alert", alert2, tok)
check("alert accepted by replica 2 (never opened it)", st == 200,
      f"status={st}" + ("  <-- 404 = the old bug" if st == 404 else ""))

# 4. close on replica 2 — output_path is set at open time now, so there is no
# separate write path to exercise here; this closes the flight instead.
st, _ = post(f"{R2}/flight/{flight_id}/close", {}, tok)
check("flight close accepted by replica 2", st == 200, f"status={st}")

# 5. auth still enforced across replicas
st, _ = post(f"{R2}/session/{flight_id}/alert", alert2, None)
check("unauthenticated write still rejected on replica 2", st == 401, f"status={st}")
st, _ = post(f"{R2}/session/{flight_id + 999}/alert", alert2, tok)
check("token replay to another flight rejected on replica 2", st == 401, f"status={st}")

print("\n" + "="*60)
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)

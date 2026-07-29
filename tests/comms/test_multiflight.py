"""Multiple flights sharing one process-wide queue, interleaved across replicas."""
import json, sys, urllib.request, urllib.error
R = ["http://dbw-1:8000", "http://dbw-2:8000"]

def post(url, body, token=None):
    h = {"Content-Type": "application/json"}
    if token: h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}

# Two more flights for the same user (sole active stream -> both allowed)
flights = []
for i in range(2):
    st, b = post(f"{R[i % 2]}/session/start", {"email": "pilot@test.io", "password": "pw123"})
    assert st == 200, st
    flights.append((b["flight_id"], b["publisher_token"]))
print("opened flights:", [f for f, _ in flights])

# 20 alerts per flight, alternating replicas per request
sent = {f: 0 for f, _ in flights}
bad = 0
for n in range(20):
    for fid, tok in flights:
        st, _ = post(f"{R[n % 2]}/session/{fid}/alert",
                     {"frame_id": n, "alert_msg": f"f{fid}-n{n}", "timestamp": float(n),
                      "datetime": "2026-07-29T12:00:00", "image_data": None,
                      "image_width": 1, "image_height": 1}, tok)
        if st == 200: sent[fid] += 1
        else: bad += 1
print("accepted per flight:", sent, "| rejected:", bad)
sys.exit(0 if bad == 0 else 1)

"""
Does a ws-server replica resubscribe to Redis after the broker restarts?

The viewer stays connected throughout; only Redis goes away. If redis-py did NOT
resubscribe on reconnect, the WebSocket would stay open and simply never receive
anything again — a silent failure, which is the case worth proving.

Driven by run_redis_failure.sh, which restarts Redis when this prints READY_FOR_RESTART.
"""
import asyncio, json, sys, urllib.request
from datetime import datetime, timedelta, timezone
import jwt, websockets

SECRET = open("/t/rr-secret.txt").read().strip()
results = []
def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""))

def tok(flight_id, scope="view"):
    now = datetime.now(timezone.utc)
    return jwt.encode({"flight_id": flight_id, "scope": scope, "iat": now,
                       "exp": now + timedelta(seconds=900)}, SECRET, algorithm="HS256")

def publish(host, flight_id, frame):
    req = urllib.request.Request(
        f"http://{host}:8000/session/{flight_id}/alert",
        data=json.dumps({"frame_id": frame, "alert_msg": f"frame-{frame}"}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {tok(flight_id, 'publish')}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status

async def recv(ws, timeout):
    try:
        return json.loads(await asyncio.wait_for(ws.recv(), timeout))
    except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
        return None

async def phase1():
    ws = await websockets.connect(f"ws://rr-ws2:8765/?token={tok(5)}")
    await asyncio.sleep(1.0)
    publish("rr-ws1", 5, 1)
    got = await recv(ws, 5.0)
    check("baseline: cross-replica delivery works", got is not None and got.get("frame_id") == 1, f"got={got}")
    return ws

async def phase2(ws):
    # Called after the host has restarted Redis.
    publish("rr-ws1", 5, 2)
    got = await recv(ws, 15.0)
    check("SAME viewer receives alerts after Redis restart", got is not None and got.get("frame_id") == 2,
          f"got={got}" + ("  <-- silent failure: resubscribe did NOT happen" if got is None else ""))
    # And a second one, to be sure it is not a one-off
    publish("rr-ws1", 5, 3)
    got = await recv(ws, 10.0)
    check("delivery is stable afterwards", got is not None and got.get("frame_id") == 3, f"got={got}")
    await ws.close()

async def run():
    ws = await phase1()
    print("\n  >>> READY_FOR_RESTART <<<\n", flush=True)
    # Host restarts Redis during this window.
    await asyncio.sleep(20)
    await phase2(ws)
    print("\n" + "="*60)
    print(f"{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1

sys.exit(asyncio.run(run()))

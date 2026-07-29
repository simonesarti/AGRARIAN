"""Redis fully down for a sustained period, then back. What survives?"""
import asyncio, json, sys, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone
import jwt, websockets

SECRET = open("/t/rr-secret.txt").read().strip()
results = []
def check(name, ok, detail=""):
    results.append(ok); print(("PASS  " if ok else "FAIL  ")+name+(f"   [{detail}]" if detail else ""))

def tok(f, scope="view"):
    now = datetime.now(timezone.utc)
    return jwt.encode({"flight_id": f, "scope": scope, "iat": now,
                       "exp": now+timedelta(seconds=900)}, SECRET, algorithm="HS256")

def publish(host, f, frame):
    req = urllib.request.Request(f"http://{host}:8000/session/{f}/alert",
        data=json.dumps({"frame_id": frame, "alert_msg": f"f{frame}"}).encode(),
        headers={"Content-Type":"application/json","Authorization":f"Bearer {tok(f,'publish')}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=8) as r: return r.status
    except urllib.error.HTTPError as e: return e.code
    except Exception as e: return type(e).__name__

async def recv(ws, t):
    try: return json.loads(await asyncio.wait_for(ws.recv(), t))
    except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed): return None

async def run():
    ws = await websockets.connect(f"ws://rr-ws2:8765/?token={tok(9)}")
    await asyncio.sleep(1.0)
    publish("rr-ws1", 9, 1)
    got = await recv(ws, 5.0)
    check("baseline delivery", got is not None and got["frame_id"] == 1, f"got={got}")

    print("\n  >>> STOP_REDIS <<<\n", flush=True)
    await asyncio.sleep(12)                       # host stops Redis during this window

    st = publish("rr-ws1", 9, 2)                  # published while Redis is down
    check("publish during outage fails loudly (not silently OK)", st != 200, f"status={st}")
    got = await recv(ws, 3.0)
    check("nothing delivered during outage", got is None, f"leaked={got}")
    check("viewer socket still open during outage", ws.state.name == "OPEN", f"state={ws.state.name}")

    print("\n  >>> START_REDIS <<<\n", flush=True)
    await asyncio.sleep(20)                       # host starts Redis during this window

    st = publish("rr-ws1", 9, 3)
    check("publish works again after recovery", st == 200, f"status={st}")
    got = await recv(ws, 15.0)
    check("SAME viewer receives again after full outage", got is not None and got.get("frame_id") == 3,
          f"got={got}")
    check("alert published during outage NOT replayed", got is None or got.get("frame_id") != 2,
          "best-effort by design")
    await ws.close()
    print("\n"+"="*60); print(f"{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1

sys.exit(asyncio.run(run()))

"""
Cross-replica fan-out: a viewer on replica 2 must receive an alert published to
replica 1.

This is what Redis pub/sub exists for. Without it, ws-server's in-memory session map
would only deliver to viewers that happened to land on the same replica as the
publisher — and with a round-robin load balancer, that is a coin flip.

Needs TWO ws-server replicas sharing one Redis. See README.md.
"""
import asyncio
import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone

import jwt
import websockets

SECRET = os.environ["SESSION_JWT_SECRET"]
PUBLISHER_HOST = os.environ.get("PUBLISH_TO", "ws-server")     # replica 1
VIEWER_HOST = os.environ.get("VIEW_ON", "ws-replica-2")        # replica 2


def token(flight_id, scope="view"):
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"flight_id": flight_id, "scope": scope, "iat": now,
         "exp": now + timedelta(seconds=300)},
        SECRET, algorithm="HS256")


def post_alert(host, flight_id, body):
    req = urllib.request.Request(
        f"http://{host}:8000/session/{flight_id}/alert",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token(flight_id, 'publish')}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status


async def recv(ws, timeout=3.0):
    try:
        return json.loads(await asyncio.wait_for(ws.recv(), timeout))
    except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
        return None


async def main():
    ok = True
    # Two viewers of different flights, both on the replica that will NOT publish.
    async with websockets.connect(f"ws://{VIEWER_HOST}:8765/?token={token(7)}") as v7, \
               websockets.connect(f"ws://{VIEWER_HOST}:8765/?token={token(8)}") as v8:
        await asyncio.sleep(0.5)

        # Publish on the other replica entirely.
        post_alert(PUBLISHER_HOST, 7, {"frame_id": 99, "alert_msg": "cross-replica"})

        got7, got8 = await asyncio.gather(recv(v7), recv(v8, 2.0))

        a = got7 is not None and got7.get("frame_id") == 99
        print(("PASS  " if a else "FAIL  ") +
              f"replica-2 viewer got alert published to replica-1   [got={got7}]")
        ok &= a

        b = got8 is None
        print(("PASS  " if b else "FAIL  ") +
              f"isolation holds across replicas   [leaked={got8}]")
        ok &= b

    print("=" * 60)
    print("cross-replica fan-out OK" if ok else "cross-replica fan-out BROKEN")
    return 0 if ok else 1


raise SystemExit(asyncio.run(main()))

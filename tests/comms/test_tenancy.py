"""
ws-server tenancy, auth and Redis fan-out.

The load-bearing assertion is #5: an alert published for flight 1 must reach flight
1's viewer and NOT flight 2's. Before this work ws-server broadcast every alert —
including its JPEG and position — to every connected client.

Needs a live ws-server + Redis. See README.md.
"""
import asyncio
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import jwt
import websockets

SECRET = os.environ["SESSION_JWT_SECRET"]
WS = os.environ.get("WS_URL", "ws://ws-server:8765")
API = os.environ.get("API_URL", "http://ws-server:8000")

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""))


def token(flight_id, scope="view", ttl=300, secret=SECRET):
    """Mint what db-writer would mint. The scope claim is what separates the two kinds."""
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"flight_id": flight_id, "scope": scope, "sub": "1", "iat": now,
         "exp": now + timedelta(seconds=ttl)},
        secret, algorithm="HS256")


def post_alert(flight_id, body, bearer):
    req = urllib.request.Request(
        f"{API}/session/{flight_id}/alert",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {bearer}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


async def recv(ws, timeout=3.0):
    try:
        return json.loads(await asyncio.wait_for(ws.recv(), timeout))
    except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
        return None


async def expect_rejected(name, url):
    try:
        async with websockets.connect(url) as ws:
            await recv(ws, 2.0)
            check(name, ws.close_code == 4401, f"close={ws.close_code}")
    except websockets.exceptions.ConnectionClosedError as e:
        check(name, e.code == 4401, f"close={e.code}")


async def main():
    # 1-3. Viewers without a valid, unexpired, correctly-signed token are refused.
    await expect_rejected("viewer without token rejected", f"{WS}/")
    await expect_rejected("forged token rejected",
                          f"{WS}/?token={token(1, secret='not-the-secret')}")
    await expect_rejected("expired token rejected", f"{WS}/?token={token(1, ttl=-10)}")

    # 4. A viewer token must not work as a publisher token — both are signed with the
    #    same secret, so only the scope claim stops a viewer injecting alerts.
    check("publish with viewer-scoped token -> 401",
          post_alert(1, {"frame_id": 0}, token(1, scope="view")) == 401)
    check("publish without bearer -> 401", post_alert(1, {"frame_id": 0}, "") == 401)
    check("publish with garbage bearer -> 401", post_alert(1, {"frame_id": 0}, "wrong") == 401)

    # 5. A publisher token names one flight and cannot be replayed against another.
    check("publisher token for flight 1 rejected on flight 2 -> 401",
          post_alert(2, {"frame_id": 0}, token(1, scope="publish")) == 401)

    # 6. THE ISOLATION TEST — two tenants, one alert.
    async with websockets.connect(f"{WS}/?token={token(1)}") as a, \
               websockets.connect(f"{WS}/?token={token(2)}") as b:
        await asyncio.sleep(0.5)   # let both subscriptions land

        status = post_alert(1, {"frame_id": 42, "alert_msg": "flight-1-secret"},
                            token(1, scope="publish"))
        check("publish with valid publisher token -> 200", status == 200, f"status={status}")

        got_a, got_b = await asyncio.gather(recv(a), recv(b, 2.0))
        check("flight 1 viewer receives its alert",
              got_a is not None and got_a.get("frame_id") == 42, f"got={got_a}")
        check("flight 2 viewer receives NOTHING (isolation)", got_b is None, f"leaked={got_b}")

    # 7. No replay: a viewer joining after the fact starts blank, by design.
    async with websockets.connect(f"{WS}/?token={token(1)}") as late:
        got = await recv(late, 2.0)
        check("late joiner gets no replayed alert", got is None, f"replayed={got}")

    print("\n" + "=" * 60)
    failed = [n for n, ok, _ in results if not ok]
    print(f"{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
    return 1 if failed else 0


raise SystemExit(asyncio.run(main()))

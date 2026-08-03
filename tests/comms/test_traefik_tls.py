"""
The ingress tier, exercised through Traefik rather than around it.

Driven by run_traefik_tls.sh, which stands up a real portal, db-writer, ws-server,
MediaMTX and PostgreSQL behind a real Traefik holding a real (locally issued)
certificate. Every URL here goes through the proxy on the port a viewer would
dial; nothing in this file talks to an upstream directly except where it needs the
source of truth, which is db-writer.

What this is for. Two claims in CLOUD_ARCHITECTURE.md were untestable until
Traefik existed:

  §8  "the portal has no working configuration today" — COOKIE_SECURE defaults on,
      a Secure cookie is not returned over http://, and there was no terminator.
      So the portal either forgot every login or ran with the local-HTTP
      affordance enabled. Asserted here over real TLS, with the cookie coming back.

  §4  the rate limiter counts the right address. Traefik is now the peer address
      the portal sees, so PORTAL_TRUSTED_PROXY_HOPS became load-bearing the moment
      the proxy landed: at 0 every client on the internet shares one bucket and a
      single attacker locks everybody out. Asserted by forging X-Forwarded-For
      from the client and watching the limit hold anyway.

What this does NOT cover: whether video plays. HLS and WHEP are asserted to reach
MediaMTX's auth hook through the proxy — which is what proves the routing — but
no stream is published, so there is nothing to decode. run_orchestrator_real_app.sh
and run_portal.sh own that.
"""
import asyncio
import http.cookiejar
import json
import os
import secrets
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import jwt
import websockets

PORTAL = os.environ.get("PORTAL", "https://portal.agrarian.local")
WSS = os.environ.get("WSS", "wss://ws.agrarian.local:8765")
HLS = os.environ.get("HLS", "https://media.agrarian.local:8888")
WHEP = os.environ.get("WHEP", "https://media.agrarian.local:8889")
# The two internal ports §8 says must never be routed from outside. They are
# reached here over the test network directly, which is the point: if they were
# reachable through Traefik this file would have found them on a port above.
DBW = os.environ.get("DBW", "http://tt-dbw:8000")
WS_API = os.environ.get("WS_API", "http://tt-ws:8000")
CA = os.environ.get("CA", "/certs/ca.crt")
SECRET = os.environ["SESSION_JWT_SECRET"]

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""))


# The CA this stack was issued by, and nothing else. Deliberately not the system
# trust store: verifying against that would pass for a certificate somebody else
# signed, which is the one thing a TLS test must not do.
_ctx = ssl.create_default_context(cafile=CA)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A browser follows redirects; this test asserts on them instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(
    NoRedirect, urllib.request.HTTPSHandler(context=_ctx))


class Response:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def set_cookie(self):
        return self.headers.get("Set-Cookie") or ""

    def json(self):
        try:
            return json.loads(self.body)
        except ValueError:
            return {}


def request(url, method="GET", data=None, cookie=None, origin=None, headers=None,
            opener=None):
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    if data:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    if cookie:
        req.add_header("Cookie", cookie)
    if origin:
        req.add_header("Origin", origin)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with (opener or _opener).open(req, timeout=20) as r:
            return Response(r.status, dict(r.headers), r.read().decode(errors="replace"))
    except urllib.error.HTTPError as e:
        return Response(e.code, dict(e.headers), e.read().decode(errors="replace"))


def session_cookie(resp):
    raw = resp.set_cookie
    if "agrarian_session=" not in raw:
        return None
    return "agrarian_session=" + raw.split("agrarian_session=", 1)[1].split(";", 1)[0]


# ── 1. TLS is really terminated, and really by our certificate ────────────────

r = request(f"{PORTAL}/login")
check("portal answers over HTTPS through Traefik", r.status == 200, f"status={r.status}")
check("and it is the portal's own login page", "password" in r.body.lower())

# The same request with the system trust store instead of our CA. It must fail:
# a certificate that validates against the public roots is not the one this script
# issued, and a test that passed either way would be asserting nothing.
_public_only = urllib.request.build_opener(
    NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context()))
try:
    request(f"{PORTAL}/login", opener=_public_only)
    check("a client without our CA refuses the connection", False, "it succeeded")
except urllib.error.URLError as e:
    check("a client without our CA refuses the connection",
          "CERTIFICATE_VERIFY_FAILED" in str(e.reason), str(e.reason)[:60])

# ── 2. The Secure cookie, which is the whole §8 gap ───────────────────────────

email = f"tls-{secrets.token_hex(4)}@example.com"
password = "correct-horse-battery"

r = request(f"{PORTAL}/register", "POST",
            {"email": email, "password": password}, origin=PORTAL)
check("register over TLS redirects to the dashboard",
      r.status == 303, f"status={r.status}")

cookie = session_cookie(r)
check("registration sets the session cookie", cookie is not None)

attrs = r.set_cookie.lower()
check("the cookie is Secure", "secure" in attrs, r.set_cookie[:90])
check("the cookie is HttpOnly", "httponly" in attrs)
check("the cookie is SameSite=strict", "samesite=strict" in attrs)

# The assertion §8 was waiting for. Over http:// a browser would not return this
# cookie at all, which is what "no working configuration" meant: the login
# appeared to succeed and the next page did not know who you were.
r = request(f"{PORTAL}/", cookie=cookie)
check("the Secure cookie is accepted on the next request", r.status == 200,
      f"status={r.status}")
check("and the dashboard renders the account's own page",
      "stream" in r.body.lower() or "slot" in r.body.lower())

# Unchanged by the proxy, and worth re-pinning here because the proxy is a new
# place a token could be leaked into a header or a body.
token_value = cookie.split("=", 1)[1]
check("the rendered HTML still never contains the session token",
      token_value not in r.body)

# Logging in again over TLS, since register and login are different paths.
r = request(f"{PORTAL}/login", "POST",
            {"email": email, "password": password}, origin=PORTAL)
check("login over TLS returns a session", r.status == 303 and session_cookie(r),
      f"status={r.status}")

# ── 3. The cross-site check still works behind the proxy ─────────────────────
#
# Traefik preserves the Host header (passHostHeader), so the portal compares the
# Origin against the name the browser used. A proxy that rewrote Host would break
# this in a way that looks like a working site until somebody tries to POST.

r = request(f"{PORTAL}/streams", "POST", {"label": "through-the-proxy"},
            cookie=cookie, origin=PORTAL)
check("a same-origin POST succeeds through Traefik", r.status in (200, 303),
      f"status={r.status}")

r = request(f"{PORTAL}/streams", "POST", {"label": "evil"},
            cookie=cookie, origin="https://attacker.example")
check("a cross-site POST is still refused through Traefik", r.status == 403,
      f"status={r.status}")

# ── 4. The rate limiter counts the client, not Traefik ───────────────────────
#
# The hop count is now load-bearing and newly so: before Traefik the portal was
# reached directly and 0 was correct. Traefik appends the real peer address to
# X-Forwarded-For, so with one hop trusted the portal reads the RIGHTMOST entry
# and everything the client wrote to the left of it is ignored.
#
# Forging a different address on every attempt is what an attacker does to mint a
# fresh bucket. If it worked, the limit below would never trip.

limited = None
for i in range(12):
    r = request(f"{PORTAL}/login", "POST",
                {"email": email, "password": "wrong-password"},
                origin=PORTAL,
                headers={"X-Forwarded-For": f"203.0.113.{i}"})
    if r.status == 429:
        limited = i
        break

check("a forged X-Forwarded-For does not mint a fresh rate-limit bucket",
      limited is not None, f"tripped after {limited} attempts" if limited
      else "never tripped in 12 attempts")
check("and the 429 carries Retry-After",
      limited is not None and r.headers.get("Retry-After"))

# ── 5. WSS through Traefik ───────────────────────────────────────────────────
#
# The one protocol here that is not plain request/response. Traefik carries the
# Upgrade without configuration, but "without configuration" is exactly the kind
# of claim that is worth one test.


def mint(flight_id, scope="view", ttl=300):
    """
    What db-writer would mint. Minted here rather than fetched because this file
    is testing Traefik's carriage of the WebSocket, not the issuing path — that is
    run_mediamtx_auth.sh's subject, and going through it would need an open flight
    to scope the token to.
    """
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"flight_id": flight_id, "scope": scope, "sub": "1", "iat": now,
         "exp": now + timedelta(seconds=ttl)},
        SECRET, algorithm="HS256")


def post_alert(flight_id, bearer):
    """Straight to ws-server's alert-write API, which is internal-only by §8."""
    req = urllib.request.Request(
        f"{WS_API}/session/{flight_id}/alert",
        data=json.dumps({"message": "through the proxy",
                         "image_b64": "", "width": 0, "height": 0}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {bearer}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


async def wss_checks():
    flight = 7
    try:
        async with websockets.connect(f"{WSS}/?token={mint(flight)}", ssl=_ctx,
                                      open_timeout=20) as ws:
            check("wss:// handshake completes through Traefik", True)

            # An alert raised while this socket is open must arrive over it. The
            # handshake alone would pass even if Traefik half-closed the tunnel
            # after the upgrade, which is the failure worth catching: it looks
            # like a working connection that never delivers anything.
            status = post_alert(flight, mint(flight, scope="publish"))
            check("the alert was accepted by ws-server", status in (200, 202),
                  f"status={status}")
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                check("and it arrives over the TLS-terminated socket",
                      msg.get("message") == "through the proxy", str(msg)[:70])
            except asyncio.TimeoutError:
                check("and it arrives over the TLS-terminated socket", False,
                      "nothing received in 10s")
    except Exception as e:  # noqa: BLE001 - any failure here is the failure
        check("wss:// handshake completes through Traefik", False, repr(e)[:80])

    # TLS changes nothing about authorization: the token is still what decides.
    try:
        async with websockets.connect(f"{WSS}/?token=garbage", ssl=_ctx,
                                      open_timeout=20) as ws:
            # ws-server may accept the socket and close it rather than refusing
            # the upgrade; either is a refusal, receiving alerts is not.
            try:
                await asyncio.wait_for(ws.recv(), timeout=3)
                check("wss:// with a bad token is refused", False, "it stayed open")
            except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed) as e:
                check("wss:// with a bad token is refused",
                      isinstance(e, websockets.exceptions.ConnectionClosed))
    except Exception:  # noqa: BLE001
        check("wss:// with a bad token is refused", True)


asyncio.run(wss_checks())

# ── 6. HLS and WHEP reach MediaMTX's auth hook through Traefik ───────────────
#
# No stream is published, so there is nothing to watch. What is being asserted is
# the routing: a 401 can only come from db-writer's /auth/mediamtx, which means
# the request crossed Traefik, reached MediaMTX, and MediaMTX asked. A proxy
# misconfiguration gives 404 or 502 here instead.
#
# Both of these need driving the way the real client drives them, and getting
# that wrong is how a proxy test passes while the proxy is broken.

# HLS redirects BEFORE it authenticates: the first request answers 302 to
# ?cookieCheck=1 and only the followed request reaches the hook (§4). A client
# that does not follow redirects and keep cookies sees 302 for everything and
# never learns whether it was authorised — so this opener does both, which is
# also the only reason the 401 below means anything.
_hls_opener = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=_ctx),
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

r = request(f"{HLS}/out/00000000-0000-0000-0000-000000000000/index.m3u8",
            opener=_hls_opener)
check("HLS over TLS reaches MediaMTX's authorization", r.status in (401, 403),
      f"status={r.status}")

# WHEP checks the content type before it checks the credential, so a POST without
# application/sdp is refused 400 by MediaMTX itself and never reaches the hook.
# That still proves the routing, but it proves it by accident — assert the path a
# real WHEP client takes instead.
r = request(f"{WHEP}/out/00000000-0000-0000-0000-000000000000/whep", "POST",
            headers={"Content-Type": "application/sdp"})
check("WHEP signalling over TLS reaches MediaMTX's authorization",
      r.status in (401, 403), f"status={r.status}")

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)

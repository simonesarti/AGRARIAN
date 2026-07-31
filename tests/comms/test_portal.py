"""
The portal, exercised the way a browser exercises it.

Form posts, a session cookie, an Origin header, and no direct access to
db-writer except where this test deliberately checks the portal's work against
the source of truth. Driven by run_portal.sh, which supplies two portal replicas
(PORTAL1/PORTAL2) and the db-writer behind them (DBW).

What is NOT covered here: whether video actually plays. That is WebRTC
negotiation between the browser and MediaMTX, and the portal is not in the middle
of it — it composes a URL. The URL's shape is asserted; the picture is not.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

PORTAL1 = os.environ.get("PORTAL1", "http://pt-1:8000")
PORTAL2 = os.environ.get("PORTAL2", "http://pt-2:8000")
DBW = os.environ.get("DBW", "http://pt-dbw:8000")

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A browser follows redirects; this test asserts on them instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(NoRedirect)


class Response:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def location(self):
        return self.headers.get("Location")

    @property
    def set_cookie(self):
        return self.headers.get("Set-Cookie") or ""

    def json(self):
        try:
            return json.loads(self.body)
        except ValueError:
            return {}


def call(base, path, form=None, cookie=None, origin="", method=None, headers=None):
    """
    One request. `origin` defaults to the base being called — the same-origin case,
    which is what a browser sends. Pass origin=None to send none at all.
    """
    url = base + path
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data is not None else "GET"))
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    if origin == "":
        origin = base
    if origin:
        req.add_header("Origin", origin)
    if cookie:
        req.add_header("Cookie", f"agrarian_session={cookie}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)

    try:
        with _opener.open(req, timeout=15) as r:
            return Response(r.status, r.headers, r.read().decode(errors="replace"))
    except urllib.error.HTTPError as e:
        return Response(e.code, e.headers, e.read().decode(errors="replace"))


def post_json(base, path, body=None, token=None, method="POST"):
    """Straight to db-writer, for ground truth the portal is checked against."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except ValueError:
            return e.code, {}


def cookie_value(resp):
    """The session token out of a Set-Cookie header, or None."""
    raw = resp.set_cookie
    if not raw.startswith("agrarian_session="):
        return None
    # An empty value arrives quoted — agrarian_session="" — which is how a
    # deletion looks and must not read as a token.
    value = raw.split(";", 1)[0].split("=", 1)[1].strip('"')
    return value or None


PW = "correct horse"

# ── Nothing works without a session ──────────────────────────────────────────

r = call(PORTAL1, "/health")
check("/health is 200 and needs no session", r.status == 200, str(r.status))

r = call(PORTAL1, "/")
check("the dashboard redirects an anonymous visitor to /login",
      r.status == 303 and r.location == "/login", f"{r.status} {r.location}")

r = call(PORTAL1, "/login")
check("/login renders a sign-in form", r.status == 200 and 'name="password"' in r.body,
      str(r.status))

r = call(PORTAL1, "/watch")
check("/watch redirects an anonymous visitor too", r.status == 303, str(r.status))

r = call(PORTAL1, "/api/viewer-token", form={})
check("the viewer-token API 401s without a cookie, and answers JSON not HTML",
      r.status == 401 and "error" in r.json(), f"{r.status} {r.body[:80]}")

r = call(PORTAL1, "/streams", form={"label": "smuggled"})
check("adding a slot with no cookie redirects to /login rather than creating one",
      r.status == 303 and r.location == "/login", f"{r.status} {r.location}")

# ── Registration ─────────────────────────────────────────────────────────────

r = call(PORTAL1, "/register", form={"email": "alice@test.io", "password": PW})
alice = cookie_value(r)
check("registering redirects to the dashboard", r.status == 303 and r.location == "/",
      f"{r.status} {r.location}")
check("registering sets a session cookie", bool(alice), r.set_cookie[:60])

raw = r.set_cookie
check("the session cookie is HttpOnly — page JS cannot read it",
      "httponly" in raw.lower(), raw)
check("the session cookie is SameSite=strict — a cross-site POST carries no session",
      "samesite=strict" in raw.lower(), raw)
check("the session cookie is Secure by default", "secure" in raw.lower(), raw)
check("the session cookie is scoped to the whole site", "path=/" in raw.lower(), raw)

r = call(PORTAL1, "/register", form={"email": "alice@test.io", "password": PW})
check("a duplicate registration is refused with db-writer's own 409",
      r.status == 409 and "already registered" in r.body.lower(), str(r.status))

r = call(PORTAL1, "/register", form={"email": "weak@test.io", "password": "short"})
check("a too-short password is refused and says why",
      r.status == 400 and "8 characters" in r.body, f"{r.status} {r.body[:120]}")

r = call(PORTAL1, "/register", form={"email": "notanemail", "password": PW})
check("a malformed email is refused", r.status == 400, str(r.status))
check("...and no cookie is set by a failed registration", cookie_value(r) is None)

# ── Login ────────────────────────────────────────────────────────────────────

r = call(PORTAL1, "/login", form={"email": "alice@test.io", "password": "wrong"})
check("a wrong password re-renders the form, it does not redirect-loop",
      r.status == 401 and 'name="password"' in r.body, str(r.status))
check("a failed login sets no session cookie", cookie_value(r) is None, r.set_cookie[:60])
check("the failure does not say whether the account exists",
      "invalid email or password" in r.body.lower(), r.body[:200])

r = call(PORTAL1, "/login", form={"email": "nobody@test.io", "password": PW})
check("an unknown address gets the same message as a wrong password",
      r.status == 401 and "invalid email or password" in r.body.lower(), str(r.status))

r = call(PORTAL1, "/login", form={"email": "ALICE@test.io", "password": PW})
alice = cookie_value(r)
check("signing in works in any casing and returns a session",
      r.status == 303 and bool(alice), f"{r.status} {r.set_cookie[:40]}")

r = call(PORTAL1, "/login", cookie=alice)
check("an already-signed-in visitor is sent on from /login", r.status == 303, str(r.status))

# ── The dashboard, and what it must never contain ────────────────────────────

r = call(PORTAL1, "/", cookie=alice)
check("the dashboard renders for a signed-in user", r.status == 200, str(r.status))
check("it names the signed-in account", "alice@test.io" in r.body, r.body[:200])
check("a new account is told it has no slots", "No slots yet" in r.body)

check("the session token is never written into the page — the cookie is the only copy",
      alice not in r.body, "token found in dashboard HTML")

# ── The stateless property: the cookie is the whole session ──────────────────

r = call(PORTAL2, "/", cookie=alice)
check("a cookie issued by replica 1 is accepted by replica 2 — no server-side session",
      r.status == 200 and "alice@test.io" in r.body, str(r.status))

r = call(PORTAL2, "/", cookie="garbage")
check("a forged cookie is bounced to /login, not accepted",
      r.status == 303 and r.location == "/login?expired=1", f"{r.status} {r.location}")
check("...and the bad cookie is cleared on the way out",
      "agrarian_session=" in r.set_cookie and cookie_value(r) is None, r.set_cookie[:80])

# ── Cross-site request forgery ───────────────────────────────────────────────

r = call(PORTAL1, "/streams", form={"label": "from evil"}, cookie=alice,
         origin="http://evil.example")
check("a cross-origin slot creation is refused", r.status == 403, str(r.status))

r = call(PORTAL1, "/streams", form={"label": "no origin"}, cookie=alice, origin=None)
check("a state-changing POST with no Origin or Referer is refused", r.status == 403, str(r.status))

r = call(PORTAL1, "/logout", form={}, cookie=alice, origin="http://evil.example")
check("a cross-origin sign-out is refused too", r.status == 403, str(r.status))

r = call(PORTAL1, "/login", form={"email": "alice@test.io", "password": PW},
         origin="http://evil.example")
check("a cross-origin login is refused before the password reaches db-writer",
      r.status == 403, str(r.status))

r = call(PORTAL1, "/streams", form={"label": "from referer"}, cookie=alice,
         origin=None, headers={"Referer": PORTAL1 + "/"})
check("a same-site Referer is accepted when Origin is absent", r.status == 303, str(r.status))

r = call(PORTAL1, "/", cookie=alice)
check("neither refused request created anything, and the accepted one did",
      r.body.count("rtmps://") == 1 and "from evil" not in r.body and "no origin" not in r.body)

# ── Slots ────────────────────────────────────────────────────────────────────

r = call(PORTAL1, "/streams", form={"label": "north field"}, cookie=alice)
check("adding a slot redirects back to the dashboard",
      r.status == 303 and r.location == "/", f"{r.status} {r.location}")

# Ground truth: the cookie IS the session token, so it can be spent at db-writer
# directly. Everything the page shows is checked against this.
st, body = post_json(DBW, "/streams", token=alice, method="GET")
slots = body["streams"]
check("db-writer holds the two slots the portal created", st == 200 and len(slots) == 2, str(st))

north = [s for s in slots if s["label"] == "north field"][0]
r = call(PORTAL1, "/", cookie=alice)
check("the dashboard shows the slot's label", "<strong>north field</strong>" in r.body)
check("it shows a full ingest URL with the real key, ready to retype",
      f"rtmps://media.test.local:1936/in/{north['stream_key']}" in r.body,
      north["stream_key"])
check("an idle slot offers no Watch button", "/watch?stream_id=" not in r.body)

# A label is user input rendered into HTML. Jinja autoescaping is what stands
# between that and script execution in another tab of the same account.
r = call(PORTAL1, "/streams", form={"label": "<script>alert(1)</script>"}, cookie=alice)
r = call(PORTAL1, "/", cookie=alice)
check("a label containing markup is escaped, not executed",
      "<script>alert(1)</script>" not in r.body and "&lt;script&gt;" in r.body)

st, _ = post_json(DBW, f"/streams/{north['stream_id']}/revoke", token=alice)
r = call(PORTAL1, "/streams", form={"label": "west field"}, cookie=alice)
check("a slot retired underneath the portal does not break the next add", r.status == 303, str(r.status))

# ── Rotate and retire, through the pages ─────────────────────────────────────

st, body = post_json(DBW, "/streams", token=alice, method="GET")
target = body["streams"][0]
old_key = target["stream_key"]

r = call(PORTAL1, f"/streams/{target['stream_id']}/rotate", form={}, cookie=alice)
check("New key redirects back to the dashboard", r.status == 303, str(r.status))

r = call(PORTAL1, "/", cookie=alice)
check("the old key is gone from the page the moment it is rotated", old_key not in r.body)
st, body = post_json(DBW, "/streams", token=alice, method="GET")
new_key = [s for s in body["streams"] if s["stream_id"] == target["stream_id"]][0]["stream_key"]
check("and the new one is shown instead", new_key in r.body and new_key != old_key)

r = call(PORTAL1, f"/streams/{target['stream_id']}/revoke", form={}, cookie=alice)
check("Retire redirects back to the dashboard", r.status == 303, str(r.status))
r = call(PORTAL1, "/", cookie=alice)
check("a retired slot disappears from the dashboard", new_key not in r.body)

st, body = post_json(DBW, "/streams?include_revoked=true", token=alice, method="GET")
check("...but nothing was deleted — the row survives with revoked_at set",
      any(s["stream_id"] == target["stream_id"] and s["revoked_at"] for s in body["streams"]),
      str(body)[:200])

# ── Another tenant ───────────────────────────────────────────────────────────

st, body = post_json(DBW, "/streams", token=alice, method="GET")
alice_slot = body["streams"][0]
alice_key = alice_slot["stream_key"]

r = call(PORTAL2, "/register", form={"email": "mallory@test.io", "password": PW})
mallory = cookie_value(r)
check("a second account registers on the other replica", r.status == 303 and bool(mallory))

# Checked on the key, not the label: the add-slot placeholder puts a label-like
# string on every dashboard, so a label proves nothing. A key is the credential.
r = call(PORTAL1, "/", cookie=mallory)
check("their dashboard shows their own slots, not alice's",
      "No slots yet" in r.body and alice_key not in r.body, str(r.status))

r = call(PORTAL1, f"/streams/{alice_slot['stream_id']}/rotate", form={}, cookie=mallory)
check("another tenant rotating alice's slot gets a 404, not a new key", r.status == 404, str(r.status))

st, body = post_json(DBW, "/streams", token=alice, method="GET")
check("alice's key is untouched by the attempt",
      body["streams"][0]["stream_key"] == alice_slot["stream_key"])

# ── Watching ─────────────────────────────────────────────────────────────────

r = call(PORTAL1, "/watch", cookie=alice)
check("the watch page renders for a signed-in user", r.status == 200, str(r.status))
check("it carries no credential of its own — the token is fetched by script",
      alice not in r.body and "/static/watch.js" in r.body)

r = call(PORTAL1, "/api/viewer-token", form={}, cookie=alice)
check("asking for a viewer token with nothing flying is a plain 404",
      r.status == 404 and "error" in r.json(), f"{r.status} {r.body[:80]}")

# Open a flight the way the orchestrator does, so there is something to watch.
st, flight = post_json(DBW, "/flight/open", {"stream_key": alice_slot["stream_key"]})
check("a flight opens for alice's key", st == 200 and "public_uuid" in flight, str(st))

r = call(PORTAL1, "/", cookie=alice)
check("the dashboard now marks that slot live and offers a Watch button",
      ">Live<" in r.body and f"/watch?stream_id={alice_slot['stream_id']}" in r.body)

r = call(PORTAL1, "/api/viewer-token", form={},
         cookie=alice, headers={"Content-Type": "application/json"})
info = r.json()
check("the viewer-token API answers for the one live flight",
      r.status == 200 and info.get("flight_id") == flight["flight_id"], f"{r.status} {r.body[:120]}")
check("the video URL points at MediaMTX's public host and the flight's own path",
      info.get("webrtc_url", "").startswith(
          f"https://media.test.local:8889/out/{flight['public_uuid']}/?jwt="),
      info.get("webrtc_url", "")[:120])
check("an HLS URL is offered as the fallback for browsers that cannot do WebRTC",
      f"https://media.test.local:8888/out/{flight['public_uuid']}/index.m3u8?jwt="
      in info.get("hls_url", ""), info.get("hls_url", "")[:120])
check("the alert stream points at ws-server over WSS, on its own host",
      info.get("ws_url", "").startswith("wss://ws.test.local:8765/?token="),
      info.get("ws_url", "")[:80])

viewer = info.get("webrtc_url", "").split("jwt=")[-1]
check("what reaches the page is NOT the session token — it is a downgrade",
      viewer and viewer != alice, "the page was handed the account credential")
check("the same token is used for video and for alerts (one flight, one credential)",
      info.get("ws_url", "").endswith(viewer))

st, _ = post_json(DBW, "/me", token=viewer, method="GET")
check("and the downgraded token cannot act as a session — no path back", st == 401, str(st))

st, _ = post_json(DBW, "/viewer/token", {}, token=viewer)
check("nor can it mint another viewer token for itself", st == 401, str(st))

# Mallory holds a valid session, just not this flight's.
r = call(PORTAL1, "/api/viewer-token", form={}, cookie=mallory)
check("another tenant asking for a token gets nothing to watch", r.status == 404, str(r.status))

r = call(PORTAL1, f"/api/viewer-token?stream_id={alice_slot['stream_id']}", form={}, cookie=mallory)
check("naming alice's stream explicitly does not help them", r.status == 404, str(r.status))

# ── Signing out ──────────────────────────────────────────────────────────────

r = call(PORTAL1, "/logout", form={}, cookie=alice)
check("signing out redirects to /login", r.status == 303 and r.location == "/login",
      f"{r.status} {r.location}")
check("...and clears the cookie", "agrarian_session=" in r.set_cookie and cookie_value(r) is None,
      r.set_cookie[:80])

print()
print("=" * 60)
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)

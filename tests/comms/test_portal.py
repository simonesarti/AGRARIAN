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
import base64
import json
import os
import sys
import time
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


def get_raw(base, path, cookie=None):
    """
    One GET, body kept as bytes. Returns (status, headers, body).

    Separate from call() because that one decodes with errors="replace", which
    would silently corrupt exactly the thing an image response is asserted on.
    """
    req = urllib.request.Request(base + path, method="GET")
    if cookie:
        req.add_header("Cookie", f"agrarian_session={cookie}")
    try:
        with _opener.open(req, timeout=15) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


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
check("the video URL is MediaMTX's WHEP endpoint on the flight's own path",
      info.get("whep_url", "").startswith(
          f"https://media.test.local:8889/out/{flight['public_uuid']}/whep?jwt="),
      info.get("whep_url", "")[:120])
# Regression pin. This used to point at MediaMTX's built-in reader page at
# /<path>/, which answers 401 and never reaches the auth hook at all -- it is
# gated behind the internal user roster that authMethod: http replaced, so no
# credential could open it. Nothing may quietly go back to serving that URL.
check("and it is NOT the built-in reader page, which no credential can open",
      "/whep?jwt=" in info.get("whep_url", "")
      and not info.get("whep_url", "").endswith(f"/out/{flight['public_uuid']}/"),
      info.get("whep_url", "")[:120])
check("an HLS URL is offered as the fallback for browsers that cannot do WebRTC",
      f"https://media.test.local:8888/out/{flight['public_uuid']}/index.m3u8?jwt="
      in info.get("hls_url", ""), info.get("hls_url", "")[:120])
check("the alert stream points at ws-server over WSS, on its own host",
      info.get("ws_url", "").startswith("wss://ws.test.local:8765/?token="),
      info.get("ws_url", "")[:80])

viewer = info.get("whep_url", "").split("jwt=")[-1]
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

# ── Flight history ───────────────────────────────────────────────────────────
#
# Built on the flight opened just above: alerts and a recording are written the
# way the app and the recorder write them, the flight is closed the way the
# orchestrator closes it, and then the pages are read the way a browser reads
# them. Nothing here reaches into the database directly except for ground truth.

FID = flight["flight_id"]
PUB = flight["publisher_token"]

# Bytes chosen to be recognisable rather than valid: what is being asserted is
# that exactly what was stored comes back out, through two services.
CROP = b"\xff\xd8\xff\xe0 not a real jpeg, but exactly these bytes \x00\x01\x02"

for i in range(3):
    st, _ = post_json(DBW, f"/session/{FID}/alert", {
        "frame_id": 100 + i,
        "alert_msg": f"person in field {i}",
        "timestamp": float(i),
        "datetime": f"2026-07-01T09:00:0{i}",
        # Only the first carries a crop, so has_image has both answers to give.
        "image_data": base64.b64encode(CROP).decode() if i == 0 else None,
        "image_width": 320, "image_height": 240,
    }, token=PUB)
check("three alerts are accepted for the live flight", st == 200, str(st))

st, _ = post_json(DBW, "/recording", {
    "public_uuid": flight["public_uuid"],
    "segment_path": "/recordings/out/seg-0.mp4",
    "storage_backend": "azure",
    "storage_location": "agrarian/seg-0.mp4",
})
check("the recorder logs a segment against the flight", st == 200, str(st))

st, _ = post_json(DBW, f"/flight/{FID}/close", token=PUB)
check("the orchestrator closes the flight", st == 200, str(st))

# Alerts are written by a background thread, so the count is eventually right
# rather than immediately right. Poll rather than sleep: a fixed sleep is either
# slower than it needs to be or flaky, and usually both.
history = ""
for _ in range(30):
    history = call(PORTAL1, "/history", cookie=alice).body
    if "<td>3</td>" in history:
        break
    time.sleep(0.5)

check("the history page lists the flight that just landed",
      f"flight {FID}<" in history, history[:200])
check("...with its alerts counted", "<td>3</td>" in history)
check("...and its recording counted", "<td>1</td>" in history)
check("a closed flight shows a duration rather than an Open badge",
      ">Open<" not in history, "the landed flight is still shown as open")
check("the history page offers a way into the flight",
      f'href="/flights/{FID}"' in history)

r = call(PORTAL1, "/", cookie=alice)
check("the dashboard links to the history, and to one slot's history",
      'href="/history"' in r.body and f'href="/history?stream_id={alice_slot["stream_id"]}"' in r.body)

r = call(PORTAL1, f"/history?stream_id={alice_slot['stream_id']}", cookie=alice)
check("filtering the history to one slot still shows that slot's flight",
      r.status == 200 and f"flight {FID}<" in r.body, str(r.status))

# ── One flight's page ────────────────────────────────────────────────────────

r = call(PORTAL1, f"/flights/{FID}", cookie=alice)
check("the flight page renders", r.status == 200, str(r.status))
check("it shows the alerts that were raised",
      all(f"person in field {i}" in r.body for i in range(3)), r.body[:200])
check("it shows where the recording was archived",
      "agrarian/seg-0.mp4" in r.body and "azure" in r.body)
check("the crop is a lazily-fetched resource, not bytes inlined into the page",
      'loading="lazy"' in r.body and "data:image/jpeg;base64," not in r.body)

# The alert_id is not something the test knows; it comes from the page, which is
# also the only place a browser would learn it.
img_src = r.body.split(f'src="/flights/{FID}/alerts/')[1].split('"')[0]
check("exactly one of the three alerts offers an image — the two without a crop do not",
      r.body.count(f'src="/flights/{FID}/alerts/') == 1, str(r.body.count("<img")))
check("...and the image is sized from what was stored, so the page does not reflow",
      'width="320"' in r.body and 'height="240"' in r.body)

status, headers, body = get_raw(PORTAL1, f"/flights/{FID}/alerts/{img_src}", cookie=alice)
check("the crop comes back through the portal, byte for byte", body == CROP,
      f"{status} {len(body)} bytes")
check("...as an image, not as JSON",
      (headers.get("Content-Type") or "") == "image/jpeg", headers.get("Content-Type"))
check("...marked private, so no shared cache keeps a tenant's photograph",
      "private" in (headers.get("Cache-Control") or ""), headers.get("Cache-Control"))

# ── Another tenant, again ────────────────────────────────────────────────────

r = call(PORTAL1, f"/flights/{FID}", cookie=mallory)
check("another tenant opening the flight page gets a 404", r.status == 404, str(r.status))

status, _, body = get_raw(PORTAL1, f"/flights/{FID}/alerts/{img_src}", cookie=mallory)
check("...and cannot fetch the crop either", status == 404 and body != CROP, str(status))

status, _, _ = get_raw(PORTAL1, f"/flights/{FID}/alerts/{img_src}", cookie=None)
check("nor can an anonymous visitor with the exact URL",
      status in (303, 401, 404), str(status))

r = call(PORTAL2, "/history", cookie=mallory)
check("their own history is empty, not alice's", r.status == 200 and "No flights yet" in r.body,
      str(r.status))

# ── Paging ───────────────────────────────────────────────────────────────────
#
# Enough flights to overflow one page. Each is opened and closed the way the
# orchestrator would, so these are ordinary rows and not a fixture.

opened = []
for _ in range(21):
    st, f = post_json(DBW, "/flight/open", {"stream_key": alice_slot["stream_key"]})
    opened.append(f["flight_id"])
    post_json(DBW, f"/flight/{f['flight_id']}/close", token=f["publisher_token"])
check("twenty-one more flights fly and land", len(opened) == 21 and st == 200, str(st))

r = call(PORTAL1, "/history", cookie=alice)
check("the newest flight is on the first page", f"flight {opened[-1]}<" in r.body)
check("...and the oldest is not — the page is bounded",
      f"flight {FID}<" not in r.body, "the whole history came back in one page")
check("a cursor onward is offered", "/history?before=" in r.body, "no Older link on a full page")

cursor = r.body.split("/history?before=")[1].split('"')[0]
older = call(PORTAL1, f"/history?before={cursor}", cookie=alice)
check("following it reaches the older flights", older.status == 200 and f"flight {FID}<" in older.body,
      str(older.status))
check("...and repeats none of the newer ones",
      not any(f"flight {i}<" in older.body for i in opened[-5:]),
      "a row appeared on both pages")
check("the older page offers a way back to the newest",
      'href="/history"' in older.body)

# ── Rate limiting ────────────────────────────────────────────────────────────
#
# Last in the file, because exhausting a bucket is not undoable inside the
# window. The runner starts pt-1 trusting no proxy and pt-2 trusting one, so a
# scenario can claim a clean bucket by presenting an X-Forwarded-For to pt-2 —
# and the fact that pt-1 ignores exactly the same header is itself asserted
# below, since that is the difference between a rate limiter and a rate
# suggester.


def as_ip(ip):
    return {"X-Forwarded-For": ip}


def fail_login(base, email, ip=None):
    return call(base, "/login", form={"email": email, "password": "wrong"},
                headers=as_ip(ip) if ip else None)


# Dedicated accounts on their own registration bucket, so the scenarios below
# start from a known count rather than inheriting one.
made = [call(PORTAL2, "/register", form={"email": f"{n}@test.io", "password": PW},
             headers=as_ip("203.0.113.99")).status for n in ("ratelimit-a", "ratelimit-b")]
check("two accounts for the rate-limit scenarios register cleanly", made == [303, 303], str(made))

# ── the per-account limit: one account tried from anywhere ───────────────────
codes = [fail_login(PORTAL2, "ratelimit-a@test.io", "203.0.113.10").status for _ in range(5)]
check("the first five failures for an account are answered normally",
      codes == [401] * 5, str(codes))

r = fail_login(PORTAL2, "ratelimit-a@test.io", "203.0.113.10")
check("the sixth is refused with 429, not another password check", r.status == 429, str(r.status))
check("...with a Retry-After a client can obey",
      r.headers.get("Retry-After", "0").isdigit() and int(r.headers["Retry-After"]) > 0,
      r.headers.get("Retry-After"))
check("...and a wait a person can read", "Try again in about" in r.body)

# The account bucket must be the account's, not the address's — otherwise one
# locked-out account would take every other account on that NAT down with it.
r = fail_login(PORTAL2, "someone-else@test.io", "203.0.113.10")
check("a different account from the same address still gets its attempt",
      r.status == 401, str(r.status))

# ── a success clears it ──────────────────────────────────────────────────────
codes = [fail_login(PORTAL2, "ratelimit-b@test.io", "203.0.113.11").status for _ in range(4)]
r = call(PORTAL2, "/login", form={"email": "ratelimit-b@test.io", "password": PW},
         headers=as_ip("203.0.113.11"))
check("signing in correctly succeeds with failures already on the counter",
      codes == [401] * 4 and r.status == 303, f"{codes} {r.status}")

codes = [fail_login(PORTAL2, "ratelimit-b@test.io", "203.0.113.11").status for _ in range(5)]
check("a success clears the account's counter — the next five failures are not refused",
      codes == [401] * 5, str(codes))

# ── the per-address limit: many accounts tried from one source ───────────────
codes = [fail_login(PORTAL2, f"spray{i}@test.io", "203.0.113.12").status for i in range(12)]
r = fail_login(PORTAL2, "spray-last@test.io", "203.0.113.12")
check("spraying twelve different accounts from one address is allowed to the limit",
      codes == [401] * 12, str(codes))
check("...and then refused, even though no single account was tried twice",
      r.status == 429, str(r.status))

r = fail_login(PORTAL2, "spray0@test.io", "203.0.113.13")
check("another address is unaffected — the limit is per source, not global",
      r.status == 401, str(r.status))

# ── the counters are shared, so replicas cannot be dealt around ──────────────
# No X-Forwarded-For here: both replicas then count against the peer address,
# which is the same for both, so this is the cross-replica property and nothing
# else. Filled by loop rather than by arithmetic because earlier sections of
# this file already put a couple of failures in this bucket.
attempts, refused = 0, False
for i in range(20):
    attempts += 1
    if fail_login(PORTAL1, f"shared{i}@test.io").status == 429:
        refused = True
        break
check("one replica eventually refuses on its own", refused, f"after {attempts} attempts")

r = fail_login(PORTAL2, "shared-elsewhere@test.io")
check("the OTHER replica refuses immediately — the counters are in Redis, not memory",
      r.status == 429, f"{r.status} after {attempts} on replica 1")

# ── a forged X-Forwarded-For buys nothing where no proxy is trusted ──────────
# pt-1 runs with TRUSTED_PROXY_HOPS=0. Its bucket is exhausted; a client that
# invents a header must not thereby invent a fresh bucket.
r = fail_login(PORTAL1, "forged@test.io", "198.51.100.7")
check("a forged X-Forwarded-For does not mint a clean bucket where none is trusted",
      r.status == 429, str(r.status))

# The same header on the replica that IS behind a proxy is believed — which is
# the whole reason the setting exists, and why setting it wrongly is dangerous.
r = fail_login(PORTAL2, "forged@test.io", "198.51.100.7")
check("the same header is believed by the replica configured to trust one hop",
      r.status == 401, str(r.status))

# ── registration counts every attempt, not every account ─────────────────────
# All eight are malformed, so nothing is created and no password is hashed. An
# attempt that fails still tells the caller whether an address is taken, so it
# is the attempts that have to be bounded.
codes = [call(PORTAL2, "/register", form={"email": f"nope{i}", "password": PW},
              headers=as_ip("203.0.113.20")).status for i in range(8)]
r = call(PORTAL2, "/register", form={"email": "alice@test.io", "password": PW},
         headers=as_ip("203.0.113.20"))
check("eight registration attempts that all failed are still counted",
      codes == [400] * 8, str(codes))
check("...and the ninth is refused before db-writer is asked anything",
      r.status == 429, str(r.status))

r = call(PORTAL2, "/register", form={"email": "fresh@test.io", "password": PW},
         headers=as_ip("203.0.113.21"))
check("registration from another address is unaffected", r.status == 303, str(r.status))

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

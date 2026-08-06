"""
portal — the pages a human clicks.

The only new service on the public internet (§8). Everything it knows it learns
from db-writer over the private network; the browser never crosses that hop, which
is what keeps db-writer's user-facing routes off the public side.

Holds no state
--------------
The session is a signed JWT in an httpOnly cookie, minted by db-writer and
validated by db-writer. This process keeps nothing between requests, so any
replica can serve any request — the property an in-memory session store would
quietly destroy, exactly as an in-memory client set once did in ws-server.

Holds no secret either
----------------------
SESSION_JWT_SECRET is deliberately absent here (§7). The portal cannot tell a
valid token from a forged one and does not try: it forwards the cookie's value
and treats db-writer's 401 as the answer. That costs one hop and keeps the key
that signs every credential in the system out of the tier facing the internet.

Three credentials appear in this file and only one of them is ever held:

  session token   in the cookie, httpOnly    — the account. Never reaches page JS
  viewer token    handed to page JS          — one flight, read-only, short-lived
  stream key      rendered into the page     — the operator has to retype it

The middle one is a downgrade of the first (§3), which is why it is safe to put
in a URL and the first is not.
"""

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from constants import (
    API_HOST,
    API_PORT,
    HLS_PORT,
    APP_MODE_CHOICES,
    INGEST_PATH_PREFIX,
    LOGIN_MAX_FAILURES_PER_ACCOUNT,
    LOGIN_MAX_FAILURES_PER_IP,
    RATE_LIMIT_WINDOW_S,
    REGISTER_MAX_PER_IP,
    REGISTER_WINDOW_S,
    RTMPS_PORT,
    SESSION_COOKIE,
    SESSION_COOKIE_MAX_AGE_S,
    WEBRTC_PORT,
    WS_PORT,
)
from db_writer_client import (
    DbWriterClient,
    DbWriterUnavailable,
    SessionExpired,
    UpstreamRejected,
)
from rate_limit import RateLimiter, account_key, client_ip, ip_key, retry_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("portal")

# ── Configuration ─────────────────────────────────────────────────────────────

_DB_WRITER_URL = os.environ.get("DB_WRITER_URL", "http://db-writer:8000")

# The hostnames the BROWSER dials, which are not the ones this process dials.
# Video and alerts go straight from the browser to the hub; the portal only
# composes the URLs (§5 — this is why flights store a path, not a URL).
#
# Refused empty rather than defaulted. Compose passes an unset MEDIAMTX_HOST
# through as "", which os.environ[...] would happily accept — and the failure
# would be a page full of `rtmps://:1936/in/<key>` that a user copies into a
# controller before anyone notices the missing host.
_MEDIA_PUBLIC_HOST = os.environ.get("MEDIA_PUBLIC_HOST", "").strip()
if not _MEDIA_PUBLIC_HOST:
    raise RuntimeError(
        "MEDIA_PUBLIC_HOST is required: it is the hostname viewers and drone "
        "operators dial. Set MEDIAMTX_HOST in .env.")

_WS_PUBLIC_HOST = os.environ.get("WS_PUBLIC_HOST", "").strip() or _MEDIA_PUBLIC_HOST
_WS_PUBLIC_PORT = int(os.environ.get("WS_PORT", WS_PORT))

# Both default to on, and both are off only for local HTTP testing. A Secure
# cookie is simply not sent over http://, so leaving this on locally does not
# fail loudly — it fails as a login that appears to work and then forgets you.
_COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() != "false"
_PUBLIC_TLS = os.environ.get("PUBLIC_TLS", "true").lower() != "false"

_SCHEME = "https" if _PUBLIC_TLS else "http"
_WS_SCHEME = "wss" if _PUBLIC_TLS else "ws"

# Set only where a reverse proxy rewrites Host so it no longer matches the Origin
# the browser sends. Empty means "compare against this request's own Host", which
# is what a normally-configured proxy allows.
_ALLOWED_ORIGIN_HOSTS = {
    h.strip().lower()
    for h in os.environ.get("PORTAL_ALLOWED_ORIGIN_HOSTS", "").split(",")
    if h.strip()
}

# How many proxies stand between the client and this process. It decides which
# entry of X-Forwarded-For is believed, and believing the wrong one lets a client
# choose its own rate-limit bucket — or push somebody else's to the limit. Count
# the hops that actually exist rather than copying a number: 0 trusts nothing and
# uses the peer address, one HTTP proxy in front makes it 1, a cloud L7 load
# balancer in front of that makes it 2. An L4 load balancer that preserves the
# source address adds no hop.
_TRUSTED_PROXY_HOPS = int(os.environ.get("TRUSTED_PROXY_HOPS", "0"))

# Required rather than optional, though the limiter fails open at request time.
# Those are different failures: an unreachable Redis is an incident, an unset
# variable is a portal that was never rate limited at all and says nothing about
# it. The first is survivable; the second is the open item this closes.
_REDIS_URL = os.environ.get("REDIS_URL", "").strip()
if not _REDIS_URL:
    raise RuntimeError(
        "REDIS_URL is required: it backs the rate limits on /login and /register, "
        "which are the only endpoints here that anyone on the internet can reach.")

_LOGIN_PER_ACCOUNT = int(os.environ.get("LOGIN_RATE_LIMIT_PER_ACCOUNT", LOGIN_MAX_FAILURES_PER_ACCOUNT))
_LOGIN_PER_IP = int(os.environ.get("LOGIN_RATE_LIMIT_PER_IP", LOGIN_MAX_FAILURES_PER_IP))
_REGISTER_PER_IP = int(os.environ.get("REGISTER_RATE_LIMIT_PER_IP", REGISTER_MAX_PER_IP))

_db = DbWriterClient(_DB_WRITER_URL)
_login_limiter = RateLimiter(_REDIS_URL, int(os.environ.get("RATE_LIMIT_WINDOW_S", RATE_LIMIT_WINDOW_S)))
_register_limiter = RateLimiter(_REDIS_URL, int(os.environ.get("REGISTER_RATE_WINDOW_S", REGISTER_WINDOW_S)))

_HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        f"portal ready — db-writer at {_DB_WRITER_URL}, "
        f"rate limits {_LOGIN_PER_ACCOUNT}/account and {_LOGIN_PER_IP}/address on login, "
        f"{_REGISTER_PER_IP}/address on registration, trusting {_TRUSTED_PROXY_HOPS} proxy hop(s)")
    yield
    await _login_limiter.close()
    await _register_limiter.close()


app = FastAPI(title="AGRARIAN portal", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

# ── Session cookie ────────────────────────────────────────────────────────────


def _session_of(request: Request) -> Optional[str]:
    return request.cookies.get(SESSION_COOKIE)


def _set_session(response: Response, token: str) -> None:
    """
    Store the session token where page JavaScript cannot read it.

    httpOnly is the point: an XSS bug in these pages can act as the user while
    they are on the page, but it cannot exfiltrate a credential that keeps
    working for eight hours after they close the tab.

    SameSite=strict, not lax. Nothing legitimately enters this site from a
    cross-site POST or link — there is no OAuth callback and no payment return —
    so the strictest setting costs nothing and removes cross-site request forgery
    as a category before the Origin check below has to catch it.
    """
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_COOKIE_MAX_AGE_S,
        httponly=True,
        secure=_COOKIE_SECURE,
        samesite="strict",
        path="/",
    )


def _clear_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def _redirect(to: str) -> RedirectResponse:
    """303, so the browser turns a form POST into a GET and a reload cannot repost it."""
    return RedirectResponse(to, status_code=303)


# ── Cross-site request forgery ────────────────────────────────────────────────


class CrossOriginRefused(Exception):
    """A state-changing request that did not come from this site."""


def _check_origin(request: Request) -> None:
    """
    Refuse a state-changing request that did not originate from this site.

    Belt and braces with SameSite=strict, and cheap enough to be worth it: the
    cookie attribute is enforced by the browser, this is enforced here, and the
    two fail independently. What they guard is real — a cross-site POST to
    /streams/{id}/revoke would take a tenant's ingest key out of service without
    them.

    Origin first, Referer only as a fallback: Origin is sent on every browser
    POST and carries no path, so it leaks nothing. Neither present is refused
    rather than allowed — a browser sends one, and something that sends neither
    is not the browser these pages are for.
    """
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        logger.warning(f"Refused {request.method} {request.url.path}: no Origin or Referer")
        raise CrossOriginRefused()

    host = (urlparse(source).netloc or "").lower()
    expected = (request.headers.get("host") or "").lower()
    if host != expected and host not in _ALLOWED_ORIGIN_HOSTS:
        logger.warning(f"Refused cross-origin {request.method} {request.url.path} from {source!r}")
        raise CrossOriginRefused()


# ── URLs the browser dials ────────────────────────────────────────────────────


def _ingest_url(stream_key: str) -> str:
    """What the operator types into the drone controller (§6, step 3)."""
    return f"rtmps://{_MEDIA_PUBLIC_HOST}:{RTMPS_PORT}/{INGEST_PATH_PREFIX}/{stream_key}"


def _watch_urls(output_path: str, viewer_token: str) -> dict:
    """
    Where to read the annotated output, authorised by a flight-scoped token.

    The token rides in the query string for all three. That is not ideal — it
    reaches proxy access logs — and it is why viewer tokens are short-lived and
    scoped to one flight: neither a browser's WebSocket handshake nor a <video>
    source can carry an Authorization header.
    """
    return {
        # The WHEP endpoint itself, not MediaMTX's built-in reader page at
        # /<path>/. That page was the original target and it cannot work here:
        # it answers 401 with WWW-Authenticate: Basic and never calls the auth
        # hook at all — not for ?jwt=, ?token=, ?user=&pass=, Bearer, or Basic.
        # It is gated behind MediaMTX's internal user roster, which authMethod:
        # http deliberately replaced (§4). The WHEP endpoint below does consult
        # the hook and does accept the viewer token, so watch.js negotiates the
        # session itself. The media path is still browser-to-MediaMTX end to end
        # (DTLS-SRTP over UDP, §7) — only the signalling moved.
        "whep_url": f"{_SCHEME}://{_MEDIA_PUBLIC_HOST}:{WEBRTC_PORT}/{output_path}/whep?jwt={viewer_token}",
        "hls_url": f"{_SCHEME}://{_MEDIA_PUBLIC_HOST}:{HLS_PORT}/{output_path}/index.m3u8?jwt={viewer_token}",
        "ws_url": f"{_WS_SCHEME}://{_WS_PUBLIC_HOST}:{_WS_PUBLIC_PORT}/?token={viewer_token}",
    }


# ── Error handling ────────────────────────────────────────────────────────────


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith("/api/")


@app.exception_handler(CrossOriginRefused)
async def _cross_origin(request: Request, exc: CrossOriginRefused):
    """
    403 and nothing else. No detail about which check failed: the only caller
    that ever sees this is one that should not have been making the request.
    """
    if _wants_json(request):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    return templates.TemplateResponse(
        request, "error.html",
        {"status": 403, "message": "This request did not come from this site and was refused."},
        status_code=403)


@app.exception_handler(SessionExpired)
async def _session_expired(request: Request, exc: SessionExpired):
    """
    db-writer said the token is no good, so neither is the cookie holding it.

    Clearing it here is what stops a user bouncing between a page that thinks
    they are signed in and an API that disagrees.
    """
    if _wants_json(request):
        return JSONResponse({"error": "Session expired"}, status_code=401)
    response = _redirect("/login?expired=1")
    _clear_session(response)
    return response


@app.exception_handler(DbWriterUnavailable)
async def _upstream_down(request: Request, exc: DbWriterUnavailable):
    """
    502, not 500: nothing here failed. The control plane being unreachable stops
    sign-ups and key changes and stops nothing that is already flying (§2).
    """
    logger.error(f"db-writer unavailable serving {request.url.path}")
    if _wants_json(request):
        return JSONResponse({"error": "Service temporarily unavailable"}, status_code=502)
    return templates.TemplateResponse(
        request, "error.html",
        {"status": 502, "message": "The service is temporarily unavailable. Try again shortly."},
        status_code=502)


@app.exception_handler(UpstreamRejected)
async def _upstream_rejected(request: Request, exc: UpstreamRejected):
    """
    The catch-all for a refusal no route chose to render itself. Form routes
    handle their own, because only they know which form to redraw with the
    message attached.
    """
    if _wants_json(request):
        return JSONResponse({"error": exc.detail}, status_code=exc.status)
    return templates.TemplateResponse(
        request, "error.html", {"status": exc.status, "message": exc.detail},
        status_code=exc.status)


# ── Health ────────────────────────────────────────────────────────────────────


@app.get("/health")
def health():
    """
    This process only. db-writer is deliberately not probed from here: a portal
    that reports itself unhealthy because db-writer is restarting would be
    removed from rotation for a fault it does not have, and would take the login
    page down with it.
    """
    return {"status": "ok"}


# ── Registration and login ────────────────────────────────────────────────────


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, expired: Optional[str] = Query(default=None)):
    if _session_of(request):
        return _redirect("/")
    return templates.TemplateResponse(
        request, "login.html",
        {"error": "Your session expired — please sign in again." if expired else None})


def _too_many(request: Request, template: str, wait_s: int, email: Optional[str] = None):
    """
    429, with how long to wait — in the header for a client and in the page for a
    person. The wording is the same whatever was limited: telling a caller which
    of the two counters it hit would tell it which one to work around.
    """
    return templates.TemplateResponse(
        request, template, {"error": retry_message(wait_s), "form_email": email},
        status_code=429, headers={"Retry-After": str(wait_s)})


@app.post("/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    """
    The one place a password is typed, and the last place it exists: it is sent
    to db-writer, exchanged for a session token, and never stored on either side
    of this hop.

    Also the one place an attacker can guess one. Two counters bound that (see
    rate_limit.py) and they are consulted BEFORE db-writer is called: an attempt
    that is over the limit must not cost a bcrypt verification, or the limiter
    becomes the cheapest way to load the database with expensive work.
    """
    _check_origin(request)

    source = client_ip(request.client.host if request.client else None,
                       request.headers.get("x-forwarded-for"), _TRUSTED_PROXY_HOPS)
    acct_bucket = account_key(email)
    ip_bucket = ip_key("login", source)

    wait = await _login_limiter.blocked_for(
        [(acct_bucket, _LOGIN_PER_ACCOUNT), (ip_bucket, _LOGIN_PER_IP)])
    if wait is not None:
        logger.warning(f"Login refused by rate limit from {source}")
        return _too_many(request, "login.html", wait, email)

    try:
        result = await _db.login(email, password)
    except SessionExpired:
        # db-writer answers 401 for bad credentials, which is not an expired
        # session — redirecting to /login from /login would loop. Caught here so
        # the global handler never sees it.
        #
        # Counted here rather than above, so only failures count: a busy user
        # signing in from three devices is not an attack, and locking them out
        # for it would be a self-inflicted outage.
        await _login_limiter.record(acct_bucket, ip_bucket)
        return templates.TemplateResponse(
            request, "login.html", {"error": "Invalid email or password.", "form_email": email},
            status_code=401)
    except UpstreamRejected as e:
        return templates.TemplateResponse(
            request, "login.html", {"error": e.detail, "form_email": email}, status_code=e.status)

    # The account's counter clears, the source's does not. Someone who holds one
    # valid account would otherwise reset their own IP budget at will, which is
    # exactly the position a credential-stuffing run is in.
    await _login_limiter.forget(acct_bucket)

    response = _redirect("/")
    _set_session(response, result["session_token"])
    logger.info(f"Signed in: user_id={result['user_id']}")
    return response


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    if _session_of(request):
        return _redirect("/")
    return templates.TemplateResponse(request, "register.html", {"error": None})


@app.post("/register")
async def register(request: Request, email: str = Form(...), password: str = Form(...)):
    """
    Registration is open (§3), so this is the one form on the public internet
    anyone can succeed at. It signs the new account straight in — the password
    was proven in the same request, and bouncing to a login form would only ask
    for it a second time.

    Rate limited per source address, counting every attempt rather than every
    success: a 409 on a taken address makes this an account-existence oracle
    whether or not it creates anything, and that is worth bounding on its own.
    Being open is also what makes the count the only brake there is — there is no
    invitation to run out of.
    """
    _check_origin(request)

    source = client_ip(request.client.host if request.client else None,
                       request.headers.get("x-forwarded-for"), _TRUSTED_PROXY_HOPS)
    bucket = ip_key("register", source)

    wait = await _register_limiter.blocked_for([(bucket, _REGISTER_PER_IP)])
    if wait is not None:
        logger.warning(f"Registration refused by rate limit from {source}")
        return _too_many(request, "register.html", wait, email)
    await _register_limiter.record(bucket)

    try:
        result = await _db.register(email, password)
    except UpstreamRejected as e:
        # 409 duplicate address and 400 malformed email or weak password all land
        # here, and db-writer's message is already written for a person to read.
        return templates.TemplateResponse(
            request, "register.html", {"error": e.detail, "form_email": email}, status_code=e.status)

    response = _redirect("/")
    _set_session(response, result["session_token"])
    logger.info(f"Registered and signed in: user_id={result['user_id']}")
    return response


@app.post("/logout")
def logout(request: Request):
    """
    Drops the cookie, and that is all it can do.

    The token itself stays valid until it expires — there is no revocation list,
    which is the price of stateless sessions and the reason their lifetime is
    hours rather than weeks. Logging out on a shared machine is what this
    protects; a stolen token is not what it protects against.
    """
    _check_origin(request)
    response = _redirect("/login")
    _clear_session(response)
    return response


# ── Dashboard ─────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """
    Stream slots, their ingest URLs, and whatever is airborne right now.

    Two calls rather than one because they answer different questions of
    different services' tables — and because a slot with nothing flying is the
    normal case, not a missing flight.
    """
    token = _session_of(request)
    if not token:
        return _redirect("/login")

    me = await _db.whoami(token)
    streams = await _db.list_streams(token)
    flights = await _db.active_flights(token)
    # Needed to render the per-slot picker, and cheap: a user has a handful of these.
    fences = await _db.list_geofences(token)
    fence_labels = {f["geofence_id"]: f["label"] or f"Boundary {f['geofence_id']}"
                    for f in fences}

    live_by_stream = {f["stream_id"]: f for f in flights}
    rows = [
        {
            "stream_id": s["stream_id"],
            "label": s["label"],
            "stream_key": s["stream_key"],
            "created_at": s["created_at"],
            "app_mode": s.get("app_mode"),
            "geofence_id": s.get("geofence_id"),
            "geofence_label": fence_labels.get(s.get("geofence_id")),
            "ingest_url": _ingest_url(s["stream_key"]),
            "live": live_by_stream.get(s["stream_id"]),
        }
        for s in streams
    ]
    return templates.TemplateResponse(
        request, "dashboard.html",
        {"email": me.get("email"), "streams": rows, "live_count": len(flights),
         "app_modes": APP_MODE_CHOICES, "geofences": fences})


@app.post("/streams")
async def add_stream(request: Request, label: str = Form(default=""),
                     app_mode: str = Form(default=""),
                     geofence_id: str = Form(default="")):
    """
    The endpoint that spends money (§4): a slot is what lets a GPU container come
    into existence. db-writer caps it per user; a 409 here is that cap, and it is
    shown to the user rather than swallowed.

    app_mode chooses which pipeline this slot's flights run. An empty value means
    "whatever the deployment is set to", which is what every slot did while the mode
    was a deployment-wide variable. It is not validated here: db-writer owns the list
    of supported modes and answers 400, so the portal does not hold a second copy to
    drift out of step.
    """
    _check_origin(request)
    token = _session_of(request)
    if not token:
        return _redirect("/login")
    await _db.create_stream(token, label.strip() or None, app_mode.strip() or None,
                            int(geofence_id) if geofence_id.strip() else None)
    return _redirect("/")


@app.post("/streams/{stream_id}/mode")
async def set_stream_mode(request: Request, stream_id: int,
                          app_mode: str = Form(default="")):
    """
    Change which pipeline a slot runs, from the next flight onwards.

    Not the one in the air: the mode is read when a flight opens and handed to a
    container that has already started.
    """
    _check_origin(request)
    token = _session_of(request)
    if not token:
        return _redirect("/login")
    await _db.set_stream_mode(token, stream_id, app_mode.strip() or None)
    return _redirect("/")


@app.post("/streams/{stream_id}/geofence")
async def set_stream_geofence(request: Request, stream_id: int,
                              geofence_id: str = Form(default="")):
    """
    Point a slot at one of the caller's boundaries, from the next flight onwards.

    Empty means none, which disables geofencing for the slot. Both ids are checked
    against the session's user by db-writer, and a guess at either answers 404.
    """
    _check_origin(request)
    token = _session_of(request)
    if not token:
        return _redirect("/login")
    await _db.set_stream_geofence(
        token, stream_id, int(geofence_id) if geofence_id.strip() else None)
    return _redirect("/")


def _vertices_from(lon: list[str], lat: list[str]) -> list:
    """
    Pair up the editor's parallel lon/lat fields, dropping blank rows.

    Blank rows are dropped rather than rejected because the editor always renders one
    empty row to type the next point into, and submitting without touching it must not
    be an error.

    Nothing here validates a coordinate. db-writer owns the ranges, the three-point
    floor and the ceiling, and answers 400 with a message written for a human — a
    second copy of those rules here is a second thing to keep in step, and §11.3 keeps
    only ONE other copy on purpose, in the app, as a boundary assertion rather than a
    parser.
    """
    return [[x.strip(), y.strip()] for x, y in zip(lon, lat) if x.strip() or y.strip()]


@app.get("/geofences", response_class=HTMLResponse)
async def geofences_page(request: Request):
    """The caller's named boundaries, and the form to add one."""
    token = _session_of(request)
    if not token:
        return _redirect("/login")
    me = await _db.whoami(token)
    fences = await _db.list_geofences(token)
    return templates.TemplateResponse(
        request, "geofences.html", {"email": me.get("email"), "geofences": fences})


@app.post("/geofences")
async def add_geofence(request: Request, label: str = Form(default=""),
                       lon: list[str] = Form(default=[]),
                       lat: list[str] = Form(default=[])):
    _check_origin(request)
    token = _session_of(request)
    if not token:
        return _redirect("/login")
    await _db.create_geofence(token, label.strip() or None, _vertices_from(lon, lat))
    return _redirect("/geofences")


@app.post("/geofences/{geofence_id}")
async def edit_geofence(request: Request, geofence_id: int,
                        label: str = Form(default=""),
                        lon: list[str] = Form(default=[]),
                        lat: list[str] = Form(default=[])):
    """
    Replace a boundary's points and name.

    Every slot pointing at it flies the new shape from its next flight — which is the
    reason boundaries are named at all. Flights already recorded keep the boundary they
    were judged against, because db-writer snapshots it when the flight opens.
    """
    _check_origin(request)
    token = _session_of(request)
    if not token:
        return _redirect("/login")
    await _db.update_geofence(token, geofence_id, label.strip() or None,
                              _vertices_from(lon, lat))
    return _redirect("/geofences")


@app.post("/geofences/{geofence_id}/delete")
async def remove_geofence(request: Request, geofence_id: int):
    """
    Delete a boundary. Slots using it stop geofencing; past flights are unaffected.

    A hard delete rather than the revoke a stream gets, and safe for one reason:
    nothing in history points here. Each flight stores what it was judged against.
    """
    _check_origin(request)
    token = _session_of(request)
    if not token:
        return _redirect("/login")
    await _db.delete_geofence(token, geofence_id)
    return _redirect("/geofences")


@app.post("/streams/{stream_id}/rotate")
async def rotate_stream(request: Request, stream_id: int):
    """
    Replace a slot's key. The answer to a leaked key that keeps the slot usable.

    A flight already in the air keeps going — MediaMTX only checks on connect —
    so this takes effect from the next one.
    """
    _check_origin(request)
    token = _session_of(request)
    if not token:
        return _redirect("/login")
    await _db.rotate_stream(token, stream_id)
    return _redirect("/")


@app.post("/streams/{stream_id}/revoke")
async def revoke_stream(request: Request, stream_id: int):
    """
    Retire a slot. Nothing is deleted: the row, its flights and their alerts
    survive, and the key stops resolving immediately (§5).
    """
    _check_origin(request)
    token = _session_of(request)
    if not token:
        return _redirect("/login")
    await _db.revoke_stream(token, stream_id)
    return _redirect("/")


# ── Flight history ────────────────────────────────────────────────────────────


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    """
    db-writer's timestamps come back as ISO-8601 strings. Anything unparseable
    reads as absent rather than raising: a malformed timestamp should cost the
    duration column on one row, not the whole page.
    """
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _duration(start: Optional[str], end: Optional[str]) -> Optional[str]:
    """
    How long a flight lasted, as a person would say it. None while it is still
    open — "so far" is not what this column means, and a number that grows every
    time the page is reloaded would be read as a duration that had been recorded.
    """
    started, ended = _parse_time(start), _parse_time(end)
    if started is None or ended is None:
        return None
    seconds = int((ended - started).total_seconds())
    if seconds < 0:
        # Not reachable through the orchestrator, which stamps end_time on
        # teardown. Rendered as unknown rather than as "-3m" if it ever is.
        return None
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _history_row(flight: dict) -> dict:
    """One flight as the table renders it. The only place duration is computed."""
    return {
        **flight,
        "duration": _duration(flight.get("start_time"), flight.get("end_time")),
        # Not "live". An open flight is usually one in the air, but it is also
        # what a crashed orchestrator leaves behind (see db-writer's note on
        # flights.end_time), and history must not assert liveness it cannot know.
        "open": not flight.get("end_time"),
    }


@app.get("/history", response_class=HTMLResponse)
async def history_page(
    request: Request,
    before: Optional[int] = Query(default=None),
    stream_id: Optional[int] = Query(default=None),
):
    """
    Every flight this account has flown, newest first.

    `before` is db-writer's cursor, carried in the URL so an "Older" link is an
    ordinary link — no state here, which is the same reason the session lives in
    a cookie. The portal never invents a cursor; it echoes the one it was given.
    """
    token = _session_of(request)
    if not token:
        return _redirect("/login")

    me = await _db.whoami(token)
    page = await _db.flight_history(token, before=before, stream_id=stream_id)

    return templates.TemplateResponse(
        request, "history.html",
        {
            "email": me.get("email"),
            "flights": [_history_row(f) for f in page["flights"]],
            "next_before": page.get("next_before"),
            "stream_id": stream_id,
            # Whether this is a later page, so the first one can be linked back
            # to without keeping a stack of cursors.
            "paged": before is not None,
        })


@app.get("/flights/{flight_id}", response_class=HTMLResponse)
async def flight_page(request: Request, flight_id: int):
    """
    One flight: what it recorded, and the alerts it raised.

    A 404 from db-writer means "not yours or not there", and the global handler
    renders it as a plain error page — the two are not distinguished here
    because they are not distinguished there either.
    """
    token = _session_of(request)
    if not token:
        return _redirect("/login")

    me = await _db.whoami(token)
    flight = await _db.flight_detail(token, flight_id)

    return templates.TemplateResponse(
        request, "flight.html",
        {
            "email": me.get("email"),
            "flight": _history_row(flight),
            # The list is capped, so the page has to be able to say so rather
            # than presenting a page of alerts as the whole flight.
            "alerts_shown": len(flight["alerts"]),
        })


@app.get("/flights/{flight_id}/alerts/{alert_id}.jpg")
async def alert_image(request: Request, flight_id: int, alert_id: int):
    """
    The crop stored with one past alert, forwarded from db-writer.

    A URL rather than a data: URI, unlike the live alert aside, and the
    difference is deliberate: live alerts arrive one at a time over a socket
    that is already open, while a flight's history is fifty of them at once.
    As resources the browser fetches them lazily, caches them, and never blocks
    the page on them.

    Nothing is cached in this process. The portal holds no state (see the module
    docstring) and an image cache would be the first thing to break that.
    """
    token = _session_of(request)
    if not token:
        return _redirect("/login")

    image = await _db.alert_image(token, flight_id, alert_id)
    return Response(
        content=image,
        media_type="image/jpeg",
        # private, because this is one tenant's photograph and a shared cache
        # between here and the browser must not keep a copy for the next caller.
        headers={"Cache-Control": "private, max-age=3600, immutable"},
    )


# ── Watching a flight ─────────────────────────────────────────────────────────


@app.get("/watch", response_class=HTMLResponse)
def watch_page(request: Request, stream_id: Optional[int] = Query(default=None)):
    """
    The page. It carries no credential of its own — the viewer token is fetched
    by its JavaScript from /api/viewer-token, so the token is never baked into
    HTML that a browser or proxy might cache.
    """
    if not _session_of(request):
        return _redirect("/login")
    return templates.TemplateResponse(request, "watch.html", {"stream_id": stream_id})


@app.post("/api/viewer-token")
async def api_viewer_token(request: Request, stream_id: Optional[int] = Query(default=None)):
    """
    Trade the session cookie for a viewer token and the URLs it opens.

    This is the downgrade in §3 made concrete: in goes a credential that controls
    the whole account, out comes one that can read one flight for a few hours.
    Only the second ever reaches the page. There is no route back — a viewer
    token presented here is refused by db-writer, which is the check that stops
    it renewing itself indefinitely.

    404 when nothing is flying is the normal case, not an error: a slot with no
    active flight is a slot whose drone is on the ground.
    """
    _check_origin(request)
    token = _session_of(request)
    if not token:
        return JSONResponse({"error": "Not signed in"}, status_code=401)

    result = await _db.viewer_token(token, stream_id)
    return {
        "flight_id": result["flight_id"],
        **_watch_urls(result["output_path"], result["viewer_token"]),
    }


if __name__ == "__main__":
    uvicorn.run("main:app", host=API_HOST, port=API_PORT, log_level="info")

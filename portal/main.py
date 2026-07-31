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
        # MediaMTX's own reader page, which negotiates WHEP and forwards the
        # query string as its credential. Deliberately not reimplemented here:
        # the media path is browser-to-MediaMTX end to end (DTLS-SRTP over UDP,
        # §7), and the portal has no business in the middle of it.
        "webrtc_url": f"{_SCHEME}://{_MEDIA_PUBLIC_HOST}:{WEBRTC_PORT}/{output_path}/?jwt={viewer_token}",
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

    live_by_stream = {f["stream_id"]: f for f in flights}
    rows = [
        {
            "stream_id": s["stream_id"],
            "label": s["label"],
            "stream_key": s["stream_key"],
            "created_at": s["created_at"],
            "ingest_url": _ingest_url(s["stream_key"]),
            "live": live_by_stream.get(s["stream_id"]),
        }
        for s in streams
    ]
    return templates.TemplateResponse(
        request, "dashboard.html",
        {"email": me.get("email"), "streams": rows, "live_count": len(flights)})


@app.post("/streams")
async def add_stream(request: Request, label: str = Form(default="")):
    """
    The endpoint that spends money (§4): a slot is what lets a GPU container come
    into existence. db-writer caps it per user; a 409 here is that cap, and it is
    shown to the user rather than swallowed.
    """
    _check_origin(request)
    token = _session_of(request)
    if not token:
        return _redirect("/login")
    await _db.create_stream(token, label.strip() or None)
    return _redirect("/")


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

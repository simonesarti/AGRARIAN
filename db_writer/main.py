"""
db-writer — flight records and alert persistence.

Holds NO per-flight state. Every endpoint works from the flight_id in the URL plus
the database, so any replica can serve any request for any flight regardless of
which replica opened it. That is what makes this service safe to scale out; an
earlier version kept a per-flight manager in a module-level dict and silently
404'd on every replica but the one that handled /session/start.

The only process-local object is AlertWriter, a queue and a thread that exist to
keep database latency off the caller's hot path. It is flight-agnostic.
"""

import base64
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel

from auth import (
    AuthError,
    mint_publisher_token,
    mint_session_token,
    mint_viewer_token,
    user_id_from_session,
    verify_publisher,
)
from constants import FLIGHT_HISTORY_PAGE_SIZE
from db_manager import (
    AlertWriter,
    EmailAlreadyRegistered,
    StreamLimitReached,
    StreamNotFound,
    UserDirectory,
)
from media_auth import Denied, authorize, credential_from
from mqtt_auth import Denied as MqttDenied
from mqtt_auth import authorize as mqtt_authorize
from mqtt_auth import identify as mqtt_identify


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("db_writer")

# ------------------------------------------------------------------ #
# Build the worker DB URL from operator-supplied env vars
# ------------------------------------------------------------------ #

_DB_SERVICE  = os.environ["DB_SERVICE"].lower()
_DB_HOST     = os.environ["DB_HOST"]
_DB_PORT     = int(os.environ.get("DB_PORT", 5432))
_DB_NAME     = os.environ["DB_NAME"]
_WORKER_NAME = os.environ["DB_WORKER_NAME"]
_WORKER_PASS = os.environ["DB_WORKER_PASSWORD"]

if _DB_SERVICE == "postgresql":
    _DATABASE_URL = f"postgresql://{_WORKER_NAME}:{_WORKER_PASS}@{_DB_HOST}:{_DB_PORT}/{_DB_NAME}"
elif _DB_SERVICE == "mysql":
    _DATABASE_URL = f"mysql+pymysql://{_WORKER_NAME}:{_WORKER_PASS}@{_DB_HOST}:{_DB_PORT}/{_DB_NAME}"
else:
    raise ValueError(f"Unsupported DB_SERVICE '{_DB_SERVICE}'. Use 'postgresql' or 'mysql'.")

# ------------------------------------------------------------------ #
# Process-local components — neither is keyed by flight
# ------------------------------------------------------------------ #

# Stateless lookups and flight creation. create_engine is lazy, so building this at
# import time opens no connection and cannot delay or fail startup.
_directory = UserDirectory(_DATABASE_URL)

# One writer thread per replica, draining alerts for every flight this replica
# happens to receive.
_writer = AlertWriter(_DATABASE_URL)

# ------------------------------------------------------------------ #
# Publisher authentication
# ------------------------------------------------------------------ #


def _require_publisher(authorization: Optional[str], flight_id: int) -> None:
    """Reject anything that does not hold a publisher token for THIS flight."""
    try:
        verify_publisher(authorization, flight_id)
    except AuthError as e:
        logger.warning(f"Rejected write to flight {flight_id}: {e}")
        raise HTTPException(status_code=401, detail=str(e))


def _require_session(authorization: Optional[str]) -> int:
    """
    Resolve the caller's user_id from a session token, or 401.

    Account-scoped endpoints call this instead of trusting a user_id in the
    request. Same shape as _require_publisher on purpose — one way to check a
    credential in this service, not two.
    """
    try:
        return user_id_from_session(authorization)
    except AuthError as e:
        logger.warning(f"Rejected session-scoped request: {e}")
        raise HTTPException(status_code=401, detail=str(e))

# ------------------------------------------------------------------ #
# Request / response models
# ------------------------------------------------------------------ #

class CredentialsRequest(BaseModel):
    """Registration and login take the same pair, so they share one model."""
    email: str
    password: str


class CreateStreamRequest(BaseModel):
    label: Optional[str] = None
    # Omitted or null follows the deployment's own APP_MODE, which is what every
    # slot did before this field existed.
    app_mode: Optional[str] = None


class StreamModeRequest(BaseModel):
    app_mode: Optional[str] = None


class ViewerTokenRequest(BaseModel):
    """
    Carries no credential: the caller is identified by their session token, in the
    Authorization header, like every other account-scoped route.

    stream_id is required only when the caller has more than one flight active at
    once — omitting it still works for the common case of exactly one, which is why
    the whole body is optional.
    """
    stream_id: Optional[int] = None

class OpenFlightRequest(BaseModel):
    stream_key: str


class MediaMTXAuthRequest(BaseModel):
    """
    What MediaMTX POSTs on every connection attempt.

    Every field is optional with an empty default: which ones are populated depends
    on the protocol, and a missing one must read as "no credential supplied" rather
    than fail validation — a 422 would deny, but it would deny with the wrong reason
    and be far harder to diagnose than an explicit 401.
    """
    action: str = ""
    path: str = ""
    user: str = ""
    password: str = ""
    token: str = ""
    query: str = ""
    protocol: str = ""
    ip: str = ""
    id: str = ""
    userAgent: str = ""     # noqa: N815 — MediaMTX sends this key verbatim


class MqttAuthRequest(BaseModel):
    """What mosquitto-go-auth POSTs to auth_opt_http_getuser_uri on every CONNECT."""
    username: str = ""
    password: str = ""
    clientid: str = ""


class MqttAclRequest(BaseModel):
    """What mosquitto-go-auth POSTs to auth_opt_http_aclcheck_uri on every publish,
    subscribe, and per-message read that follows a subscribe."""
    username: str = ""
    clientid: str = ""
    topic: str = ""
    acc: int = 0


class RecordingRequest(BaseModel):
    public_uuid: str
    segment_path: str
    storage_backend: str
    storage_location: Optional[str] = None


class AlertRequest(BaseModel):
    frame_id: int
    alert_msg: str
    timestamp: float
    datetime: str                   # ISO-8601 string from the app
    image_data: Optional[str] = None  # base64-encoded JPEG bytes
    image_width: int
    image_height: int

# ------------------------------------------------------------------ #
# API
# ------------------------------------------------------------------ #

@asynccontextmanager
async def lifespan(app: FastAPI):
    _writer.start()
    yield
    _writer.stop()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "alert_queue_depth": _writer.queue_depth,
        "alerts_dropped": _writer.dropped,
    }


@app.post("/register", status_code=201)
def register(req: CredentialsRequest):
    """
    Create an account and return a session token for it.

    Registration is open, so this is the one endpoint here that anyone on the
    internet can reach and succeed at. It is still not routed publicly: the portal
    calls it over the private network and holds the returned token in an httpOnly
    cookie (§4), so db-writer stays off the public side.

    Returning a token rather than making the caller log in immediately afterwards
    is not a shortcut — the password was just proven in the same request, and a
    second round trip would prove nothing further.

    409 rather than 400 for a taken address, which does disclose that the address
    is registered. That is unavoidable in open registration: the user has to be
    told why their signup failed, and "something was wrong" would leave them
    retyping a password that was never the problem.
    """
    try:
        user = _directory.create_user(req.email, req.password)
    except EmailAlreadyRegistered as e:
        # Checked before ValueError — it is a subclass, so the order matters.
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected error registering user: {e}")
        raise HTTPException(status_code=500, detail="Registration failed")

    logger.info(f"Registered user_id={user['user_id']}")
    return {
        "user_id": user["user_id"],
        "email": user["email"],
        "session_token": mint_session_token(user["user_id"]),
    }


@app.post("/login")
def login(req: CredentialsRequest):
    """
    Exchange an email and password for a session token.

    The password is presented here and nowhere else for the rest of the session,
    which is the entire reason this endpoint exists — the portal would otherwise
    have to keep it for as long as the user stayed logged in.

    No attempt is made to equalise response time between "no such user" and "wrong
    password". bcrypt runs only in the second case, so the timing does distinguish
    them — but /register already discloses exactly the same fact outright, by
    design, so hiding it here would cost a dummy hash on every failed login and
    conceal nothing. What is genuinely missing is rate limiting: this is a password
    oracle open to whoever can reach it, slowed only by bcrypt's own cost. See §9.
    """
    try:
        user_id = _directory.authenticate(req.email, req.password)
    except ValueError:
        # Deliberately not echoing the reason: it distinguishes an unknown address
        # from a wrong password to anyone reading the response body.
        logger.warning("Login failed")
        raise HTTPException(status_code=401, detail="Invalid credentials")
    except Exception as e:
        logger.error(f"Unexpected error during login: {e}")
        raise HTTPException(status_code=500, detail="Login failed")

    logger.info(f"Login: user_id={user_id}")
    return {"user_id": user_id, "session_token": mint_session_token(user_id)}


@app.get("/me")
def whoami(authorization: Optional[str] = Header(default=None)):
    """
    Resolve a session token to its user. The portal's "am I still logged in?".

    Also the smallest possible consumer of _require_session, which every
    account-scoped endpoint added after this one will use the same way.

    The email comes back too, so the portal can name the signed-in account
    without keeping a copy of it anywhere the browser could edit. A token whose
    user row has since been deleted resolves to null rather than 404: the token
    is still validly signed, and this endpoint reports what it says.
    """
    user_id = _require_session(authorization)
    return {"user_id": user_id, "email": _directory.user_email(user_id)}


# ------------------------------------------------------------------ #
# Stream slots — the portal's CRUD, all scoped by the session claim
# ------------------------------------------------------------------ #
#
# Every route here takes user_id from _require_session and NEVER from the URL or
# body. UserDirectory already refuses cross-user access, but that check is only
# worth something if the user_id it is handed is a fact rather than a guess. This
# is why the session token exists at all.
#
# A stream_id naming another user's slot is answered with the same 404 as one that
# does not exist. Distinguishing them would confirm the existence of a row the
# caller has no business knowing about, and stream_ids are sequential.


@app.get("/streams")
def list_streams(
    include_revoked: bool = False,
    authorization: Optional[str] = Header(default=None),
):
    """
    The caller's own stream slots, retired ones hidden unless asked for.

    Keys are included, deliberately. Unlike a password these are recoverable by
    design — the operator has to retype the ingest URL into a controller before
    every flight, so a key the portal cannot show again would be a key the user
    has to rotate to use.
    """
    user_id = _require_session(authorization)
    return {"streams": _directory.list_streams(user_id, include_revoked=include_revoked)}


@app.post("/streams", status_code=201)
def add_stream(
    req: CreateStreamRequest,
    authorization: Optional[str] = Header(default=None),
):
    """
    Add a concurrency slot and mint its ingest key.

    This is the endpoint that turns an account into GPU capacity: a slot is what
    lets a container come into existence. With registration open it is capped per
    user, or anyone who can sign up could create unbounded slots — see
    MAX_STREAMS_PER_USER.
    """
    user_id = _require_session(authorization)
    try:
        return _directory.create_stream(user_id, req.label, req.app_mode)
    except StreamLimitReached as e:
        # Before ValueError — it is a subclass, so order matters here as it does
        # in /register.
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected error creating stream: {e}")
        raise HTTPException(status_code=500, detail="Failed to create stream")


@app.post("/streams/{stream_id}/rotate")
def rotate_stream(
    stream_id: int,
    authorization: Optional[str] = Header(default=None),
):
    """
    Replace a slot's ingest key, killing the old one immediately.

    The answer to a leaked key that keeps the slot usable, as opposed to retiring
    it. A flight already in the air is unaffected — MediaMTX only re-checks on
    connect — so it takes effect from the next one.

    Rotating a RETIRED slot revives it. That is how a user brings one back, and it
    is why this is capped like creation: without that, revoke → add → rotate would
    net a slot over the limit on every repeat.
    """
    user_id = _require_session(authorization)
    try:
        stream_key = _directory.rotate_stream_key(stream_id, user_id)
    except StreamLimitReached as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError:
        raise HTTPException(status_code=404, detail="Stream not found")
    except Exception as e:
        logger.error(f"Unexpected error rotating stream {stream_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to rotate stream key")
    return {"stream_id": stream_id, "stream_key": stream_key}


@app.post("/streams/{stream_id}/revoke")
def revoke_stream(
    stream_id: int,
    authorization: Optional[str] = Header(default=None),
):
    """
    Retire a slot. This is what "remove" means in the portal.

    Nothing is deleted: the row, its flights and their alerts all survive, and the
    key stops resolving immediately. There is deliberately no delete_stream() to
    expose — a Remove button implemented as a hard delete would destroy flight
    history along with the slot.
    """
    user_id = _require_session(authorization)
    try:
        _directory.revoke_stream(stream_id, user_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Stream not found")
    except Exception as e:
        logger.error(f"Unexpected error revoking stream {stream_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to revoke stream")
    return {"ok": True}


@app.post("/streams/{stream_id}/mode")
def set_stream_mode(
    stream_id: int,
    req: StreamModeRequest,
    authorization: Optional[str] = Header(default=None),
):
    """
    Choose which pipeline this slot's flights run. Null reverts to the deployment's.

    Takes effect on the next flight, never the one in the air: the mode is read when
    a flight opens and injected into a container that has already started.

    A bad mode is 400 and a stream that is not the caller's is the same 404 as one
    that does not exist — `stream_id` is sequential across every tenant, so telling
    those apart would confirm a row the caller has no business knowing about.
    """
    user_id = _require_session(authorization)
    try:
        _directory.set_stream_mode(stream_id, user_id, req.app_mode)
    except StreamNotFound:
        # Before ValueError — it is a subclass, so order matters here as it does for
        # StreamLimitReached in /register and /streams.
        raise HTTPException(status_code=404, detail="Stream not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected error setting mode on stream {stream_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to set stream mode")
    return {"ok": True}


@app.get("/flights")
def list_active_flights(authorization: Optional[str] = Header(default=None)):
    """
    The caller's currently airborne flights — what the portal marks "live".

    Read-only and scoped by the session claim like every route above it. It
    returns exactly what /viewer/token disambiguates over, which is deliberate:
    the page that offers a Watch button and the call that authorises watching
    must agree on what is active, and two different queries would eventually
    disagree.

    No flight *history* here. That is a separate question with separate paging
    and a separate cost, and it is answered by /flights/history below.
    """
    user_id = _require_session(authorization)
    return {"flights": _directory.active_flights(user_id)}


# ------------------------------------------------------------------ #
# Flight history — the read side of everything the system recorded
# ------------------------------------------------------------------ #
#
# Declared BEFORE /flights/{flight_id}. FastAPI matches routes in declaration
# order, and "history" is not an int, so the parameterised route would answer
# this path with a 422 about path parameter parsing if it came first.


@app.get("/flights/history")
def flight_history(
    limit: int = FLIGHT_HISTORY_PAGE_SIZE,
    before: Optional[int] = None,
    stream_id: Optional[int] = None,
    authorization: Optional[str] = Header(default=None),
):
    """
    A page of the caller's own flights, newest first, with alert and recording
    counts.

    `before` is a cursor, not a page number: it is a flight_id, and the page is
    the flights below it. See UserDirectory.flight_history for why an offset
    would repeat rows here.

    Scoped by the session claim like every account route above it, so there is
    no user_id to tamper with and a stream_id belonging to somebody else selects
    nothing rather than being refused — the filter runs inside the same query
    that already restricts to this user's flights.
    """
    user_id = _require_session(authorization)
    return _directory.flight_history(
        user_id, limit=limit, before=before, stream_id=stream_id)


@app.get("/flights/{flight_id}")
def flight_detail(flight_id: int, authorization: Optional[str] = Header(default=None)):
    """
    One flight: when it flew, what it recorded, and its most recent alerts.

    404 for a flight belonging to another user, identical to the 404 for one
    that does not exist. flight_ids are sequential, so a distinguishable
    response would confirm the existence of other tenants' flights to anyone
    willing to count.

    Alert images are not in this response — see UserDirectory.flight_detail.
    They are fetched one at a time from the route below, so a page with fifty
    alerts on it loads fifty small resources the browser can cache and defer
    rather than one enormous one it cannot.
    """
    user_id = _require_session(authorization)
    detail = _directory.flight_detail(flight_id, user_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Flight not found")
    return detail


@app.get("/flights/{flight_id}/alerts/{alert_id}/image")
def alert_image(
    flight_id: int,
    alert_id: int,
    authorization: Optional[str] = Header(default=None),
):
    """
    The JPEG crop stored with one alert.

    The only route in this service that returns something other than JSON. It is
    still session-scoped: these are photographs of a tenant's own land, and both
    the flight and the alert are checked against the caller's ownership in one
    query (see alert_image) rather than trusted from the URL.

    404 covers all three failures — no such alert, wrong tenant, and an alert
    that simply carried no crop — because the caller is entitled to distinguish
    none of them.
    """
    user_id = _require_session(authorization)
    image = _directory.alert_image(alert_id, flight_id, user_id)
    if image is None:
        raise HTTPException(status_code=404, detail="No image for that alert")
    # private: a crop is one tenant's, and a shared proxy must not keep a copy
    # to hand to the next caller asking for the same URL. immutable because an
    # alert row is never rewritten — the bytes at this id will not change.
    return Response(
        content=image,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=3600, immutable"},
    )


@app.post("/session/{flight_id}/alert")
def save_alert(
    flight_id: int,
    req: AlertRequest,
    authorization: Optional[str] = Header(default=None),
):
    _require_publisher(authorization, flight_id)

    image_bytes = base64.b64decode(req.image_data) if req.image_data else None

    queued = _writer.enqueue(
        flight_id,
        frame_id=req.frame_id,
        alert_msg=req.alert_msg,
        timestamp=req.timestamp,
        datetime=datetime.fromisoformat(req.datetime),
        image_data=image_bytes,
        image_width=req.image_width,
        image_height=req.image_height,
    )
    if not queued:
        raise HTTPException(status_code=503, detail="Alert queue full — DB may be unavailable")
    return {"queued": True}


@app.post("/flight/open")
def open_flight(req: OpenFlightRequest):
    """
    Open a flight for a stream key. Called by the orchestrator, never by an app.

    This is what removes end-user credentials from the GPU tier. Previously the app
    container called /session/start with the operator's email and password, which put
    a reusable account credential inside a container that processes untrusted video.
    Now the orchestrator — which learned the key from MediaMTX going live — opens the
    flight and injects only the result.

    Returns the publisher token once. It is not re-derivable from the flight_id, and
    nothing stores it.

    INTERNAL ONLY. Possession of a live stream key is the whole authorisation, which
    is the same authority the key already carries at MediaMTX; but this port must
    never be routed from outside the cluster network regardless.
    """
    flight = _directory.open_flight_for_key(req.stream_key)
    if flight is None:
        # Deliberately vague: this endpoint must not become a way to test whether a
        # given stream key exists.
        logger.warning("Refused flight open for an unknown or revoked stream key")
        raise HTTPException(status_code=401, detail="unauthorized")

    logger.info(
        f"Flight opened for orchestrator: flight_id={flight['flight_id']} "
        f"stream_id={flight['stream_id']}"
    )
    return {
        "flight_id": flight["flight_id"],
        "public_uuid": flight["public_uuid"],
        "stream_id": flight["stream_id"],
        "user_id": flight["user_id"],
        # The paths the app must read from and publish to. Derived here so the
        # orchestrator does not have to know the naming scheme, and so a change to it
        # is a change in one place.
        "ingest_path": f"in/{req.stream_key}",
        "output_path": f"out/{flight['public_uuid']}",
        # Which pipeline this slot runs, or None to leave the deployment's setting
        # alone. This is what lets one cluster serve a livestock tenant and a terrain
        # tenant at once — the mode used to be an environment variable on the
        # orchestrator, so every flight it ever started ran the same product.
        "app_mode": flight["app_mode"],
        "publisher_token": mint_publisher_token(flight["flight_id"]),
    }


@app.post("/flight/{flight_id}/close")
def close_flight(flight_id: int, authorization: Optional[str] = Header(default=None)):
    """
    Stamp a flight as finished. Idempotent.

    Requires the flight's own publisher token, so the orchestrator must still hold the
    credential it was given when the flight opened — a stale or cross-flight token
    cannot close somebody else's flight.
    """
    _require_publisher(authorization, flight_id)
    if not _directory.close_flight(flight_id):
        raise HTTPException(status_code=404, detail=f"Flight {flight_id} not found")
    return {"ok": True}


@app.post("/recording")
def record_upload(req: RecordingRequest):
    """
    Log an uploaded recording segment against the flight it belongs to.

    Called by the recorder sidecar, never by the app or a tenant. The recorder only
    ever learns an output path (out/<public_uuid>) from MediaMTX's segment-complete
    hook, never a flight_id, so this is also where that path is resolved.

    INTERNAL ONLY, like /auth/mediamtx: the caller is a trusted sidecar on the private
    network, not a tenant, and there is no credential to check here — a public_uuid
    names no capability by itself, unlike a stream key or a token.
    """
    flight_id = _directory.record_upload(
        req.public_uuid, req.segment_path, req.storage_backend, req.storage_location,
    )
    if flight_id is None:
        logger.warning(f"Recording upload for unknown output path: {req.public_uuid!r}")
        raise HTTPException(status_code=404, detail="Unknown public_uuid")
    return {"flight_id": flight_id}


@app.post("/auth/mediamtx")
def mediamtx_auth(req: MediaMTXAuthRequest):
    """
    Authorise one MediaMTX connection attempt. 200 allows, 401 denies.

    This replaces MediaMTX's static authInternalUsers roster, which cannot express
    "user 101 signed up while 100 people were streaming". MediaMTX now asks a
    question instead of holding a list, so a new user works the instant their row
    exists — no restart, no reload.

    Choosing HTTP auth over MediaMTX's built-in JWT method is what keeps the
    credential form open: what counts as valid is decided here, in Python, so a
    future ground station that can fetch a short-lived token is a change to this
    function rather than to media server configuration.

    The denial reason is logged and never returned. A caller learning whether a
    stream key exists, or whether a token was merely for the wrong flight, would be
    learning something about another tenant.

    NOTE: every publish and every read passes through here, so this endpoint is on
    the critical path for the whole media plane. It does one indexed lookup and no
    write. Caching is deliberately absent — a cache on an authorisation decision
    delays revocation, and revocability is the property stream keys are built on.
    """
    credential = credential_from(req.user, req.password, req.token, req.query)

    try:
        authorize(req.action, req.path, credential, _directory)
    except Denied as e:
        logger.warning(
            f"MediaMTX auth denied: action={req.action!r} path={req.path!r} "
            f"protocol={req.protocol!r} ip={req.ip!r}: {e}"
        )
        raise HTTPException(status_code=401, detail="unauthorized")

    logger.info(
        f"MediaMTX auth allowed: action={req.action!r} path={req.path!r} "
        f"protocol={req.protocol!r} ip={req.ip!r}"
    )
    return {"ok": True}


@app.post("/auth/mqtt/user")
def mqtt_auth_user(req: MqttAuthRequest):
    """
    Authorise one Mosquitto CONNECT. 200 allows, 401 denies.

    The counterpart to /auth/mediamtx for the telemetry plane, called by the
    mosquitto-go-auth plugin's HTTP backend instead of MediaMTX's built-in hook.
    Only proves the credential is live; which topics it may touch is decided per
    attempt in /auth/mqtt/acl below.
    """
    if not mqtt_identify(req.username, _directory):
        logger.warning("Mosquitto auth denied: unknown or revoked credential")
        raise HTTPException(status_code=401, detail="unauthorized")
    return {"ok": True}


@app.post("/auth/mqtt/acl")
def mqtt_auth_acl(req: MqttAclRequest):
    """
    Authorise one Mosquitto publish, subscribe, or per-message read. 200 allows,
    401 denies. See mqtt_auth.py for the four legitimate combinations.
    """
    try:
        mqtt_authorize(req.username, req.topic, req.acc, _directory)
    except MqttDenied as e:
        logger.warning(f"Mosquitto ACL denied: topic={req.topic!r} acc={req.acc}: {e}")
        raise HTTPException(status_code=401, detail="unauthorized")
    return {"ok": True}


@app.post("/viewer/token")
def issue_viewer_token(
    req: Optional[ViewerTokenRequest] = None,
    authorization: Optional[str] = Header(default=None),
):
    """
    Issue a viewer token for one of the caller's own currently active flights,
    without opening a new one. This is how the UI gets the credential it
    presents to ws-server and to MediaMTX.

    Authorised by a **session token**, not an email and password. It used to take
    the password on every call, which was survivable when the caller was a script
    but not once a portal is the caller: a page that refreshes a viewer token
    whenever one expires would have to keep the password for the length of the
    session. This was the last endpoint outside /login that accepted one, so the
    password now reaches exactly one route in the whole system.

    This is also a **credential downgrade**, which is the point of doing it this
    way round: the caller presents a token that identifies their account, and
    receives one scoped to a single flight, with no path back. What the browser
    hands to MediaMTX and ws-server can watch one flight and do nothing else,
    while the session token that authorised it stays in an httpOnly cookie the
    page's own JavaScript cannot read.

    Which flight: it used to return "the most recent", which was wrong two ways
    at once — it could hand out a token for a flight that had already landed
    (start time is not a liveness signal), and it silently picked one once a
    second stream could be active at the same time. Both are the same underlying
    bug ("latest" is not "active"), so both are fixed by the same query: with
    zero active flights there is nothing to hand out, with exactly one there is
    nothing to ask, and with more than one the caller must say which stream_id
    it means.

    The token is scoped to a flight belonging to the authenticated user, so a
    user can never be issued a token for someone else's flight.
    """
    user_id = _require_session(authorization)
    stream_id = req.stream_id if req is not None else None

    try:
        active = _directory.active_flights(user_id)
    except Exception as e:
        logger.error(f"Unexpected error issuing viewer token: {e}")
        raise HTTPException(status_code=500, detail="Failed to issue viewer token")

    if stream_id is not None:
        matches = [f for f in active if f["stream_id"] == stream_id]
        if not matches:
            # Deliberately the same 404 whether stream_id belongs to someone else
            # or simply has nothing active: the caller already owns every row in
            # `active`, so distinguishing the two would only leak which is true.
            raise HTTPException(status_code=404, detail="No active flight on that stream")
        flight = matches[0]
    elif len(active) == 1:
        flight = active[0]
    elif len(active) == 0:
        raise HTTPException(status_code=404, detail="No active flight for this user")
    else:
        # jsonable_encoder because this is an HTTPException detail, which FastAPI
        # serialises with plain json.dumps rather than the response encoder — a
        # datetime in here is a 500, not a 409, and the rows carry start_time.
        raise HTTPException(status_code=409, detail={
            "message": "More than one flight is active; specify stream_id",
            "active_flights": jsonable_encoder(active),
        })

    flight_id = flight["flight_id"]
    logger.info(f"Viewer token issued: flight_id={flight_id}, user_id={user_id}")
    return {
        "flight_id": flight_id,
        # What the token is good for. The portal needs both to build a playable
        # URL, and returning them together means it never has to ask a second
        # question about a flight it was just authorised for.
        "output_path": flight["output_path"],
        "viewer_token": mint_viewer_token(flight_id, user_id),
    }


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, log_level="info")

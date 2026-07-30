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
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from auth import AuthError, mint_publisher_token, mint_viewer_token, verify_publisher
from db_manager import AlertWriter, UserDirectory
from media_auth import Denied, authorize, credential_from


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

# ------------------------------------------------------------------ #
# Request / response models
# ------------------------------------------------------------------ #

class StartSessionRequest(BaseModel):
    email: str
    password: str

class StreamUrlRequest(BaseModel):
    url: str

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


@app.post("/session/start")
def start_session(req: StartSessionRequest):
    """
    Verify user identity against the users table and open a new flight record.

    Returns the flight_id the app must attach to subsequent requests, the viewer
    token the UI presents to ws-server, and the publisher token authorising writes
    to this flight. Both tokens are returned once, here, and neither is re-derivable
    from the flight_id alone.

    Nothing is retained in this process: the flight is a database row, so a later
    alert for it can be served by any replica.
    """
    try:
        flight = _directory.start_flight(req.email, req.password)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected error during session start: {e}")
        raise HTTPException(status_code=500, detail="Failed to start session")

    logger.info(f"Session started: flight_id={flight['flight_id']}, user={req.email}")
    return {
        "flight_id": flight["flight_id"],
        "public_uuid": flight["public_uuid"],
        "viewer_token": mint_viewer_token(flight["flight_id"], flight["user_id"]),
        # Handed to the app container so it can write alerts to this flight and no
        # other. Returned once, here — it is not re-derivable from the flight_id.
        "publisher_token": mint_publisher_token(flight["flight_id"]),
    }


@app.post("/session/{flight_id}/stream-url")
def set_stream_url(
    flight_id: int,
    req: StreamUrlRequest,
    authorization: Optional[str] = Header(default=None),
):
    _require_publisher(authorization, flight_id)
    if not _directory.set_output_url(flight_id, req.url):
        raise HTTPException(status_code=404, detail=f"Flight {flight_id} not found")
    return {"ok": True}


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


@app.post("/viewer/token")
def issue_viewer_token(req: StartSessionRequest):
    """
    Issue a viewer token for the caller's most recent flight, without opening a
    new one. This is how the UI gets the credential it presents to ws-server.

    The token is scoped to the flight belonging to the authenticated user, so a
    user can never be issued a token for someone else's flight.
    """
    try:
        user_id = _directory.authenticate(req.email, req.password)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected error issuing viewer token: {e}")
        raise HTTPException(status_code=500, detail="Failed to issue viewer token")

    flight_id = _directory.latest_flight_id(user_id)
    if flight_id is None:
        raise HTTPException(status_code=404, detail="No flight found for this user")

    logger.info(f"Viewer token issued: flight_id={flight_id}, user={req.email}")
    return {
        "flight_id": flight_id,
        "viewer_token": mint_viewer_token(flight_id, user_id),
    }


@app.delete("/session/{flight_id}")
def close_session(flight_id: int, authorization: Optional[str] = Header(default=None)):
    """
    Mark a flight finished.

    There is nothing per-flight to tear down any more — no manager, no connection,
    no thread — so this only records the event. It is kept because the app calls it,
    and because the orchestrator will want a place to hook flight completion.
    Idempotent by construction.

    Queued alerts are unaffected: they belong to the process-wide writer and are
    drained regardless of whether the flight has been closed.
    """
    _require_publisher(authorization, flight_id)
    logger.info(f"Session closed: flight_id={flight_id}")
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, log_level="info")

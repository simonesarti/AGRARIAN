import base64
import logging
import os
from datetime import datetime
from threading import Lock
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from db_manager import DatabaseManager


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
# Session store — one DatabaseManager per active flight
# ------------------------------------------------------------------ #

_sessions: dict[int, DatabaseManager] = {}
_lock = Lock()

# ------------------------------------------------------------------ #
# Request / response models
# ------------------------------------------------------------------ #

class StartSessionRequest(BaseModel):
    email: str
    password: str

class StreamUrlRequest(BaseModel):
    url: str

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

app = FastAPI()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/session/start")
def start_session(req: StartSessionRequest):
    """
    Verify user identity against the users table and open a new flight record.
    Returns the flight_id that the app must attach to subsequent requests.
    """
    manager = DatabaseManager(database_url=_DATABASE_URL)
    try:
        manager.initialize(req.email, req.password)
    except ValueError as e:
        # initialize() raises ValueError on bad credentials
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected error during session start: {e}")
        raise HTTPException(status_code=500, detail="Failed to start session")

    with _lock:
        _sessions[manager.flight_id] = manager

    logger.info(f"Session started: flight_id={manager.flight_id}, user={req.email}")
    return {"flight_id": manager.flight_id}


@app.post("/session/{flight_id}/stream-url")
def set_stream_url(flight_id: int, req: StreamUrlRequest):
    with _lock:
        manager = _sessions.get(flight_id)
    if manager is None:
        raise HTTPException(status_code=404, detail=f"Session {flight_id} not found")
    manager.set_stream_url(req.url)
    return {"ok": True}


@app.post("/session/{flight_id}/alert")
def save_alert(flight_id: int, req: AlertRequest):
    with _lock:
        manager = _sessions.get(flight_id)
    if manager is None:
        raise HTTPException(status_code=404, detail=f"Session {flight_id} not found")

    image_bytes = base64.b64decode(req.image_data) if req.image_data else None

    queued = manager.save_alert(
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


@app.delete("/session/{flight_id}")
def close_session(flight_id: int):
    """Close and remove the DatabaseManager for this flight, flushing any queued alerts."""
    with _lock:
        manager = _sessions.pop(flight_id, None)
    if manager is None:
        return {"ok": True}  # idempotent
    manager.close()
    logger.info(f"Session closed: flight_id={flight_id}")
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, log_level="info")

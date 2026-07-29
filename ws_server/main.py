"""
ws_server — per-flight WebSocket alert broadcaster.

Alerts are scoped to a flight. App pods POST them on the internal API port;
viewers connect on the WebSocket port presenting a JWT that names the single
flight they may read.

Architecture
------------
  App pod   ──POST /session/{flight_id}/alert──►  API_PORT  ──►  Redis
  Viewer UI ──WS  /?token=<jwt>───────────────►  WS_PORT   ◄──  fan-out, that flight only

Redis carries the fan-out so the service can run behind a load balancer: the
publishing pod and the viewers need not land on the same replica.

TLS
---
The reverse proxy terminates WSS and forwards plain ws:// to WS_PORT, so nothing
here handles certificates. API_PORT is internal-only and must never be routed
from outside the cluster network — it is the alert *write* path.
"""

import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from auth import AuthError, verify_publisher
from constants import API_PORT, REDIS_URL, WS_HOST, WS_PORT
from websocket_manager import WebSocketManager

# ── Config ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("ws_server")


ws_port   = int(os.getenv("WS_PORT", WS_PORT))
redis_url = os.getenv("REDIS_URL", REDIS_URL)

# ── Singleton ─────────────────────────────────────────────────────────────────

_manager = WebSocketManager(host=WS_HOST, port=ws_port, redis_url=redis_url)

# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await _manager.start()
    logger.info(f"ws_server ready — WS on :{ws_port}, API on :{API_PORT}")
    yield
    # uvicorn is PID 1 (CMD ["uvicorn", ...] in Dockerfile), so Docker/Compose sends SIGTERM
    # directly to it on container stop. uvicorn finishes in-flight requests, then resumes this
    # coroutine past yield — which closes the WebSocket server and Redis connections.
    await _manager.stop()

# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="WS Server", lifespan=lifespan)


class AlertPayload(BaseModel):
    model_config = {"extra": "allow"}   # accept any alert fields the app sends


@app.get("/health")
def health():
    return {
        "status": "ok",
        "connected_clients": _manager.connected_clients,
        "active_flights": _manager.active_flights,
    }


@app.post("/session/{flight_id}/alert")
async def receive_alert(
    flight_id: int,
    payload: AlertPayload,
    authorization: Optional[str] = Header(default=None),
):
    try:
        verify_publisher(authorization, flight_id)
    except AuthError as e:
        logger.warning(f"Rejected alert publish for flight {flight_id}: {e}")
        raise HTTPException(status_code=401, detail=str(e))

    await _manager.publish_alert(flight_id, payload.model_dump())
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run("main:app", host=WS_HOST, port=API_PORT, log_level="info")

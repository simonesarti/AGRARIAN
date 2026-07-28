"""
ws_server — WebSocket broadcaster.

The app POSTs alerts to POST /alert.
Viewer clients connect over WebSocket on WS_PORT.

Architecture
------------
  App process  ──POST /alert──►  ws_server:API_PORT  ──queue_alert()──►  WebSocketManager
  Viewer UI    ──WS────────────►  ws_server:WS_PORT
"""

import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel
from constants import API_PORT, WS_HOST, WS_PORT
from websocket_manager import WebSocketManager

# ── Config ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("ws_server")


ws_port = int(os.getenv("WS_PORT", WS_PORT))

# ── Singleton ─────────────────────────────────────────────────────────────────

_manager = WebSocketManager(host=WS_HOST, port=ws_port)

# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    _manager.start()
    logger.info(f"ws_server ready — WS on :{ws_port}, API on :{API_PORT}")
    yield
    # uvicorn is PID 1 (CMD ["uvicorn", ...] in Dockerfile), so Docker/Compose sends SIGTERM
    # directly to it on container stop. uvicorn finishes in-flight requests, then resumes this
    # coroutine past yield — which calls _manager.stop() to shut down the WebSocket thread.
    _manager.stop()

# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="WS Server", lifespan=lifespan)


class AlertPayload(BaseModel):
    model_config = {"extra": "allow"}   # accept any alert fields the app sends


@app.get("/health")
def health():
    return {
        "status": "ok",
        "connected_clients": len(_manager.connected_clients),
    }


@app.post("/alert")
def receive_alert(payload: AlertPayload):
    _manager.queue_alert(payload.model_dump())
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run("main:app", host=WS_HOST, port=API_PORT, log_level="info")

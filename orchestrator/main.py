"""
orchestrator — one GPU container per active flight.

MediaMTX calls this when a stream goes live and when it stops:

    runOnAvailable   ──POST /stream-online   key=<stream_key>──►  open flight, spawn
    runOnUnavailable ──POST /stream-offline  key=<stream_key>──►  stop, close flight

**Auth and spawn are separate events.** The MediaMTX auth hook fires on every
connection attempt, including aborted and retried ones; spawning from it would start
GPU containers for drones that never stream. Spawning belongs on availability, which
fires once the stream is actually carrying media.

INTERNAL ONLY. This service can start containers and, in the Docker backend, holds the
daemon socket. Its port must never be routed from outside the cluster network.
"""

import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Form

from constants import API_HOST, API_PORT, DEFAULT_SHM_SIZE, RECONNECT_GRACE_S
from db_writer_client import DbWriterClient
from flights import FlightOrchestrator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("orchestrator")

# ── Config ────────────────────────────────────────────────────────────────────

_APP_IMAGE      = os.environ["APP_IMAGE"]
_DB_WRITER_URL  = os.environ["DB_WRITER_URL"]
_APP_NETWORK    = os.environ.get("APP_NETWORK") or None
_APP_GPUS       = os.environ.get("APP_GPUS") or None
_APP_SHM_SIZE   = os.environ.get("APP_SHM_SIZE", DEFAULT_SHM_SIZE)
_GRACE_S        = float(os.environ.get("RECONNECT_GRACE_S", RECONNECT_GRACE_S))

# Deployment-wide settings passed through to every app container. Anything prefixed
# APP_ENV_ is forwarded with the prefix stripped, so the operator configures the app
# on the orchestrator without this service knowing what any of the settings mean.
# Per-flight values always win over these — see build_flight_env.
_BASE_ENV = {
    key[len("APP_ENV_"):]: value
    for key, value in os.environ.items()
    if key.startswith("APP_ENV_")
}

# ── Wiring ────────────────────────────────────────────────────────────────────

def _build_runtime():
    """
    Docker today, Kubernetes at deployment time. The lifecycle logic does not change
    when this does — that is the entire reason FlightRuntime exists.
    """
    from runtime import DockerFlightRuntime

    return DockerFlightRuntime(
        image=_APP_IMAGE,
        network=_APP_NETWORK,
        gpus=_APP_GPUS,
        shm_size=_APP_SHM_SIZE,
    )


_orchestrator = FlightOrchestrator(
    runtime=_build_runtime(),
    directory=DbWriterClient(_DB_WRITER_URL),
    base_env=_BASE_ENV,
    grace_s=_GRACE_S,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        f"orchestrator ready — image={_APP_IMAGE} network={_APP_NETWORK} "
        f"gpus={_APP_GPUS or 'none'} grace={_GRACE_S}s "
        f"forwarded settings={sorted(_BASE_ENV)}"
    )
    yield
    # Every flight container is a child of this service's bookkeeping, not of the
    # process, so they outlive it unless they are stopped explicitly.
    logger.info("Shutting down — stopping all active flights")
    await _orchestrator.shutdown()


app = FastAPI(title="Orchestrator", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok", "active_flights": _orchestrator.active,
            "flights": _orchestrator.snapshot()}


# MediaMTX's hooks post form-encoded bodies (wget --post-data), not JSON.

@app.post("/stream-online")
async def stream_online(key: str = Form(...)):
    flight_id = await _orchestrator.stream_online(key)
    return {"ok": flight_id is not None, "flight_id": flight_id}


@app.post("/stream-offline")
async def stream_offline(key: str = Form(...)):
    await _orchestrator.stream_offline(key)
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run("main:app", host=API_HOST, port=API_PORT, log_level="info")

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

from constants import (
    API_HOST,
    API_PORT,
    DEFAULT_NAMESPACE,
    DEFAULT_SHM_SIZE,
    RECONNECT_GRACE_S,
)
from db_writer_client import DbWriterClient
from flights import FlightOrchestrator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("orchestrator")

# ── Config ────────────────────────────────────────────────────────────────────

_RUNTIME        = os.environ.get("FLIGHT_RUNTIME", "docker").strip().lower()
_APP_IMAGE      = os.environ["APP_IMAGE"]
_DB_WRITER_URL  = os.environ["DB_WRITER_URL"]
_APP_NETWORK    = os.environ.get("APP_NETWORK") or None
_APP_GPUS       = os.environ.get("APP_GPUS") or None
_APP_SHM_SIZE   = os.environ.get("APP_SHM_SIZE", DEFAULT_SHM_SIZE)
_GRACE_S        = float(os.environ.get("RECONNECT_GRACE_S", RECONNECT_GRACE_S))

# Kubernetes backend only. APP_GPUS names cards on a known host and has no meaning
# here — the scheduler picks the node — so the count is a separate setting.
_APP_NAMESPACE  = os.environ.get("APP_NAMESPACE", DEFAULT_NAMESPACE)
_APP_GPU_COUNT  = int(os.environ.get("APP_GPU_COUNT", "0")) or None
_APP_PULL_SECRET = os.environ.get("APP_IMAGE_PULL_SECRET") or None
# key=value,key=value. A GPU node pool is normally both labelled and tainted, so
# reaching it usually needs the selector and the toleration together.
_APP_NODE_SELECTOR = os.environ.get("APP_NODE_SELECTOR") or ""
_APP_GPU_TOLERATION = os.environ.get("APP_GPU_TOLERATION", "").strip()

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

def _pairs(spec: str) -> dict:
    """'a=b,c=d' → {'a': 'b', 'c': 'd'}. Empty string → {}."""
    out = {}
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"expected key=value pairs, got {item!r}")
        key, value = item.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def _build_runtime():
    """
    Docker on a single host, Kubernetes in a cluster. The lifecycle logic does not
    change between them — that is the entire reason FlightRuntime exists, and both
    backends are now built against it.

    Docker stays the default because it is what a laptop and the compose stack in
    this repo can run. FLIGHT_RUNTIME=kubernetes is the deployment setting.
    """
    if _RUNTIME == "docker":
        from runtime import DockerFlightRuntime

        return DockerFlightRuntime(
            image=_APP_IMAGE,
            network=_APP_NETWORK,
            gpus=_APP_GPUS,
            shm_size=_APP_SHM_SIZE,
        )

    if _RUNTIME == "kubernetes":
        from runtime import KubernetesFlightRuntime

        tolerations = None
        if _APP_GPU_TOLERATION:
            # The convention every GPU node pool uses: the pool is tainted with a key
            # and ordinary workloads are kept off it. Only the key is configurable
            # because NoSchedule/Exists is what the taint is in practice, and a knob
            # nobody sets correctly is worse than a default that is right.
            tolerations = [{
                "key": _APP_GPU_TOLERATION,
                "operator": "Exists",
                "effect": "NoSchedule",
            }]

        return KubernetesFlightRuntime(
            image=_APP_IMAGE,
            namespace=_APP_NAMESPACE,
            gpu_count=_APP_GPU_COUNT,
            shm_size=_APP_SHM_SIZE,
            node_selector=_pairs(_APP_NODE_SELECTOR),
            tolerations=tolerations,
            image_pull_secret=_APP_PULL_SECRET,
        )

    # Fail at import rather than at the first flight. A typo here would otherwise
    # surface as a drone taking off and nothing happening.
    raise ValueError(
        f"FLIGHT_RUNTIME={_RUNTIME!r} is not one of: docker, kubernetes")


_orchestrator = FlightOrchestrator(
    runtime=_build_runtime(),
    directory=DbWriterClient(_DB_WRITER_URL),
    base_env=_BASE_ENV,
    grace_s=_GRACE_S,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs before uvicorn starts accepting connections, so a stream-online/offline
    # hook can never race the containers this reattaches to _flights.
    await _orchestrator.recover()
    # Only the settings the selected backend actually reads. Logging APP_NETWORK on a
    # cluster, or a namespace on a laptop, invites debugging a setting that was never
    # consulted.
    if _RUNTIME == "kubernetes":
        placement = (f"namespace={_APP_NAMESPACE} gpus={_APP_GPU_COUNT or 'none'} "
                     f"nodeSelector={_APP_NODE_SELECTOR or 'none'}")
    else:
        placement = f"network={_APP_NETWORK} gpus={_APP_GPUS or 'none'}"
    logger.info(
        f"orchestrator ready — runtime={_RUNTIME} image={_APP_IMAGE} {placement} "
        f"grace={_GRACE_S}s "
        f"forwarded settings={sorted(_BASE_ENV)} "
        f"recovered flights={_orchestrator.active}"
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

"""
Where a flight's GPU container actually runs.

The orchestrator's hard part is lifecycle logic — resolving a stream key, opening the
flight, injecting credentials, coping with a stream that drops and reconnects — and
none of it is platform-specific. Coupling that work to a Kubernetes cluster that does
not exist yet would block it behind an infrastructure decision, so it is written
against this two-method interface instead.

The Docker backend below runs on a laptop and needs no cloud account. The Kubernetes
backend (create Job, delete Job) is a comparable amount of code and lands at deployment
time; a flight is a finite workload, so it maps to a Job rather than a Deployment.
"""

import logging
from typing import Optional, Protocol

from constants import DEFAULT_SHM_SIZE, STOP_TIMEOUT_S

logger = logging.getLogger("orchestrator.runtime")


class FlightRuntime(Protocol):
    """One container per active flight. Nothing here knows what a flight *is*."""

    def start(self, flight_id: int, env: dict) -> str:
        """Launch the app for this flight and return an opaque handle."""
        ...

    def stop(self, handle: str) -> None:
        """Tear down whatever start() created. Must tolerate an already-gone handle."""
        ...


class DockerFlightRuntime:
    """
    Runs each flight as a container on the local Docker daemon.

    Requires /var/run/docker.sock. That is a real privilege — anything that can reach
    this service can start containers — which is why the orchestrator's port is
    internal-only and must never be routed from outside the cluster network.
    """

    def __init__(
            self,
            image: str,
            network: Optional[str] = None,
            gpus: Optional[str] = None,
            shm_size: str = DEFAULT_SHM_SIZE,
            extra_env: Optional[dict] = None,
    ):
        # Imported here rather than at module scope so the lifecycle logic and its
        # tests can be exercised with a fake runtime and no docker SDK installed.
        import docker

        self._client = docker.from_env()
        self._image = image
        self._network = network
        self._gpus = gpus
        self._shm_size = shm_size
        self._extra_env = extra_env or {}

    def _device_requests(self):
        if not self._gpus:
            return None
        from docker.types import DeviceRequest

        if self._gpus == "all":
            return [DeviceRequest(count=-1, capabilities=[["gpu"]])]
        return [DeviceRequest(device_ids=self._gpus.split(","), capabilities=[["gpu"]])]

    def start(self, flight_id: int, env: dict) -> str:
        # Named after the flight so an operator reading `docker ps` during an incident
        # can tell which container belongs to which flight without a lookup.
        name = f"agrarian-flight-{flight_id}"

        # A container left behind under this name would fail the run with a name
        # conflict and strand the flight.
        self._remove_if_present(name)

        container = self._client.containers.run(
            self._image,
            name=name,
            detach=True,
            environment={**self._extra_env, **env},
            network=self._network,
            device_requests=self._device_requests(),
            shm_size=self._shm_size,
            # No restart policy, deliberately. A flight is a finite workload: if the
            # app dies the stream is over, and Docker restarting it would reconnect to
            # an ingest path whose publisher has gone.
            restart_policy={"Name": "no"},
            labels={"agrarian.flight_id": str(flight_id)},
        )
        logger.info(f"Started {name} ({container.short_id}) for flight {flight_id}")
        return container.id

    def stop(self, handle: str) -> None:
        import docker.errors

        try:
            container = self._client.containers.get(handle)
        except docker.errors.NotFound:
            # Already gone: the app exited on its own, or a previous stop succeeded.
            # Teardown has to be idempotent — MediaMTX can deliver the offline hook
            # more than once and the flight still has to end cleanly.
            logger.info(f"Container {handle[:12]} already gone")
            return

        try:
            container.stop(timeout=STOP_TIMEOUT_S)
            logger.info(f"Stopped container {handle[:12]}")
        except Exception as e:
            logger.error(f"Failed to stop container {handle[:12]}: {e}")
        finally:
            try:
                container.remove(force=True)
            except Exception as e:
                logger.warning(f"Could not remove container {handle[:12]}: {e}")

    def _remove_if_present(self, name: str) -> None:
        import docker.errors

        try:
            stale = self._client.containers.get(name)
        except docker.errors.NotFound:
            return
        logger.warning(f"Removing stale container {name} left by a previous run")
        try:
            stale.remove(force=True)
        except Exception as e:
            logger.error(f"Could not remove stale container {name}: {e}")

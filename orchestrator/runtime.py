"""
Where a flight's GPU container actually runs.

The orchestrator's hard part is lifecycle logic — resolving a stream key, opening the
flight, injecting credentials, coping with a stream that drops and reconnects — and
none of it is platform-specific. Coupling that work to a Kubernetes cluster that did
not exist yet would have blocked it behind an infrastructure decision, so it is written
against this interface instead.

Both backends are now here. The Docker one runs on a laptop and needs no cloud account.
The Kubernetes one creates a Job per flight — a flight is a finite workload, so it maps
to a Job rather than a Deployment — and is what makes the GPU node pool at min = 0 in
§2 possible, since a Job that nothing is running lets the pool scale to nothing.

The two differ in exactly one thing the caller can observe, and it is worth stating
because it is the hinge the whole abstraction turns on: **`stop()` is synchronous under
Docker and asynchronous under Kubernetes.** Docker blocks until the container is down;
the API server accepts a deletion and returns before any pod has died. Nothing in
flights.py depends on the difference — teardown closes the flight row either way — but
anything that later wants "the GPU is definitely free" needs to ask, not assume.
"""

import logging
import re
import time
from typing import Optional, Protocol

from constants import (
    DEFAULT_SHM_SIZE,
    FLIGHT_LABEL,
    JOB_DELETE_TIMEOUT_S,
    STOP_TIMEOUT_S,
)

logger = logging.getLogger("orchestrator.runtime")


class FlightRuntime(Protocol):
    """One container per active flight. Nothing here knows what a flight *is*."""

    def start(self, flight_id: int, env: dict) -> str:
        """Launch the app for this flight and return an opaque handle."""
        ...

    def stop(self, handle: str) -> None:
        """Tear down whatever start() created. Must tolerate an already-gone handle."""
        ...

    def list_managed(self) -> list:
        """
        Everything this runtime has ever started for a flight, running or not —
        containers under Docker, Jobs under Kubernetes.

        Used once at startup to recover bookkeeping a crash lost: the label and the
        environment start() set are the only state that survives the orchestrator's
        own process dying without a chance to run its shutdown hook. Each entry is
        {"handle": str, "running": bool, "env": dict}.
        """
        ...


class DockerFlightRuntime:
    """
    Runs each flight as a container on the local Docker daemon.

    Requires /var/run/docker.sock. That is a real privilege — anything that can reach
    this service can start containers — which is why the orchestrator's port is
    internal-only and must never be routed from outside the cluster network.

    KubernetesFlightRuntime below is the answer to that privilege rather than another
    instance of it: the socket becomes a service account that may create Jobs in one
    namespace and nothing else. See configs/k8s/orchestrator-rbac.yaml.
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
            labels={FLIGHT_LABEL: str(flight_id)},
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

    def list_managed(self) -> list:
        # sparse=False (the default) is load-bearing: it makes docker-py inspect
        # each container, which is what puts Config.Env — the only place
        # PUBLISHER_TOKEN and the stream paths survive a restart — into .attrs.
        # The plain list API this filters does not include it.
        containers = self._client.containers.list(all=True, filters={"label": FLIGHT_LABEL})
        managed = []
        for container in containers:
            env = dict(
                item.split("=", 1) for item in container.attrs["Config"]["Env"] if "=" in item
            )
            managed.append({
                "handle": container.id,
                "running": container.status == "running",
                "env": env,
            })
        return managed

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


# ── Kubernetes ────────────────────────────────────────────────────────────────

# Docker accepts 256m / 1g; Kubernetes wants 256Mi / 1Gi. One APP_SHM_SIZE setting
# has to mean the same thing under both backends, so it is written in Docker's
# spelling — which is what .env.example already documents — and translated here.
_DOCKER_SIZE = re.compile(r"^(\d+)\s*([bkmg])?$", re.IGNORECASE)
_QUANTITY_SUFFIX = {"b": "", "k": "Ki", "m": "Mi", "g": "Gi"}


def docker_size_to_quantity(size: str) -> str:
    """'256m' → '256Mi'. Raises rather than guessing: a silently wrong /dev/shm is
    the SIGBUS this setting exists to prevent, three frames into the flight."""
    match = _DOCKER_SIZE.match(size.strip())
    if not match:
        raise ValueError(
            f"APP_SHM_SIZE={size!r} is not a Docker size like '256m' or '1g'")
    return match.group(1) + _QUANTITY_SUFFIX[(match.group(2) or "b").lower()]


class KubernetesFlightRuntime:
    """
    Runs each flight as a Kubernetes Job.

    A Job, not a Deployment, because a flight ends: the drone lands, the publisher
    goes away, and the workload is *supposed* to terminate. A Deployment would
    restart the app forever against an ingest path whose publisher has gone.

    What this backend buys, beyond running in a cluster at all:

    - **The Docker socket goes away.** The Docker backend needs /var/run/docker.sock,
      which is root on the host for anything that can reach the orchestrator's port.
      Here the equivalent authority is a ServiceAccount bound to a Role that may
      touch Jobs in one namespace — see configs/k8s/orchestrator-rbac.yaml. That is
      the strongest single argument for the migration and it is the reason this class
      exists, so it is worth being precise: the privilege is not reduced, it is
      *scoped*. A compromised orchestrator can still start GPU workloads. It can no
      longer start a privileged container on the host.
    - **GPU nodes can scale to zero.** Requesting nvidia.com/gpu on a Job the cluster
      cannot currently place is what makes the node pool create a machine, and a
      finished Job is what lets it destroy one. Under Docker the GPU is rented
      whether or not anything is flying (§2).

    Deliberately NOT set, both times because the orchestrator's recovery path depends
    on it:

    - `ttlSecondsAfterFinished`. A finished Job is the only record that a flight's
      container exited while the orchestrator was down; recover() reads it, closes
      the flight row and deletes it. Let the cluster garbage-collect these and
      flights whose app died during an outage keep an open-ended end_time forever.
      Jobs are cleaned up by stop(), which is the same contract the Docker backend
      has — nothing accumulates as long as the orchestrator runs.
    - A restart policy of anything but `Never`, and `backoffLimit` above 0. Same
      reasoning as the Docker backend's restart_policy={"Name": "no"}: a restarted
      app reconnects to an ingest path that no longer has a publisher.
    """

    def __init__(
            self,
            image: str,
            namespace: str,
            gpu_count: Optional[int] = None,
            shm_size: str = DEFAULT_SHM_SIZE,
            node_selector: Optional[dict] = None,
            tolerations: Optional[list] = None,
            image_pull_secret: Optional[str] = None,
            extra_env: Optional[dict] = None,
    ):
        # Imported here rather than at module scope for the same reason the docker
        # SDK is: the lifecycle logic and its tests run against a fake runtime, and
        # neither client should be a hard import for someone running the other backend.
        from kubernetes import client, config

        try:
            # The normal case: the orchestrator is itself a pod, and this reads the
            # service account token the RBAC manifest granted it.
            config.load_incluster_config()
            logger.info("Using in-cluster Kubernetes credentials")
        except config.ConfigException:
            # Out-of-cluster: a kubeconfig, which is how the test harness and anyone
            # debugging against a local cluster reach the API server.
            config.load_kube_config()
            logger.info("Using kubeconfig credentials (not running in-cluster)")

        self._batch = client.BatchV1Api()
        self._image = image
        self._namespace = namespace
        self._gpu_count = gpu_count
        self._shm_quantity = docker_size_to_quantity(shm_size)
        self._node_selector = node_selector or None
        self._tolerations = tolerations or None
        self._image_pull_secret = image_pull_secret
        self._extra_env = extra_env or {}

    # ── Spec construction ─────────────────────────────────────────────────────

    def _container_spec(self, env: dict) -> dict:
        container = {
            "name": "app",
            "image": self._image,
            # Every value stringified: the API server rejects a non-string env value
            # outright, and base_env comes from os.environ so this only bites on a
            # per-flight int like FLIGHT_ID.
            "env": [{"name": k, "value": str(v)} for k, v in sorted(env.items())],
            # The Kubernetes spelling of Docker's --shm-size. Without it a pod gets
            # the container runtime's 64 MB /dev/shm and the annotation worker takes
            # a silent SIGBUS a few frames in — the same failure, on a platform where
            # it is far harder to see.
            "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
        }
        if self._gpu_count:
            # A count, not device IDs. This is the one setting that genuinely does not
            # translate from the Docker backend, where APP_GPUS names cards on a known
            # host; here the scheduler chooses the node and the device plugin chooses
            # the card, which is the entire point of the node pool.
            container["resources"] = {
                "limits": {"nvidia.com/gpu": str(self._gpu_count)},
            }
        return container

    def _job_body(self, flight_id: int, name: str, env: dict) -> dict:
        labels = {FLIGHT_LABEL: str(flight_id), "app.kubernetes.io/name": "agrarian-flight"}

        pod_spec = {
            "restartPolicy": "Never",
            # Mirrors the Docker backend's STOP_TIMEOUT_S: SIGTERM, then this long to
            # drain the queues, then SIGKILL. The app installs handlers and flushes.
            "terminationGracePeriodSeconds": STOP_TIMEOUT_S,
            "containers": [self._container_spec(env)],
            "volumes": [{
                "name": "dshm",
                "emptyDir": {"medium": "Memory", "sizeLimit": self._shm_quantity},
            }],
        }
        if self._node_selector:
            pod_spec["nodeSelector"] = self._node_selector
        if self._tolerations:
            # GPU node pools are routinely tainted so that ordinary workloads do not
            # land on an expensive machine. Without a matching toleration the Job
            # stays Pending forever and the flight never starts.
            pod_spec["tolerations"] = self._tolerations
        if self._image_pull_secret:
            pod_spec["imagePullSecrets"] = [{"name": self._image_pull_secret}]

        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": name, "labels": labels},
            "spec": {
                "backoffLimit": 0,
                "completions": 1,
                "parallelism": 1,
                "template": {"metadata": {"labels": labels}, "spec": pod_spec},
            },
        }

    # ── FlightRuntime ─────────────────────────────────────────────────────────

    def start(self, flight_id: int, env: dict) -> str:
        # Same naming as the Docker backend, for the same reason: `kubectl get jobs`
        # during an incident should say which flight each one belongs to.
        name = f"agrarian-flight-{flight_id}"
        self._delete_if_present(name)

        body = self._job_body(flight_id, name, {**self._extra_env, **env})
        self._batch.create_namespaced_job(namespace=self._namespace, body=body)

        handle = f"{self._namespace}/{name}"
        logger.info(f"Created Job {handle} for flight {flight_id}")
        # Note what has and has not happened: the API server has accepted a Job. No
        # pod is running yet, and on a scaled-to-zero pool none will be for minutes
        # while a GPU node boots. That is why list_managed() treats a Job with no
        # terminal state as running rather than as finished.
        return handle

    def stop(self, handle: str) -> None:
        from kubernetes.client.exceptions import ApiException

        namespace, name = self._split(handle)
        try:
            self._batch.delete_namespaced_job(
                name=name,
                namespace=namespace,
                # Without this the Job object is deleted and its pods are orphaned —
                # a GPU pod running forever with nothing owning it, which is the
                # single most expensive mistake available in this file.
                #
                # Note where the app's shutdown window comes from, because it is NOT
                # here: gracePeriodSeconds on this call applies to the Job object,
                # and the garbage collector deletes the pods with their own defaults.
                # What actually gives the app STOP_TIMEOUT_S to drain its queues is
                # terminationGracePeriodSeconds in the pod spec.
                propagation_policy="Background",
            )
            logger.info(f"Deleted Job {handle}")
        except ApiException as e:
            if e.status == 404:
                # Already gone: the app exited and something cleaned up, or a previous
                # stop succeeded. Teardown has to be idempotent — MediaMTX can deliver
                # the offline hook more than once and the flight still has to end.
                logger.info(f"Job {handle} already gone")
                return
            logger.error(f"Failed to delete Job {handle}: {e.status} {e.reason}")

    def list_managed(self) -> list:
        jobs = self._batch.list_namespaced_job(
            namespace=self._namespace, label_selector=FLIGHT_LABEL)
        managed = []
        for job in jobs.items:
            container = job.spec.template.spec.containers[0]
            # valueFrom entries have no .value; there are none here, but a spec edited
            # by hand or by a mutating webhook should not crash recovery.
            env = {e.name: e.value for e in (container.env or []) if e.value is not None}
            managed.append({
                "handle": f"{job.metadata.namespace}/{job.metadata.name}",
                "running": self._is_running(job),
                "env": env,
            })
        return managed

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _is_running(job) -> bool:
        """
        The Kubernetes answer to Docker's `container.status == "running"`.

        Not the same question, and the difference decides what recover() does with a
        flight after an orchestrator restart. A Job that has neither succeeded nor
        failed may be running, or may be Pending while the node pool boots a GPU
        machine — and those are indistinguishable from here without listing pods.

        Treating "not finished" as running is the safe direction. Call a Pending Job
        finished and recover() closes a flight that is about to start streaming, and
        the orchestrator then ignores its offline hook; call a genuinely finished one
        running and the next offline hook tears it down a few seconds late. The first
        loses a flight, the second costs nothing.
        """
        status = job.status
        return not (status.succeeded or status.failed)

    def _split(self, handle: str) -> tuple:
        if "/" in handle:
            namespace, name = handle.rsplit("/", 1)
            return namespace, name
        # A handle from an older release, or a hand-written one.
        return self._namespace, handle

    def _delete_if_present(self, name: str) -> None:
        """
        A Job left under this name would fail create with 409 AlreadyExists and strand
        the flight, exactly as a stale container name does under Docker.

        Unlike Docker's remove, deletion here is asynchronous — the API server records
        an intent and returns — so this waits for the name to actually be free rather
        than racing the create against the garbage collector.
        """
        from kubernetes.client.exceptions import ApiException

        try:
            self._batch.read_namespaced_job(name=name, namespace=self._namespace)
        except ApiException as e:
            if e.status == 404:
                return
            raise

        logger.warning(f"Removing stale Job {name} left by a previous run")
        try:
            self._batch.delete_namespaced_job(
                name=name, namespace=self._namespace, propagation_policy="Background")
        except ApiException as e:
            if e.status != 404:
                logger.error(f"Could not delete stale Job {name}: {e.status} {e.reason}")
                return

        deadline = time.monotonic() + JOB_DELETE_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                self._batch.read_namespaced_job(name=name, namespace=self._namespace)
            except ApiException as e:
                if e.status == 404:
                    return
                raise
            time.sleep(0.5)

        # Let create() raise the 409 rather than pretending this worked. The flight
        # fails to start, flights.py closes the row, and the operator gets a name.
        logger.error(f"Stale Job {name} still present after {JOB_DELETE_TIMEOUT_S}s")

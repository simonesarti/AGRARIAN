"""
KubernetesFlightRuntime against a real API server.

Run by run_k8s_runtime.sh, inside a container, **holding the orchestrator's own
service-account token** rather than an admin kubeconfig. That is not a detail: every
call below is simultaneously a test of the runtime and a test of
configs/k8s/orchestrator-rbac.yaml, because a Role missing a verb fails here as a 403.
A cluster-admin kubeconfig would pass this file with the manifest deleted.

What is deliberately NOT here: anything about pods. The Role grants no access to them
(on purpose — see the manifest), so "did a container actually run, with this /dev/shm
and this environment" is checked by the runner with an operator's credentials. This
file is the control plane; the runner is the data plane.

Needs: a cluster and KUBECONFIG. See README.md.
"""
import os
import sys
import time

sys.path.insert(0, os.environ.get("ORCHESTRATOR_DIR", "/orchestrator"))

from constants import STOP_TIMEOUT_S  # noqa: E402
from runtime import KubernetesFlightRuntime, docker_size_to_quantity  # noqa: E402

NAMESPACE = os.environ.get("APP_NAMESPACE", "agrarian-flights")
STUB_IMAGE = os.environ.get("STUB_IMAGE", "k8srt-stub:test")
QUICK_IMAGE = os.environ.get("QUICK_IMAGE", "k8srt-quick:test")

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""))


def make(image=STUB_IMAGE, **kw):
    return KubernetesFlightRuntime(image=image, namespace=NAMESPACE, **kw)


rt = make()
batch = rt._batch  # the same client, so a 403 here is a 403 the runtime would get


def job(name):
    return batch.read_namespaced_job(name=name, namespace=NAMESPACE)


def gone(name, timeout=60):
    from kubernetes.client.exceptions import ApiException

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            batch.read_namespaced_job(name=name, namespace=NAMESPACE)
        except ApiException as e:
            if e.status == 404:
                return True
            raise
        time.sleep(0.5)
    return False


# ── Size translation ──────────────────────────────────────────────────────────
# One APP_SHM_SIZE has to mean the same thing under both backends. Getting this
# wrong is not a config error, it is a SIGBUS three frames into a flight.

check("256m converts to 256Mi", docker_size_to_quantity("256m") == "256Mi")
check("1g converts to 1Gi", docker_size_to_quantity("1g") == "1Gi")
check("512K converts to 512Ki", docker_size_to_quantity("512K") == "512Ki")
check("a bare byte count survives", docker_size_to_quantity("1048576") == "1048576")

for bad in ("256mb", "lots", "-1m", ""):
    try:
        docker_size_to_quantity(bad)
        check(f"{bad!r} is rejected", False, "accepted silently")
    except ValueError:
        check(f"{bad!r} is rejected rather than guessed at", True)


# ── start(): the Job the cluster actually accepted ────────────────────────────

ENV = {
    "FLIGHT_ID": "1001",
    "PUBLISHER_TOKEN": "ey.a-publisher-token",
    "VIDEO_STREAM_READER_STREAM_KEY": "in/abcdefgh12345678",
    "VIDEO_OUT_STREAM_STREAM_KEY": "out/a-public-uuid",
    "TELEMETRY_LISTENER_STREAM_KEY": "abcdefgh12345678",
    "WS_SERVER_URL": "http://ws-server:8000",
}

handle = rt.start(1001, ENV)
check("start returns a namespaced handle", handle == f"{NAMESPACE}/agrarian-flight-1001",
      handle)

j = job("agrarian-flight-1001")
spec = j.spec.template.spec
container = spec.containers[0]

check("the Job exists in the API server", j.metadata.name == "agrarian-flight-1001")
check("it carries the flight label recovery searches on",
      j.metadata.labels.get("agrarian.flight_id") == "1001")
check("the container runs the configured image", container.image == STUB_IMAGE)

# A restarted app reconnects to an ingest path whose publisher has gone. Both of
# these have to hold — restartPolicy alone still lets the Job make a NEW pod.
check("restartPolicy is Never", spec.restart_policy == "Never")
check("backoffLimit is 0 — a dead flight is not retried", j.spec.backoff_limit == 0)

# recover() reads finished Jobs to close flight rows whose app died during an
# outage. Let the cluster garbage-collect them and those rows stay open forever.
check("ttlSecondsAfterFinished is unset, so recover() can still see a finished Job",
      j.spec.ttl_seconds_after_finished is None)

check("the app gets STOP_TIMEOUT_S to drain before SIGKILL",
      spec.termination_grace_period_seconds == STOP_TIMEOUT_S,
      str(spec.termination_grace_period_seconds))

volume = spec.volumes[0]
check("/dev/shm is a memory-backed emptyDir", volume.empty_dir.medium == "Memory")
check("sized from APP_SHM_SIZE, not the runtime's 64 MB default",
      volume.empty_dir.size_limit == "256Mi", str(volume.empty_dir.size_limit))
check("and it is mounted at /dev/shm",
      container.volume_mounts[0].mount_path == "/dev/shm")

env_in_spec = {e.name: e.value for e in container.env}
for key, value in ENV.items():
    check(f"{key} injected", env_in_spec.get(key) == value)
check("NO end-user credentials in the Job spec",
      not any(k in env_in_spec for k in ("DB_USERNAME", "DB_PASSWORD")))


# ── list_managed(): the state a crash cannot lose ─────────────────────────────

managed = {m["handle"]: m for m in rt.list_managed()}
check("the running flight is listed", handle in managed)
entry = managed.get(handle, {})
check("its env round-trips, which is what recover() rebuilds from",
      entry.get("env", {}).get("PUBLISHER_TOKEN") == ENV["PUBLISHER_TOKEN"])
check("a Job with no terminal state counts as running", entry.get("running") is True)


# ── A second start() for the same flight ──────────────────────────────────────
# A leftover Job under this name would fail create with 409 and strand the flight.

handle_again = rt.start(1001, {**ENV, "PUBLISHER_TOKEN": "ey.a-second-token"})
check("starting over a stale Job of the same name succeeds", handle_again == handle)
check("the replacement carries the NEW environment",
      {e.name: e.value for e in
       job("agrarian-flight-1001").spec.template.spec.containers[0].env
       }["PUBLISHER_TOKEN"] == "ey.a-second-token")
check("exactly one Job for this flight, not two",
      sum(1 for m in rt.list_managed() if m["handle"] == handle) == 1)


# ── GPU, placement and pull secrets ───────────────────────────────────────────
# These are the settings that do NOT translate from the Docker backend, so they are
# checked as spec rather than assumed.

check("no GPU requested when the count is unset",
      job("agrarian-flight-1001").spec.template.spec.containers[0].resources.limits
      in (None, {}))

gpu_rt = make(gpu_count=2,
              node_selector={"agrarian.io/pool": "gpu"},
              tolerations=[{"key": "nvidia.com/gpu", "operator": "Exists",
                            "effect": "NoSchedule"}],
              image_pull_secret="registry-creds")
# Not started: a two-GPU pod is unschedulable on this cluster and would sit Pending.
# The spec is what the API server would receive, which is the thing under test.
body = gpu_rt._job_body(9, "agrarian-flight-9", {"FLIGHT_ID": "9"})
pod = body["spec"]["template"]["spec"]
check("a GPU count becomes an nvidia.com/gpu limit",
      pod["containers"][0]["resources"]["limits"]["nvidia.com/gpu"] == "2")
check("the node selector reaches the pod spec",
      pod["nodeSelector"] == {"agrarian.io/pool": "gpu"})
check("the GPU taint is tolerated, or the flight sits Pending forever",
      pod["tolerations"][0]["key"] == "nvidia.com/gpu")
check("an image pull secret is carried through",
      pod["imagePullSecrets"] == [{"name": "registry-creds"}])


# ── A finished flight ─────────────────────────────────────────────────────────
# The recovery case that matters: the app exited while the orchestrator was down.
# recover() must see running=False and close the flight row rather than adopting it.

quick = make(image=QUICK_IMAGE)
quick_handle = quick.start(1002, {"FLIGHT_ID": "1002", "PUBLISHER_TOKEN": "tok-1002"})

deadline = time.monotonic() + 180
finished = None
while time.monotonic() < deadline:
    finished = next((m for m in quick.list_managed() if m["handle"] == quick_handle), None)
    if finished and not finished["running"]:
        break
    time.sleep(2)

check("a Job whose container exited reports running=False",
      bool(finished) and not finished["running"])
check("its env is still readable, so the flight row can be closed",
      bool(finished) and finished["env"].get("PUBLISHER_TOKEN") == "tok-1002")


# ── stop() ────────────────────────────────────────────────────────────────────

rt.stop(handle)
check("stop removes the Job", gone("agrarian-flight-1001"))
check("and it leaves list_managed",
      all(m["handle"] != handle for m in rt.list_managed()))

# MediaMTX delivers the offline hook more than once. A raising stop() would abort
# teardown and leave the flight row open.
try:
    rt.stop(handle)
    check("stopping an already-gone flight does not raise", True)
except Exception as e:
    check("stopping an already-gone flight does not raise", False, repr(e))

# A handle written before handles carried a namespace, or one typed by hand.
bare = rt.start(1003, {"FLIGHT_ID": "1003"})
rt.stop(bare.split("/", 1)[1])
check("a handle without a namespace prefix still resolves", gone("agrarian-flight-1003"))

quick.stop(quick_handle)
check("a finished Job can be cleaned up too", gone("agrarian-flight-1002"))


print()
print("=" * 58)
passed = sum(1 for _, ok, _ in results if ok)
print(f"{passed}/{len(results)} passed")
# Machine-readable, so the runner can fold these into its own tally.
print(f"TALLY {passed} {len(results) - passed}")
sys.exit(0 if passed == len(results) else 1)

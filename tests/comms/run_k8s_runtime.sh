#!/usr/bin/env bash
# The Kubernetes FlightRuntime backend, against a real API server.
#
# test_orchestrator.py proves the lifecycle logic with a fake runtime; run_orchestrator.sh
# proves the Docker backend really starts containers. This is the third leg: a real
# cluster, a real scheduler and a real kubelet, because the things that break in a
# Kubernetes backend are the things a mock cannot have an opinion about — whether the
# API server accepts the Job spec, whether a pod is schedulable, whether /dev/shm ends
# up the size it was asked for.
#
# The cluster is k3s in a container. It is a real Kubernetes distribution — same API
# server, same scheduler, same kubelet — and it starts in seconds, so there is no
# reason to test this against a fake.
#
# TWO SETS OF CREDENTIALS, ON PURPOSE
# -----------------------------------
# The runtime runs under the orchestrator's own **service-account token**, so every
# call it makes is also a test of configs/k8s/orchestrator-rbac.yaml: a missing verb
# fails as a 403 rather than passing quietly. The pod-level checks below run under the
# admin kubeconfig, because the Role deliberately grants no access to pods.
#
# THE GPU IS NOT UNDER TEST
# -------------------------
# There is no GPU here and no device plugin, so the stand-in "app" is an image that
# sleeps — the same choice run_orchestrator.sh makes, for the same reason. What is
# under test is placement and lifecycle, which is exactly what FlightRuntime is for.
#
# Needs: Docker, and enough disk for the k3s image. Nothing else — no cluster, no
# cloud account, no kubectl on the host.
#
# Usage:  ./run_k8s_runtime.sh
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

K3S_IMAGE=rancher/k3s:v1.31.5-k3s1
NET=k8srt-net
SERVER=k8srt-server
NS=agrarian-flights
STUB_IMAGE=k8srt-stub:test
QUICK_IMAGE=k8srt-quick:test
FAIL_IMAGE=k8srt-fail:test
CLIENT_IMAGE=k8srt-client:test

WORK="$(mktemp -d)"

cleanup() {
  docker rm -f "$SERVER" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  docker rmi "$STUB_IMAGE" "$QUICK_IMAGE" "$FAIL_IMAGE" "$CLIENT_IMAGE" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

pass=0; fail=0
check() {  # check <name> <expected> <actual>
  if [ "$2" = "$3" ]; then echo "PASS  $1   [$3]"; pass=$((pass+1))
  else echo "FAIL  $1   [expected $2, got $3]"; fail=$((fail+1)); fi
}

kc() { docker exec "$SERVER" kubectl "$@"; }

# ── The cluster ───────────────────────────────────────────────────────────────

echo "==> starting k3s"
docker network create "$NET" >/dev/null 2>&1 || true
# --privileged because a kubelet manages cgroups and mounts.
#
# The eviction thresholds are lowered from the 10% default for one reason: the node's
# filesystem IS the host's, so a developer machine with a fullish disk puts the node
# under DiskPressure and every flight pod sits Pending with an untolerated taint —
# which looks exactly like a broken Job spec and is not. Nothing under test here has
# an opinion about disk.
docker run -d --name "$SERVER" --privileged --network "$NET" \
  --tmpfs /run --tmpfs /var/run -e K3S_KUBECONFIG_MODE=644 \
  "$K3S_IMAGE" server \
  --disable=traefik --disable=servicelb --disable=metrics-server --disable=local-storage \
  --tls-san="$SERVER" \
  '--kubelet-arg=eviction-hard=nodefs.available<1%,imagefs.available<1%,nodefs.inodesFree<1%' \
  >/dev/null || { echo "k3s failed to start (is a previous run still up?)"; exit 1; }

for _ in $(seq 1 90); do
  kc get nodes 2>/dev/null | grep -q ' Ready' && break
  sleep 2
done
kc get nodes 2>/dev/null | grep -q ' Ready' || {
  echo "the k3s node never became Ready:"; docker logs "$SERVER" 2>&1 | tail -30; exit 1; }

# ── Images ────────────────────────────────────────────────────────────────────
# Imported into the cluster's containerd rather than pulled: these are local images
# and no registry has heard of them. The tags are deliberately not ':latest', which
# would set imagePullPolicy: Always and send the kubelet looking for a registry.

echo "==> building the stand-in app images"
build_stub() {  # build_stub <tag> <CMD line>
  local ctx; ctx=$(mktemp -d)
  printf 'FROM alpine:3.20\nCMD %s\n' "$2" > "$ctx/Dockerfile"
  docker build -q -t "$1" "$ctx" >/dev/null || { rm -rf "$ctx"; return 1; }
  rm -rf "$ctx"
  docker save "$1" | docker exec -i "$SERVER" ctr -n k8s.io images import - >/dev/null
}
# Traps SIGTERM for the same reason run_orchestrator.sh's stub does: a bare sleep as
# PID 1 ignores signals it has no handler for, so teardown would block for the full
# grace period and look like a hung runtime. The real app installs handlers.
build_stub "$STUB_IMAGE"  '["sh","-c","trap '"'"'exit 0'"'"' TERM INT; while :; do sleep 0.2; done"]' || exit 1
build_stub "$QUICK_IMAGE" '["sh","-c","sleep 2"]' || exit 1
build_stub "$FAIL_IMAGE"  '["sh","-c","exit 1"]' || exit 1

echo "==> building the client image"
CTX=$(mktemp -d)
printf 'FROM python:3.12-slim\nRUN pip install --no-cache-dir kubernetes==31.0.0\n' > "$CTX/Dockerfile"
docker build -q -t "$CLIENT_IMAGE" "$CTX" >/dev/null || { rm -rf "$CTX"; exit 1; }
rm -rf "$CTX"

# ── Credentials ───────────────────────────────────────────────────────────────

echo "==> applying configs/k8s/orchestrator-rbac.yaml"
docker exec -i "$SERVER" kubectl apply -f - < "$REPO/configs/k8s/orchestrator-rbac.yaml" >/dev/null \
  || { echo "the RBAC manifest did not apply"; exit 1; }

CA=$(kc config view --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' 2>/dev/null)
TOKEN=$(kc create token agrarian-orchestrator -n agrarian --duration=2h 2>/dev/null)
[ -n "$TOKEN" ] || { echo "could not mint a service-account token"; exit 1; }

mkdir -p "$WORK/kube"
cat > "$WORK/kube/config" <<EOF
apiVersion: v1
kind: Config
clusters:
- name: k3s
  cluster:
    certificate-authority-data: $CA
    server: https://$SERVER:6443
contexts:
- name: k3s
  context: {cluster: k3s, user: orchestrator}
current-context: k3s
users:
- name: orchestrator
  user: {token: $TOKEN}
EOF

# Everything the runtime does runs through this: the orchestrator's token, nothing more.
as_orchestrator() {  # as_orchestrator <script under $WORK> [args...]
  docker run --rm --network "$NET" -i \
    -e KUBECONFIG=/kube/config -e ORCHESTRATOR_DIR=/orchestrator \
    -e APP_NAMESPACE="$NS" -e STUB_IMAGE="$STUB_IMAGE" -e QUICK_IMAGE="$QUICK_IMAGE" \
    -v "$WORK/kube:/kube:ro" -v "$WORK:/work:ro" \
    -v "$REPO/orchestrator:/orchestrator:ro" \
    -v "$REPO/tests/comms:/tests:ro" \
    "$CLIENT_IMAGE" python "$@"
}

# ── The control plane, under the orchestrator's own token ─────────────────────

echo
echo "── the runtime against a real API server (service-account credentials) ────"
OUT=$(as_orchestrator /tests/test_k8s_runtime.py 2>&1)
echo "$OUT" | grep -v '^TALLY '
TALLY=$(echo "$OUT" | grep '^TALLY ' | tail -1)
if [ -n "$TALLY" ]; then
  pass=$((pass + $(echo "$TALLY" | awk '{print $2}')))
  fail=$((fail + $(echo "$TALLY" | awk '{print $3}')))
else
  echo "FAIL  test_k8s_runtime.py did not run to completion"
  fail=$((fail+1))
fi

# ── The data plane: a container really ran, with what it was given ────────────
# The Role grants no access to pods, so these use the admin kubeconfig. That split
# is the point of the manifest, not a limitation of the test.

echo
echo "── a flight Job actually becomes a running container ──────────────────────"
cat > "$WORK/start.py" <<'EOF'
import os, sys
sys.path.insert(0, "/orchestrator")
from runtime import KubernetesFlightRuntime
rt = KubernetesFlightRuntime(image=os.environ["STUB_IMAGE"],
                             namespace=os.environ["APP_NAMESPACE"])
print(rt.start(2001, {"FLIGHT_ID": "2001", "PUBLISHER_TOKEN": "ey.data-plane-token",
                      "VIDEO_OUT_STREAM_STREAM_KEY": "out/a-public-uuid"}))
EOF
HANDLE=$(as_orchestrator /work/start.py 2>&1 | tail -1)
check "start() returned a handle" "$NS/agrarian-flight-2001" "$HANDLE"

wait_phase() {  # wait_phase <selector> <phase>
  for _ in $(seq 1 60); do
    [ "$(kc get pods -n "$NS" -l "$1" -o jsonpath='{.items[0].status.phase}' 2>/dev/null)" = "$2" ] && return 0
    sleep 2
  done
  return 1
}
wait_phase "agrarian.flight_id=2001" Running
POD=$(kc get pods -n "$NS" -l agrarian.flight_id=2001 -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
check "the scheduler placed a pod and it is Running" Running \
  "$(kc get pods -n "$NS" -l agrarian.flight_id=2001 -o jsonpath='{.items[0].status.phase}' 2>/dev/null)"

echo
echo "── the container received the flight's identity, and no user credentials ──"
ENVOUT=$(kc exec -n "$NS" "$POD" -- printenv 2>/dev/null)
check "FLIGHT_ID reached the process"    "FLIGHT_ID=2001" "$(echo "$ENVOUT" | grep '^FLIGHT_ID=')"
check "the publisher token reached it"   "PUBLISHER_TOKEN=ey.data-plane-token" "$(echo "$ENVOUT" | grep '^PUBLISHER_TOKEN=')"
check "the output path reached it"       "VIDEO_OUT_STREAM_STREAM_KEY=out/a-public-uuid" "$(echo "$ENVOUT" | grep '^VIDEO_OUT_STREAM_STREAM_KEY=')"
check "NO end-user credentials in the pod" 0 "$(echo "$ENVOUT" | grep -cE '^(DB_USERNAME|DB_PASSWORD)=.+')"

echo
echo "── /dev/shm is the size the pipeline needs, with the default as the control ─"
# The whole reason this is checked at all: the annotation worker takes a silent
# SIGBUS a few frames in on the container runtime's 64 MB default, once the frame
# buffers live in shared memory. The control below is what makes the first line a
# statement about the emptyDir rather than about Alpine.
SHM=$(kc exec -n "$NS" "$POD" -- df -m /dev/shm 2>/dev/null | awk 'NR==2 {print $2}')
check "a flight pod gets 256 MB of /dev/shm" 256 "$SHM"

# docker exec -i, not kc: kc has no stdin, so a heredoc would silently create nothing
# and the control would read as an empty answer rather than as a failure.
docker exec -i "$SERVER" kubectl apply -f - >/dev/null 2>&1 <<EOF
apiVersion: batch/v1
kind: Job
metadata: {name: shm-control, namespace: $NS}
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers: [{name: app, image: "$STUB_IMAGE"}]
EOF
wait_phase "job-name=shm-control" Running
CPOD=$(kc get pods -n "$NS" -l job-name=shm-control -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
CSHM=$(kc exec -n "$NS" "$CPOD" -- df -m /dev/shm 2>/dev/null | awk 'NR==2 {print $2}')
check "control: the same image without the emptyDir gets 64 MB" 64 "$CSHM"
kc delete job shm-control -n "$NS" --wait=false >/dev/null 2>&1

echo
echo "── a failed app is not restarted into a dead ingest path ──────────────────"
# backoffLimit: 0 plus restartPolicy: Never. Either alone is not enough — Never
# stops the kubelet restarting the container, and the Job controller would still
# make a second pod.
cat > "$WORK/startfail.py" <<'EOF'
import os, sys
sys.path.insert(0, "/orchestrator")
from runtime import KubernetesFlightRuntime
rt = KubernetesFlightRuntime(image=os.environ["FAIL_IMAGE"],
                             namespace=os.environ["APP_NAMESPACE"])
print(rt.start(2002, {"FLIGHT_ID": "2002"}))
EOF
docker run --rm --network "$NET" -e KUBECONFIG=/kube/config -e APP_NAMESPACE="$NS" \
  -e FAIL_IMAGE="$FAIL_IMAGE" -v "$WORK/kube:/kube:ro" -v "$WORK:/work:ro" \
  -v "$REPO/orchestrator:/orchestrator:ro" "$CLIENT_IMAGE" python /work/startfail.py >/dev/null 2>&1
for _ in $(seq 1 45); do
  [ "$(kc get job agrarian-flight-2002 -n "$NS" -o jsonpath='{.status.failed}' 2>/dev/null)" = "1" ] && break
  sleep 2
done
check "the Job reached a failed state" 1 \
  "$(kc get job agrarian-flight-2002 -n "$NS" -o jsonpath='{.status.failed}' 2>/dev/null)"
check "the failed Job made exactly one pod, not a retry loop" 1 \
  "$(kc get pods -n "$NS" -l agrarian.flight_id=2002 --no-headers 2>/dev/null | wc -l | tr -d ' ')"
# Given the Job exists and has failed, an empty .status.active means it gave up
# rather than that the lookup missed.
check "and it is not still trying" "" \
  "$(kc get job agrarian-flight-2002 -n "$NS" -o jsonpath='{.status.active}' 2>/dev/null)"

echo
echo "── stop() takes the pod with it, not just the Job ─────────────────────────"
# Deleting a Job without Background propagation orphans its pods: a GPU pod running
# forever with nothing owning it, and nothing in `kubectl get jobs` to show for it.
cat > "$WORK/stop.py" <<'EOF'
import os, sys
sys.path.insert(0, "/orchestrator")
from runtime import KubernetesFlightRuntime
rt = KubernetesFlightRuntime(image=os.environ["STUB_IMAGE"],
                             namespace=os.environ["APP_NAMESPACE"])
rt.stop(sys.argv[1])
EOF
as_orchestrator /work/stop.py "$HANDLE" >/dev/null 2>&1
for _ in $(seq 1 45); do
  [ -z "$(kc get pods -n "$NS" -l agrarian.flight_id=2001 --no-headers 2>/dev/null)" ] && break
  sleep 2
done
check "the Job is gone" 0 \
  "$(kc get jobs -n "$NS" -l agrarian.flight_id=2001 --no-headers 2>/dev/null | wc -l | tr -d ' ')"
check "its pod is gone too — nothing orphaned on the GPU" 0 \
  "$(kc get pods -n "$NS" -l agrarian.flight_id=2001 --no-headers 2>/dev/null | wc -l | tr -d ' ')"

echo
echo "── the service account can run flights and nothing else ───────────────────"
# The Docker backend's authority is the daemon socket, which is root on the host.
# This is what replaces it, so it is worth proving in both directions: the grants
# above are only meaningful next to the refusals below.
SA=system:serviceaccount:agrarian:agrarian-orchestrator
cani() { kc auth can-i "$1" "$2" ${3:+-n "$3"} --as="$SA" 2>/dev/null; }
check "it may create flight Jobs"          yes "$(cani create jobs "$NS")"
check "it may list them (recover() needs this)" yes "$(cani list jobs "$NS")"
check "it may delete them"                 yes "$(cani delete jobs "$NS")"
check "it may NOT read Secrets in the hub" no  "$(cani get secrets agrarian)"
check "it may NOT read flight pod logs"    no  "$(cani get pods/log "$NS")"
check "it may NOT exec into a flight pod"  no  "$(cani create pods/exec "$NS")"
check "it may NOT create Jobs in kube-system" no "$(cani create jobs kube-system)"
check "it may NOT touch nodes"             no  "$(cani get nodes)"

echo
echo "=========================================================="
echo "$pass/$((pass+fail)) passed"
[ "$fail" -eq 0 ] || { echo; echo "--- k3s log ---"; docker logs "$SERVER" 2>&1 | tail -30; exit 1; }

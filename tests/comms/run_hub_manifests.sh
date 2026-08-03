#!/usr/bin/env bash
# The hub tier's Kubernetes manifests, against a real API server.
#
# CLOUD_ARCHITECTURE.md §9 said these manifests were "work that can only be tested
# against a cluster nobody is running". That was wrong, and run_k8s_runtime.sh next
# door had already disproved it for Jobs: k3s in a container is a real Kubernetes
# distribution — same API server, same scheduler, same kubelet — and it starts in
# seconds. This runner does the same thing for Deployments, Services and the
# kustomization that generates their ConfigMaps.
#
# WHAT A MOCK CANNOT HAVE AN OPINION ABOUT, AND WHY THIS EXISTS
# -------------------------------------------------------------
# Both bugs found while writing these manifests were invisible to reading them:
#
#   - kustomize's `namespace:` transformer rewrites Namespace OBJECTS too, which
#     collapsed agrarian-flights into agrarian and would have handed the
#     orchestrator's Role authority over the namespace holding db-writer, Redis and
#     Mosquitto — the exact thing orchestrator-rbac.yaml exists to prevent.
#   - a configMapGenerator without an explicit namespace generates into `default`
#     AND silently declines to stamp its content hash into the references. The
#     build succeeds; every mount dangles; pods sit in ContainerCreating.
#
# Neither produces an error from `kustomize build`. Both are asserted below.
#
# NOT UNDER TEST: the GPU, the load balancers, and the cloud. k3s's ServiceLB
# assigns node addresses rather than provisioning anything, so a LoadBalancer here
# proves the Service spec is accepted and routes — not that a provider will carry
# mixed TCP and UDP on one address. That last one is a real deployment constraint
# and the first real cluster is where it stops being spec.
#
# Needs: Docker, and enough disk for the k3s image. No cluster, no cloud account.
#
# Usage:  ./run_hub_manifests.sh
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

K3S_IMAGE=rancher/k3s:v1.31.5-k3s1
NET=hubman-net
SERVER=hubman-server
NS=agrarian
WORK="$(mktemp -d)"

DBW_IMAGE=hubman-db-writer:test
WSS_IMAGE=hubman-ws-server:test
PORTAL_IMAGE=hubman-portal:test
ORC_IMAGE=hubman-orchestrator:test
REC_IMAGE=hubman-recorder:test

cleanup() {
  docker rm -f "$SERVER" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  docker rmi "$DBW_IMAGE" "$WSS_IMAGE" "$PORTAL_IMAGE" "$ORC_IMAGE" "$REC_IMAGE" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

# -i is load-bearing: several calls below pipe a manifest in on stdin, and without
# it docker exec forwards nothing. kubectl then reports "no objects passed to
# apply" and carries on with exit 0 in some paths, so the symptom is an empty
# cluster rather than an error — which is how the first run of this file produced
# a page of vacuous passes.
kc() { docker exec -i "$SERVER" kubectl "$@"; }

pass=0; fail=0
check() {  # check <name> <expected> <actual>
  if [ "$2" = "$3" ]; then echo "PASS  $1   [$3]"; pass=$((pass+1))
  else echo "FAIL  $1   [expected $2, got $3]"; fail=$((fail+1)); fi
}
contains() {  # contains <name> <needle> <haystack>
  case "$3" in
    *"$2"*) echo "PASS  $1"; pass=$((pass+1)) ;;
    *)      echo "FAIL  $1   [expected to find '$2']"; fail=$((fail+1)) ;;
  esac
}
absent() {  # absent <name> <needle> <haystack>
  case "$3" in
    *"$2"*) echo "FAIL  $1   [found '$2' and should not have]"; fail=$((fail+1)) ;;
    *)      echo "PASS  $1"; pass=$((pass+1)) ;;
  esac
}

# ── Render ────────────────────────────────────────────────────────────────────
# The deploy-time files are gitignored, so a clean checkout has only the examples.
# Copying them here is what the operator does, and it keeps this runner honest
# about which values are repository facts and which are deployment facts.
for f in endpoints flight-app; do
  [ -f "$REPO/configs/k8s/$f.env" ] || cp "$REPO/configs/k8s/$f.env.example" "$REPO/configs/k8s/$f.env"
done

echo "==> rendering configs/ with kustomize"
docker run --rm -v "$REPO:/repo" -w /repo --entrypoint kubectl "$K3S_IMAGE" \
  kustomize configs/ > "$WORK/rendered.yaml" 2>"$WORK/render.err"
if [ ! -s "$WORK/rendered.yaml" ]; then
  echo "kustomize build produced nothing:"; cat "$WORK/render.err"; exit 1
fi
R=$(cat "$WORK/rendered.yaml")

echo
echo "==> what the manifests say (before anything runs)"

# The two-namespace separation, which the kustomize namespace transformer silently
# destroys. Asserted as a property of the render rather than trusted.
check "both namespaces are rendered, not collapsed into one" \
  "2" "$(printf '%s' "$R" | grep -c '^kind: Namespace')"
contains "the flight namespace survives" "name: agrarian-flights" "$R"

# The Role must be IN the flight namespace. If the transformer had collapsed them
# this would read 'agrarian', and the orchestrator would hold Jobs authority over
# db-writer's namespace.
ROLE_NS=$(python3 - "$WORK/rendered.yaml" <<'EOF'
import sys
for doc in open(sys.argv[1]).read().split('\n---\n'):
    # Exact kind match: startswith('kind: Role') also catches RoleBinding, which
    # lives in the same namespace and made this assertion print two lines.
    if any(l == 'kind: Role' for l in doc.splitlines()):
        for line in doc.splitlines():
            if line.startswith('  namespace: '):
                print(line.split(': ')[1]); break
EOF
)
check "the orchestrator Role is scoped to the flight namespace" "agrarian-flights" "$ROLE_NS"

# REFERENTIAL INTEGRITY, not a spelling check. Every ConfigMap named by a volume,
# an envFrom or a valueFrom must exist as an object in this render, in the same
# namespace as the thing referring to it.
#
# The first version of this compared names against a regex for the un-hashed
# spelling, and it did not work: the bare name sits at the end of a line, the
# pattern needed a trailing character, and so it matched nothing whether or not the
# bug was present. It passed the control that was written to break it. This version
# resolves the references instead, which is the property actually wanted, and it
# fails the control.
DANGLING=$(python3 - "$WORK/rendered.yaml" <<'EOF'
import sys, yaml

docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
have = {(d['metadata'].get('namespace'), d['metadata']['name'])
        for d in docs if d.get('kind') == 'ConfigMap'}

def refs(node, out):
    """Every ConfigMap name mentioned anywhere in a pod spec."""
    if isinstance(node, dict):
        for key in ('configMapRef', 'configMapKeyRef', 'configMap'):
            ref = node.get(key)
            if isinstance(ref, dict) and 'name' in ref:
                out.add(ref['name'])
        for v in node.values():
            refs(v, out)
    elif isinstance(node, list):
        for v in node:
            refs(v, out)

missing = []
for d in docs:
    if d.get('kind') != 'Deployment':
        continue
    ns = d['metadata'].get('namespace')
    named = set()
    refs(d['spec']['template']['spec'], named)
    for name in named:
        if (ns, name) not in have:
            missing.append(f"{d['metadata']['name']} -> {name} (ns {ns})")

for m in missing:
    print(m, file=sys.stderr)
print(len(missing))
EOF
)
check "every ConfigMap reference resolves to a ConfigMap in the same namespace" "0" "$DANGLING"

# §7: the portal holds no signing key and no database credentials. This is the
# claim run_portal.sh proves at runtime; here it is a property of the manifest, so
# a future edit that adds the secretRef fails in review rather than in production.
PORTAL_DOC=$(python3 - "$WORK/rendered.yaml" <<'EOF'
import sys
for doc in open(sys.argv[1]).read().split('\n---\n'):
    if 'kind: Deployment' in doc and 'name: portal\n' in doc:
        print(doc)
EOF
)
absent "the portal manifest carries no session secret" "agrarian-session-jwt" "$PORTAL_DOC"
absent "the portal manifest carries no database secret" "agrarian-db" "$PORTAL_DOC"

DBW_DOC=$(python3 - "$WORK/rendered.yaml" <<'EOF'
import sys
for doc in open(sys.argv[1]).read().split('\n---\n'):
    if 'kind: Deployment' in doc and 'name: db-writer\n' in doc:
        print(doc)
EOF
)
contains "db-writer does carry it — the control for the two above" "agrarian-session-jwt" "$DBW_DOC"

# §8: db-writer, the alert-write API and the orchestrator must never be routable.
for svc in db-writer ws-server-alerts orchestrator redis; do
  TYPE=$(printf '%s' "$R" | python3 -c "
import sys
docs = sys.stdin.read().split('\n---\n')
for d in docs:
    if 'kind: Service' in d and '  name: $svc\n' in d:
        t = [l for l in d.splitlines() if l.startswith('  type: ')]
        print(t[0].split(': ')[1] if t else 'ClusterIP'); break
")
  check "Service/$svc is not routable from outside" "ClusterIP" "$TYPE"
done

# 8189 must arrive on ONE address over both protocols, because ICE advertises one
# host candidate. Two Services would mean two addresses.
MTX_SVC=$(printf '%s' "$R" | python3 -c "
import sys
for d in sys.stdin.read().split('\n---\n'):
    if 'kind: Service' in d and 'name: mediamtx-public' in d: print(d)
")
contains "mediamtx-public carries WebRTC over UDP" "protocol: UDP" "$MTX_SVC"
check "…and both 8189 ports are on that one Service" \
  "2" "$(printf '%s' "$MTX_SVC" | grep -c 'port: 8189')"

# §8: 8189 must never be proxied. Traefik carrying it would terminate the
# end-to-end DTLS-SRTP the media path is built on.
TRAEFIK_SVC=$(printf '%s' "$R" | python3 -c "
import sys
for d in sys.stdin.read().split('\n---\n'):
    if 'kind: Service' in d and 'name: traefik' in d: print(d)
")
absent "Traefik does not carry the WebRTC media port" "8189" "$TRAEFIK_SVC"

# The point of the whole backend (§2).
ORC_DOC=$(python3 - "$WORK/rendered.yaml" <<'EOF'
import sys
for doc in open(sys.argv[1]).read().split('\n---\n'):
    if 'kind: Deployment' in doc and 'name: orchestrator\n' in doc:
        print(doc)
EOF
)
absent "the orchestrator mounts no Docker socket" "docker.sock" "$ORC_DOC"
contains "…and runs the Kubernetes backend instead" "value: kubernetes" "$ORC_DOC"
contains "…under its own ServiceAccount" "serviceAccountName: agrarian-orchestrator" "$ORC_DOC"

# ── The cluster ───────────────────────────────────────────────────────────────

echo
echo "==> starting k3s"
docker network create "$NET" >/dev/null 2>&1 || true
# local-storage and servicelb are LEFT ENABLED here, unlike run_k8s_runtime.sh:
# this deployment has a PersistentVolumeClaim and three LoadBalancer Services, and
# disabling the providers would leave the first Pending forever and make the other
# three prove nothing.
docker run -d --name "$SERVER" --privileged --network "$NET" \
  --tmpfs /run --tmpfs /var/run -e K3S_KUBECONFIG_MODE=644 \
  "$K3S_IMAGE" server \
  --disable=traefik --disable=metrics-server \
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

echo "==> building the five service images"
build() {  # build <tag> <context>
  docker build -q -t "$1" "$REPO/$2" >/dev/null || { echo "build failed: $2"; return 1; }
  docker save "$1" | docker exec -i "$SERVER" ctr -n k8s.io images import - >/dev/null
}
build "$DBW_IMAGE"    db_writer    || exit 1
build "$WSS_IMAGE"    ws_server    || exit 1
build "$PORTAL_IMAGE" portal       || exit 1
build "$ORC_IMAGE"    orchestrator || exit 1
build "$REC_IMAGE"    recorder     || exit 1

echo "==> pre-pulling the third-party images"
for img in redis:7-alpine traefik:v3.3 bluenviron/mediamtx:latest-ffmpeg \
           iegomez/mosquitto-go-auth:2.1.0-mosquitto_2.0.15 postgres:16-alpine; do
  docker pull -q "$img" >/dev/null 2>&1
  docker save "$img" | docker exec -i "$SERVER" ctr -n k8s.io images import - >/dev/null
done

# ── Credentials and the database ──────────────────────────────────────────────

echo "==> creating the namespaces, secrets and a database to point at"
kc apply -f - < "$REPO/configs/k8s/orchestrator-rbac.yaml" >/dev/null

CERTS="$WORK/certs"
CERT_DIR="$CERTS" "$REPO/scripts/generate_local_certs.sh" >/dev/null 2>&1 \
  || { echo "could not issue a local certificate"; exit 1; }
docker cp "$CERTS/server/server.crt" "$SERVER:/tmp/tls.crt" >/dev/null
docker cp "$CERTS/server/server.key" "$SERVER:/tmp/tls.key" >/dev/null
docker cp "$CERTS/ca/ca.crt"         "$SERVER:/tmp/ca.crt"  >/dev/null

kc -n "$NS" create secret generic agrarian-tls \
  --from-file=tls.crt=/tmp/tls.crt --from-file=tls.key=/tmp/tls.key \
  --from-file=ca.crt=/tmp/ca.crt >/dev/null
kc -n "$NS" create secret generic agrarian-session-jwt \
  --from-literal=SESSION_JWT_SECRET="$(openssl rand -hex 32)" >/dev/null
kc -n "$NS" create secret generic agrarian-recording-store \
  --from-literal=RECORDING_STORE_SERVICE=local \
  --from-literal=RECORDING_DELETE_LOCAL_ON_SUCCESS=false >/dev/null
# The design puts PostgreSQL outside the cluster (managed). One in-cluster is the
# cheapest way to give db-writer something real to connect to.
kc -n "$NS" create secret generic agrarian-db \
  --from-literal=DB_HOST=postgres --from-literal=DB_NAME=testdb \
  --from-literal=DB_WORKER_NAME=testuser --from-literal=DB_WORKER_PASSWORD=testpw >/dev/null

kc apply -f - >/dev/null <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata: {name: postgres, namespace: agrarian}
spec:
  replicas: 1
  selector: {matchLabels: {app: postgres}}
  template:
    metadata: {labels: {app: postgres}}
    spec:
      containers:
        - name: postgres
          image: postgres:16-alpine
          env:
            - {name: POSTGRES_USER,     value: testuser}
            - {name: POSTGRES_PASSWORD, value: testpw}
            - {name: POSTGRES_DB,       value: testdb}
          ports: [{containerPort: 5432}]
---
apiVersion: v1
kind: Service
metadata: {name: postgres, namespace: agrarian}
spec:
  selector: {app: postgres}
  ports: [{port: 5432, targetPort: 5432}]
EOF

# ── Apply ─────────────────────────────────────────────────────────────────────
# The rendered manifests, with the placeholder registry swapped for the images
# just imported. Everything else is exactly what `kubectl apply -k configs/` sends.

echo "==> applying the hub"
sed -e "s|ghcr.io/REPLACE_ME/agrarian-db-writer:v0.1.0|$DBW_IMAGE|" \
    -e "s|ghcr.io/REPLACE_ME/agrarian-ws-server:v0.1.0|$WSS_IMAGE|" \
    -e "s|ghcr.io/REPLACE_ME/agrarian-portal:v0.1.0|$PORTAL_IMAGE|" \
    -e "s|ghcr.io/REPLACE_ME/agrarian-orchestrator:v0.1.0|$ORC_IMAGE|" \
    -e "s|ghcr.io/REPLACE_ME/agrarian-recorder:v0.1.0|$REC_IMAGE|" \
    "$WORK/rendered.yaml" > "$WORK/applied.yaml"

APPLY_OUT=$(kc apply -f - < "$WORK/applied.yaml" 2>&1)
APPLY_RC=$?
check "the whole hub applies without an API-server rejection" "0" "$APPLY_RC"
[ "$APPLY_RC" = "0" ] || { echo "$APPLY_OUT" | tail -20; }

echo "==> waiting for the hub to come up (up to 4 minutes)"
kc -n "$NS" wait --for=condition=Available --timeout=240s \
  deployment/redis deployment/db-writer deployment/ws-server deployment/portal \
  deployment/orchestrator deployment/mediamtx deployment/mosquitto deployment/traefik \
  >/dev/null 2>&1
READY=$(kc -n "$NS" get deploy -o json | python3 -c "
import json,sys
d=json.load(sys.stdin)
names=['redis','db-writer','ws-server','portal','orchestrator','mediamtx','mosquitto','traefik']
print(sum(1 for i in d['items'] if i['metadata']['name'] in names
          and i.get('status',{}).get('readyReplicas',0) == i['spec']['replicas']))
")
check "all eight hub deployments reach their full replica count" "8" "$READY"
if [ "$READY" != "8" ]; then
  echo "--- not ready:"; kc -n "$NS" get pods 2>&1 | tail -25
  kc -n "$NS" get events --sort-by=.lastTimestamp 2>&1 | tail -15
fi

echo
echo "==> what the running cluster says"

# The dangling-ConfigMap failure shows up here and nowhere else: a pod whose mount
# cannot resolve never leaves ContainerCreating, and no manifest check sees it.
STUCK=$(kc -n "$NS" get pods -o json | python3 -c "
import json,sys
d=json.load(sys.stdin)
n=0
for p in d['items']:
    for cs in p.get('status',{}).get('containerStatuses',[]) or []:
        w=(cs.get('state',{}).get('waiting') or {}).get('reason','')
        if w in ('CreateContainerConfigError','ContainerCreating','CreateContainerError'): n+=1
print(n)
")
check "no container is stuck on an unresolvable ConfigMap or Secret" "0" "$STUCK"

# The recorder is a sidecar, not a Deployment. This is the translation decision the
# shared recordings volume forced, and it is the thing to notice if someone later
# "tidies" it back into a Deployment of its own — which would need ReadWriteMany.
MTX_POD=$(kc -n "$NS" get pods -l app.kubernetes.io/name=mediamtx -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
CONTAINERS=$(kc -n "$NS" get pod "$MTX_POD" -o jsonpath='{.spec.containers[*].name}' 2>/dev/null)
contains "the recorder runs beside MediaMTX, in one pod" "recorder" "$CONTAINERS"
contains "…alongside the media server itself" "mediamtx" "$CONTAINERS"
check "no separate recorder Deployment exists" \
  "" "$(kc -n "$NS" get deploy recorder --ignore-not-found -o name 2>/dev/null)"

# Both containers must actually mount the claim, or the sidecar reads an empty
# directory and every upload silently finds no file — which is how the recording
# path failed before (§4: the hook fired into a container with no wget).
MOUNTS=$(kc -n "$NS" get pod "$MTX_POD" -o json | python3 -c "
import json,sys
p=json.load(sys.stdin)
print(sum(1 for c in p['spec']['containers']
          for m in c.get('volumeMounts',[]) if m['mountPath']=='/recordings'))
")
check "both containers mount the recordings volume" "2" "$MOUNTS"

# The ServiceAccount is the point of the backend. A pod running as `default` would
# have no Jobs authority at all and every flight would fail to start.
SA=$(kc -n "$NS" get pod -l app.kubernetes.io/name=orchestrator -o jsonpath='{.items[0].spec.serviceAccountName}' 2>/dev/null)
check "the orchestrator pod runs under its own ServiceAccount" "agrarian-orchestrator" "$SA"

# Scoped, not reduced (§2). Both directions, because a grant that permits
# everything passes the first check as easily as a correct one.
CAN_FLIGHTS=$(kc auth can-i create jobs -n agrarian-flights \
  --as=system:serviceaccount:agrarian:agrarian-orchestrator 2>/dev/null)
check "it may create Jobs in the flight namespace" "yes" "$CAN_FLIGHTS"
CAN_HUB=$(kc auth can-i create jobs -n agrarian \
  --as=system:serviceaccount:agrarian:agrarian-orchestrator 2>/dev/null)
check "…and may not in the hub namespace" "no" "$CAN_HUB"
CAN_SECRET=$(kc auth can-i get secrets -n agrarian \
  --as=system:serviceaccount:agrarian:agrarian-orchestrator 2>/dev/null)
check "…and cannot read the session signing key" "no" "$CAN_SECRET"

# §7, as a runtime fact rather than a manifest one: the tier facing the internet
# does not hold the key that signs every credential in the system.
PORTAL_POD=$(kc -n "$NS" get pods -l app.kubernetes.io/name=portal -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
PORTAL_ENV=$(kc -n "$NS" exec "$PORTAL_POD" -- env 2>/dev/null)
absent "the running portal has no SESSION_JWT_SECRET in its environment" "SESSION_JWT_SECRET" "$PORTAL_ENV"
absent "…and no database password either" "DB_WORKER_PASSWORD" "$PORTAL_ENV"
contains "…but it does know where db-writer is" "DB_WRITER_URL" "$PORTAL_ENV"

DBW_POD=$(kc -n "$NS" get pods -l app.kubernetes.io/name=db-writer -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
DBW_ENV=$(kc -n "$NS" exec "$DBW_POD" -- env 2>/dev/null)
contains "db-writer does have the signing key — the control" "SESSION_JWT_SECRET" "$DBW_ENV"

# The services answer, which is the difference between "the pod is Running" and
# "the process inside it started".
DBW_HEALTH=$(kc -n "$NS" exec "$DBW_POD" -- python -c \
  "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/health').status)" 2>/dev/null)
check "db-writer answers its own health endpoint" "200" "$DBW_HEALTH"
PORTAL_HEALTH=$(kc -n "$NS" exec "$PORTAL_POD" -- python -c \
  "import urllib.request;print(urllib.request.urlopen('http://db-writer:8000/health').status)" 2>/dev/null)
check "the portal reaches db-writer by service DNS" "200" "$PORTAL_HEALTH"

# MediaMTX exits at startup if its certificate file is missing (§7) — not a
# warning and not a disabled listener. So an RTMPS listener on :1936 is the single
# strongest evidence available here that the agrarian-tls Secret mounted, was
# readable, and held a usable leaf: the plaintext listener would come up either way.
MTX_LOG=$(kc -n "$NS" logs "$MTX_POD" -c mediamtx --tail=80 2>/dev/null)
contains "MediaMTX opened RTMPS, so the TLS Secret mounted and parsed" \
  "[RTMPS] started with listener on :1936" "$MTX_LOG"
contains "…and the plaintext fallback §7 keeps is there too" \
  "[RTMP] started with listener on :1935" "$MTX_LOG"

# The claim is bound rather than Pending, which is what would happen if the access
# mode were wrong for the storage class.
PVC=$(kc -n "$NS" get pvc recordings -o jsonpath='{.status.phase}' 2>/dev/null)
check "the recordings claim is bound" "Bound" "$PVC"

echo
echo "============================================================"
echo "$pass/$((pass+fail)) passed"
[ "$fail" -eq 0 ] || echo "$fail FAILED"
echo
echo "==> cleaning up"
exit $([ "$fail" -eq 0 ] && echo 0 || echo 1)

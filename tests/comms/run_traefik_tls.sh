#!/usr/bin/env bash
# The ingress tier: Traefik terminating TLS in front of the portal, ws-server and
# MediaMTX, with a real certificate from the local CA.
#
# WHY THIS RUNNER EXISTS
# ----------------------
# Two claims in CLOUD_ARCHITECTURE.md could not be tested before Traefik did:
#
#   §8  "the portal has no working configuration today". COOKIE_SECURE and
#       PUBLIC_TLS both default on, a Secure cookie is not returned over http://,
#       and nothing terminated TLS — so the portal either ran with the local-HTTP
#       affordance on in production or forgot every login. Every other runner
#       here works around that by speaking plain HTTP with the defaults left on,
#       which asserts the cookie's ATTRIBUTES but never that a browser would send
#       it back. This one runs the real thing over real TLS.
#
#   §4  the rate limiter counts the client's address. Traefik is now the peer
#       address the portal sees, which makes PORTAL_TRUSTED_PROXY_HOPS
#       load-bearing in a way it was not when the portal was reached directly.
#
# The certificate is issued into a throwaway directory, not into certificates/ —
# the CA there may already be installed in somebody's browser and regenerating it
# would silently invalidate that.
#
# WHAT IS NOT COVERED: whether video plays. HLS and WHEP are driven only far
# enough to prove the request crossed Traefik and reached MediaMTX's auth hook.
# No stream is published, so there is nothing to decode.
#
# Usage:  ./run_traefik_tls.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NET=traefik-tls-net
DOMAIN=agrarian.local
CERTS="$(mktemp -d)"

DBW_IMAGE=dbw-tlstest
PORTAL_IMAGE=portal-tlstest
WS_IMAGE=ws-tlstest

cleanup() {
  docker rm -f tt-traefik tt-portal tt-ws tt-dbw tt-mediamtx tt-pg tt-redis >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  docker rmi "$PORTAL_IMAGE" "$DBW_IMAGE" "$WS_IMAGE" >/dev/null 2>&1 || true
  rm -rf "$CERTS"
}
trap cleanup EXIT

echo "==> issuing a certificate from the local CA into $CERTS"
CERT_DIR="$CERTS" "$REPO/scripts/generate_local_certs.sh" "$DOMAIN" >/dev/null
echo "    leaf expires: $(openssl x509 -in "$CERTS/server/server.crt" -noout -enddate | cut -d= -f2)"

echo "==> building images"
docker build -q -t "$DBW_IMAGE" "$REPO/db_writer" >/dev/null
docker build -q -t "$PORTAL_IMAGE" "$REPO/portal" >/dev/null
docker build -q -t "$WS_IMAGE" "$REPO/ws_server" >/dev/null

# An explicit subnet, so the two rate-limit clients at the end can be pinned to
# fixed addresses. Docker reuses the IP of an exited container, which made "two
# clients" mean "one address twice" often enough to be worthless — the second
# client inherited the first's exhausted bucket and the test failed for a reason
# that had nothing to do with the portal.
docker network create --subnet 172.28.0.0/16 "$NET" >/dev/null 2>&1 || true

docker run -d --name tt-pg --network "$NET" \
  -e POSTGRES_PASSWORD=testpw -e POSTGRES_USER=testuser -e POSTGRES_DB=testdb \
  postgres:16-alpine >/dev/null
docker run -d --name tt-redis --network "$NET" \
  redis:7-alpine redis-server --save "" --appendonly no >/dev/null

echo "==> waiting for postgres"
for _ in $(seq 1 30); do
  docker exec tt-pg pg_isready -U testuser >/dev/null 2>&1 && break
  sleep 1
done

SECRET=$(openssl rand -hex 32)
DBENV=(-e DB_SERVICE=postgresql -e DB_HOST=tt-pg -e DB_PORT=5432 -e DB_NAME=testdb
       -e DB_WORKER_NAME=testuser -e DB_WORKER_PASSWORD=testpw
       -e SESSION_JWT_SECRET="$SECRET")

echo "==> creating the schema"
docker run --rm --network "$NET" "${DBENV[@]}" "$DBW_IMAGE" \
  python rebuild_schema.py >/dev/null

# The alias matters: mediamtx.yaml hardcodes http://db-writer:8000/auth/mediamtx,
# so the auth hook only resolves if this container answers to that name.
echo "==> starting db-writer"
docker run -d --name tt-dbw --network "$NET" --network-alias db-writer \
  "${DBENV[@]}" "$DBW_IMAGE" >/dev/null

echo "==> starting ws-server"
docker run -d --name tt-ws --network "$NET" --network-alias ws-server \
  -e WS_PORT=8765 -e REDIS_URL=redis://tt-redis:6379/0 \
  -e SESSION_JWT_SECRET="$SECRET" \
  "$WS_IMAGE" >/dev/null

# TRUSTED_PROXY_HOPS=1 is the production value now that Traefik is in front, and
# it is what the forged-X-Forwarded-For assertion actually tests. Limits are
# lowered so exhausting one costs a handful of requests.
echo "==> starting the portal (COOKIE_SECURE and PUBLIC_TLS at their defaults)"
docker run -d --name tt-portal --network "$NET" --network-alias portal \
  -e DB_WRITER_URL=http://tt-dbw:8000 \
  -e MEDIA_PUBLIC_HOST="media.$DOMAIN" \
  -e WS_PUBLIC_HOST="ws.$DOMAIN" \
  -e WS_PORT=8765 \
  -e REDIS_URL=redis://tt-redis:6379/1 \
  -e TRUSTED_PROXY_HOPS="${PORTAL_HOPS:-1}" \
  -e LOGIN_RATE_LIMIT_PER_ACCOUNT=5 \
  -e LOGIN_RATE_LIMIT_PER_IP=8 \
  -e RATE_LIMIT_WINDOW_S=900 \
  "$PORTAL_IMAGE" >/dev/null

echo "==> starting mediamtx (the real config, so the auth hook is the real one)"
docker run -d --name tt-mediamtx --network "$NET" --network-alias mediamtx \
  -v "$REPO/configs/mediamtx/mediamtx.yaml:/mediamtx.yml:ro" \
  bluenviron/mediamtx:latest-ffmpeg >/dev/null

echo "==> waiting for /health"
for h in tt-dbw tt-ws tt-portal; do
  for _ in $(seq 1 30); do
    docker run --rm --network "$NET" curlimages/curl:latest \
      -sf "http://$h:8000/health" >/dev/null 2>&1 && break
    sleep 1
  done
done

# Traefik gets the repo's real configuration, not a test-shaped copy. The aliases
# are the three names the wildcard leaf covers, so TLS validation here is the same
# validation a browser would do rather than a hostname check switched off.
echo "==> starting traefik with the repo's own configuration"
docker run -d --name tt-traefik --network "$NET" \
  --network-alias "portal.$DOMAIN" \
  --network-alias "media.$DOMAIN" \
  --network-alias "ws.$DOMAIN" \
  -v "$REPO/configs/traefik/traefik.yml:/etc/traefik/traefik.yml:ro" \
  -v "$REPO/configs/traefik/dynamic:/etc/traefik/dynamic:ro" \
  -v "$CERTS/server:/etc/traefik/certs:ro" \
  traefik:v3.3 >/dev/null

echo "==> waiting for traefik to serve TLS"
for _ in $(seq 1 30); do
  docker run --rm --network "$NET" -v "$CERTS/server:/certs:ro" curlimages/curl:latest \
    -sf --cacert /certs/ca.crt "https://portal.$DOMAIN/health" >/dev/null 2>&1 && break
  sleep 1
done

# ── TLS-level assertions, which belong in shell because they are openssl's job ──
echo
echo "==> TLS floor"
TLS_RESULTS=0
TLS_TOTAL=0

tls_check() {
  TLS_TOTAL=$((TLS_TOTAL + 1))
  if eval "$2" >/dev/null 2>&1; then
    echo "PASS  $1"
    TLS_RESULTS=$((TLS_RESULTS + 1))
  else
    echo "FAIL  $1"
  fi
}

# A minimum TLS version that is configured but not applied looks exactly like one
# that is, until somebody scans it. TLS 1.1 must be refused outright.
tls_check "TLS 1.2 is accepted" \
  "docker run --rm --network $NET -v $CERTS/server:/certs:ro curlimages/curl:latest \
     -sf --tlsv1.2 --tls-max 1.2 --cacert /certs/ca.crt https://portal.$DOMAIN/health"
tls_check "TLS 1.3 is accepted" \
  "docker run --rm --network $NET -v $CERTS/server:/certs:ro curlimages/curl:latest \
     -sf --tlsv1.3 --cacert /certs/ca.crt https://portal.$DOMAIN/health"
tls_check "TLS 1.1 is refused" \
  "! docker run --rm --network $NET -v $CERTS/server:/certs:ro curlimages/curl:latest \
     -s --tlsv1.1 --tls-max 1.1 --cacert /certs/ca.crt https://portal.$DOMAIN/health"

# The wildcard leaf has to actually cover the three names the ingress tier will
# route on, or the move to Host-based routing breaks on a certificate error.
for name in "portal.$DOMAIN" "media.$DOMAIN" "ws.$DOMAIN"; do
  tls_check "the certificate is valid for $name" \
    "docker run --rm --network $NET -v $CERTS/server:/certs:ro curlimages/curl:latest \
       -sf --cacert /certs/ca.crt https://$name/health"
done

echo
echo "==> the portal, ws-server and MediaMTX through the proxy"
docker run --rm --network "$NET" \
  -v "$CERTS/server:/certs:ro" \
  -v "$REPO/tests/comms:/tests:ro" -w /tests \
  -e PORTAL="https://portal.$DOMAIN" \
  -e WSS="wss://ws.$DOMAIN:8765" \
  -e HLS="https://media.$DOMAIN:8888" \
  -e WHEP="https://media.$DOMAIN:8889" \
  -e DBW=http://tt-dbw:8000 \
  -e WS_API=http://tt-ws:8000 \
  -e CA=/certs/ca.crt \
  -e SESSION_JWT_SECRET="$SECRET" \
  python:3.12-slim sh -c \
  "pip install -q pyjwt websockets >/dev/null 2>&1 && python test_traefik_tls.py"
RC=$?

# ── Two clients, two buckets ─────────────────────────────────────────────────
#
# The assertion inside the Python file catches a hop count set too HIGH — the
# direction §4 calls dangerous, where a client names its own bucket. This one
# catches the opposite, which is new the day a proxy lands: at 0 the portal falls
# back to the peer address, that address is now always Traefik's, and every
# client on the internet shares one counter. One attacker then locks out
# everybody, and nothing in the previous test would notice.
#
# It needs two genuinely different source addresses, so it needs two containers
# and cannot live in the Python file with the rest.
#
# Confirmed non-vacuous: PORTAL_HOPS=0 ./run_traefik_tls.sh fails exactly this
# assertion and nothing else.
echo
echo "==> two clients get two rate-limit buckets"

# Pinned addresses, high in the subnet so they do not collide with anything
# Docker hands out dynamically. Without this the two clients are not reliably two
# addresses at all.
curl_in() {
  docker run --rm --network "$NET" --ip "$1" -v "$CERTS/server:/certs:ro" \
    curlimages/curl:latest sh -c "$2"
}

# Distinct addresses on every attempt, so the per-ACCOUNT limit never fires and
# what trips is the per-source one — the counter this assertion is about.
SPRAY='for i in $(seq 1 9); do
  curl -s -o /dev/null -w "%{http_code} " --cacert /certs/ca.crt \
    -X POST -d "email=spray$i@example.com&password=nope" \
    -H "Origin: https://portal.'"$DOMAIN"'" \
    https://portal.'"$DOMAIN"'/login
done'

CLIENT_A=$(curl_in 172.28.200.1 "$SPRAY")
echo "    client A: $CLIENT_A"

TLS_TOTAL=$((TLS_TOTAL + 1))
if [[ "$CLIENT_A" == *429* ]]; then
  echo "PASS  client A exhausts its own per-address limit"
  TLS_RESULTS=$((TLS_RESULTS + 1))
else
  echo "FAIL  client A exhausts its own per-address limit   [$CLIENT_A]"
fi

CLIENT_B=$(curl_in 172.28.200.2 'curl -s -o /dev/null -w "%{http_code}" --cacert /certs/ca.crt \
    -X POST -d "email=other@example.com&password=nope" \
    -H "Origin: https://portal.'"$DOMAIN"'" \
    https://portal.'"$DOMAIN"'/login')
echo "    client B: $CLIENT_B"

TLS_TOTAL=$((TLS_TOTAL + 1))
if [[ "$CLIENT_B" == "401" ]]; then
  echo "PASS  a second client is unaffected by the first's exhausted bucket"
  TLS_RESULTS=$((TLS_RESULTS + 1))
else
  echo "FAIL  a second client is unaffected by the first's exhausted bucket   [$CLIENT_B, expected 401]"
fi

echo
echo "==> shell assertions: $TLS_RESULTS/$TLS_TOTAL"
if [[ $TLS_RESULTS -ne $TLS_TOTAL ]]; then
  RC=1
fi

exit $RC

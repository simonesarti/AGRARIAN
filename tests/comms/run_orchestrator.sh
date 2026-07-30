#!/usr/bin/env bash
# End-to-end flight lifecycle: a drone starts streaming, a container appears; the
# drone stops, the container is gone and the flight row is closed.
#
# test_orchestrator.py checks the event logic against fakes. This checks what it
# cannot: that MediaMTX's runOnAvailable/runOnUnavailable hooks actually fire and
# reach the orchestrator, that the Docker backend really starts a container with the
# injected environment, and that end_time lands in PostgreSQL.
#
# The "GPU app" here is a stub image that sleeps. What is under test is the
# orchestration, not the pipeline — which is exactly what FlightRuntime exists to make
# possible without a GPU.
#
# Host ports 31935/38002 are used so this cannot collide with a running compose stack.
# Needs ffmpeg on the host and access to the Docker socket.
#
# Usage:  ./run_orchestrator.sh
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NET=orch-net
DBW_IMAGE=orch-dbw
ORC_IMAGE=orch-orchestrator
STUB_IMAGE=orch-stub-app
RTMP=localhost:31935
API=http://localhost:38002
GRACE=5

cleanup() {
  docker rm -f orch-mediamtx orchestrator db-writer orch-pg >/dev/null 2>&1 || true
  docker ps -aq --filter "label=agrarian.flight_id" | xargs -r docker rm -f >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  docker rmi "$DBW_IMAGE" "$ORC_IMAGE" "$STUB_IMAGE" >/dev/null 2>&1 || true
}
trap cleanup EXIT

pass=0; fail=0
check() {  # check <name> <expected> <actual>
  if [ "$2" = "$3" ]; then echo "PASS  $1   [$3]"; pass=$((pass+1))
  else echo "FAIL  $1   [expected $2, got $3]"; fail=$((fail+1)); fi
}

command -v ffmpeg >/dev/null || { echo "missing required tool: ffmpeg"; exit 1; }

echo "==> building images"
docker build -q -t "$DBW_IMAGE" "$REPO/db_writer" >/dev/null || exit 1
docker build -q -t "$ORC_IMAGE" "$REPO/orchestrator" >/dev/null || exit 1

# The stand-in for the GPU app: it only has to exist, hold the injected environment,
# and stay up until it is stopped. Built from an empty context — the repo root would
# ship the whole tree to the daemon for a two-line Dockerfile.
#
# It traps SIGTERM on purpose. A bare `sleep` as PID 1 ignores signals it has no
# handler for, so `docker stop` would block for the full stop timeout on every
# teardown — which looks exactly like a hung orchestrator. The real app installs
# SIGTERM/SIGINT handlers, so this matches it.
STUB_CTX=$(mktemp -d)
cat > "$STUB_CTX/Dockerfile" <<'EOF'
FROM alpine:3.20
CMD ["sh", "-c", "trap 'exit 0' TERM INT; while :; do sleep 0.2; done"]
EOF
docker build -q -t "$STUB_IMAGE" "$STUB_CTX" >/dev/null || exit 1
rm -rf "$STUB_CTX"

docker network create "$NET" >/dev/null 2>&1 || true
docker run -d --name orch-pg --network "$NET" \
  -e POSTGRES_PASSWORD=testpw -e POSTGRES_USER=testuser -e POSTGRES_DB=testdb \
  postgres:16-alpine >/dev/null

echo "==> waiting for postgres"
for _ in $(seq 1 30); do
  docker exec orch-pg pg_isready -U testuser >/dev/null 2>&1 && break
  sleep 1
done

SECRET=$(openssl rand -hex 32)
ENVV=(-e DB_SERVICE=postgresql -e DB_HOST=orch-pg -e DB_PORT=5432 -e DB_NAME=testdb
      -e DB_WORKER_NAME=testuser -e DB_WORKER_PASSWORD=testpw
      -e SESSION_JWT_SECRET="$SECRET")

echo "==> seeding a tenant"
SEED=$(docker run --rm --network "$NET" "${ENVV[@]}" "$DBW_IMAGE" \
  sh -c "echo yes | python rebuild_schema.py --drop --seed-user pilot@test.io --seed-password pw123" 2>&1)
KEY=$(echo "$SEED" | sed -n 's/.*stream key : \([a-z0-9]*\).*/\1/p')
[ -n "$KEY" ] || { echo "seeding failed:"; echo "$SEED"; exit 1; }
echo "    stream key = $KEY"

echo "==> starting db-writer, orchestrator, mediamtx"
docker run -d --name db-writer --network "$NET" -p 38002:8000 "${ENVV[@]}" "$DBW_IMAGE" >/dev/null \
  || { echo "db-writer failed to start"; exit 1; }
docker run -d --name orchestrator --network "$NET" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e APP_IMAGE="$STUB_IMAGE" -e DB_WRITER_URL=http://db-writer:8000 \
  -e APP_NETWORK="$NET" -e RECONNECT_GRACE_S="$GRACE" \
  -e APP_ENV_WS_SERVER_URL=http://ws-server:8000 \
  "$ORC_IMAGE" >/dev/null || { echo "orchestrator failed to start"; exit 1; }
docker run -d --name orch-mediamtx --network "$NET" -p 31935:1935 \
  -v "$REPO/configs/mediamtx/mediamtx.yaml:/mediamtx.yml:ro" \
  bluenviron/mediamtx:latest-ffmpeg >/dev/null \
  || { echo "mediamtx failed to start (is a previous run still holding 31935?)"; exit 1; }
sleep 6

for c in db-writer orchestrator orch-mediamtx; do
  [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = "true" ] \
    || { echo "$c exited during startup:"; docker logs "$c" 2>&1 | tail -20; exit 1; }
done

flight_containers() { docker ps -q --filter "label=agrarian.flight_id" | wc -l | tr -d ' '; }
active_flights()    { docker exec orchestrator python -c \
  "import urllib.request,json;print(json.load(urllib.request.urlopen('http://localhost:8000/health'))['active_flights'])" 2>/dev/null; }
sql() { docker exec orch-pg psql -U testuser -d testdb -tAc "$1" 2>/dev/null | tr -d ' '; }

publish_bg() {  # publish_bg <seconds>
  ffmpeg -hide_banner -loglevel error -re -f lavfi -i testsrc=size=320x240:rate=15 \
    -t "$1" -c:v libx264 -preset ultrafast -tune zerolatency -g 15 \
    -f flv "rtmp://$RTMP/in/$KEY" >/dev/null 2>&1 &
  echo $!
}

echo
echo "── a stream going live creates a flight and a container ───────────────────"
check "no flight containers before takeoff" 0 "$(flight_containers)"

FFPID=$(publish_bg 25)
sleep 10

check "one flight container is running"     1 "$(flight_containers)"
check "orchestrator tracks one flight"      1 "$(active_flights)"
check "a flight row exists"                 1 "$(sql 'SELECT COUNT(*) FROM flights;')"
check "the flight is open (no end_time)"    1 "$(sql 'SELECT COUNT(*) FROM flights WHERE end_time IS NULL;')"

CID=$(docker ps -q --filter "label=agrarian.flight_id" | head -1)
UUID=$(sql 'SELECT public_uuid FROM flights ORDER BY flight_id DESC LIMIT 1;')
FID=$(sql 'SELECT flight_id FROM flights ORDER BY flight_id DESC LIMIT 1;')
CENV=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$CID" 2>/dev/null)

echo
echo "── the container received the flight's identity, and no user credentials ──"
check "FLIGHT_ID injected"            "FLIGHT_ID=$FID"                              "$(echo "$CENV" | grep '^FLIGHT_ID=')"
check "ingest path injected"          "VIDEO_STREAM_READER_STREAM_KEY=in/$KEY"      "$(echo "$CENV" | grep '^VIDEO_STREAM_READER_STREAM_KEY=')"
check "output path injected"          "VIDEO_OUT_STREAM_STREAM_KEY=out/$UUID"       "$(echo "$CENV" | grep '^VIDEO_OUT_STREAM_STREAM_KEY=')"
check "operator setting forwarded"    "WS_SERVER_URL=http://ws-server:8000"         "$(echo "$CENV" | grep '^WS_SERVER_URL=')"
check "a publisher token was injected" 1 "$(echo "$CENV" | grep -c '^PUBLISHER_TOKEN=ey')"
check "NO end-user credentials in the container" 0 "$(echo "$CENV" | grep -cE '^(DB_USERNAME|DB_PASSWORD)=.+')"

echo
echo "── a brief drop does not tear the flight down ─────────────────────────────"
kill "$FFPID" >/dev/null 2>&1; wait "$FFPID" 2>/dev/null
sleep 2
check "container survives the publisher dropping" 1 "$(flight_containers)"
FFPID=$(publish_bg 20)
sleep 6
check "reconnect keeps the same container"        1 "$(flight_containers)"
check "reconnect did NOT open a second flight"    1 "$(sql 'SELECT COUNT(*) FROM flights;')"
check "flight is still open"                      1 "$(sql 'SELECT COUNT(*) FROM flights WHERE end_time IS NULL;')"

echo
echo "── the drone lands ────────────────────────────────────────────────────────"
kill "$FFPID" >/dev/null 2>&1; wait "$FFPID" 2>/dev/null
# Grace period, then the container stop itself, then db-writer's close round trip.
sleep $((GRACE + 12))

check "flight container is gone"          0 "$(flight_containers)"
check "orchestrator tracks no flights"    0 "$(active_flights)"
check "flight row is closed (end_time set)" 1 "$(sql 'SELECT COUNT(*) FROM flights WHERE end_time IS NOT NULL;')"
check "still exactly one flight in total" 1 "$(sql 'SELECT COUNT(*) FROM flights;')"

echo
echo "── a revoked key starts nothing ───────────────────────────────────────────"
docker run --rm --network "$NET" "${ENVV[@]}" "$DBW_IMAGE" python -c "
from db_manager import UserDirectory
d = UserDirectory('postgresql://testuser:testpw@orch-pg:5432/testdb')
s = d.resolve_stream_key('$KEY')
d.revoke_stream(s['stream_id'], s['user_id'])
" >/dev/null 2>&1
ffmpeg -hide_banner -loglevel error -re -f lavfi -i testsrc=size=320x240:rate=15 \
  -t 3 -c:v libx264 -preset ultrafast -f flv "rtmp://$RTMP/in/$KEY" >/dev/null 2>&1
sleep 3
check "revoked key spawns no container"   0 "$(flight_containers)"
check "revoked key opens no second flight" 1 "$(sql 'SELECT COUNT(*) FROM flights;')"

echo
echo "=========================================================="
echo "$pass/$((pass+fail)) passed"
[ "$fail" -eq 0 ] || { echo; echo "--- orchestrator log ---"; docker logs orchestrator 2>&1 | tail -30; exit 1; }

#!/usr/bin/env bash
# End-to-end orchestrator restart recovery, against real MediaMTX, a real Docker
# daemon and real PostgreSQL.
#
# test_orchestrator.py checks FlightOrchestrator.recover() against a fake runtime.
# This checks what it cannot: that killing the real orchestrator process — not
# stopping it, killing it, the same as an OOM kill or `docker kill` — really does
# strand a live flight container with nothing tracking it, and that a freshly
# started orchestrator really does read it back from Docker before serving a
# single request.
#
# Two flights are opened before the crash, to exercise both branches of recover():
#   - flight A's container is still running when the new orchestrator starts —
#     it must be reattached and behave exactly like any other live flight
#     afterwards (offline hook tears it down normally).
#   - flight B's container is stopped WHILE the orchestrator is down (simulating
#     it exiting on its own with nobody watching) — it must be closed out and
#     removed by recovery itself, since no offline hook is coming for it.
#
# Host ports 61935/61802 are used so this cannot collide with a running compose
# stack. Needs ffmpeg on the host and access to the Docker socket.
#
# Usage:  ./run_orchestrator_recovery.sh
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NET=orchrec-net
DBW_IMAGE=orchrec-dbw
ORC_IMAGE=orchrec-orchestrator
STUB_IMAGE=orchrec-stub-app
RTMP=localhost:61935
API=http://localhost:61802
GRACE=5

cleanup() {
  docker rm -f orchrec-mediamtx orchestrator db-writer orchrec-pg >/dev/null 2>&1 || true
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

# Same stub as run_orchestrator.sh: traps SIGTERM so a graceful `docker stop`
# during teardown does not block for the full stop timeout.
STUB_CTX=$(mktemp -d)
cat > "$STUB_CTX/Dockerfile" <<'EOF'
FROM alpine:3.20
CMD ["sh", "-c", "trap 'exit 0' TERM INT; while :; do sleep 0.2; done"]
EOF
docker build -q -t "$STUB_IMAGE" "$STUB_CTX" >/dev/null || exit 1
rm -rf "$STUB_CTX"

docker network create "$NET" >/dev/null 2>&1 || true
docker run -d --name orchrec-pg --network "$NET" \
  -e POSTGRES_PASSWORD=testpw -e POSTGRES_USER=testuser -e POSTGRES_DB=testdb \
  postgres:16-alpine >/dev/null

echo "==> waiting for postgres"
for _ in $(seq 1 30); do
  docker exec orchrec-pg pg_isready -U testuser >/dev/null 2>&1 && break
  sleep 1
done

SECRET=$(openssl rand -hex 32)
ENVV=(-e DB_SERVICE=postgresql -e DB_HOST=orchrec-pg -e DB_PORT=5432 -e DB_NAME=testdb
      -e DB_WORKER_NAME=testuser -e DB_WORKER_PASSWORD=testpw
      -e SESSION_JWT_SECRET="$SECRET")

echo "==> seeding two streams (two flights are opened before the crash)"
SEED=$(docker run --rm --network "$NET" "${ENVV[@]}" "$DBW_IMAGE" \
  sh -c "echo yes | python rebuild_schema.py --drop --seed-user pilot@test.io --seed-password pw123" 2>&1)
KEY_A=$(echo "$SEED" | sed -n 's/.*stream key : \([a-z0-9]*\).*/\1/p')
KEY_B=$(docker run --rm --network "$NET" "${ENVV[@]}" "$DBW_IMAGE" python -c "
from db_manager import UserDirectory
d = UserDirectory('postgresql://testuser:testpw@orchrec-pg:5432/testdb')
print(d.create_stream(1, 'second slot')['stream_key'])
" 2>/dev/null | tail -1)
[ -n "$KEY_A" ] && [ -n "$KEY_B" ] || { echo "seeding failed"; exit 1; }
echo "    key A = $KEY_A   key B = $KEY_B"

start_orchestrator() {
  docker run -d --name orchestrator --network "$NET" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -e APP_IMAGE="$STUB_IMAGE" -e DB_WRITER_URL=http://db-writer:8000 \
    -e APP_NETWORK="$NET" -e RECONNECT_GRACE_S="$GRACE" \
    "$ORC_IMAGE" >/dev/null
}

echo "==> starting db-writer, orchestrator, mediamtx"
docker run -d --name db-writer --network "$NET" -p 61802:8000 "${ENVV[@]}" "$DBW_IMAGE" >/dev/null \
  || { echo "db-writer failed to start"; exit 1; }
start_orchestrator || { echo "orchestrator failed to start"; exit 1; }
docker run -d --name orchrec-mediamtx --network "$NET" -p 61935:1935 \
  -v "$REPO/configs/mediamtx/mediamtx.yaml:/mediamtx.yml:ro" \
  bluenviron/mediamtx:latest-ffmpeg >/dev/null \
  || { echo "mediamtx failed to start (is a previous run still holding 61935?)"; exit 1; }
sleep 6

for c in db-writer orchestrator orchrec-mediamtx; do
  [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = "true" ] \
    || { echo "$c exited during startup:"; docker logs "$c" 2>&1 | tail -20; exit 1; }
done

flight_containers() { docker ps -q --filter "label=agrarian.flight_id" | wc -l | tr -d ' '; }
active_flights()    { docker exec orchestrator python -c \
  "import urllib.request,json;print(json.load(urllib.request.urlopen('http://localhost:8000/health'))['active_flights'])" 2>/dev/null; }
sql() { docker exec orchrec-pg psql -U testuser -d testdb -tAc "$1" 2>/dev/null | tr -d ' '; }

publish_bg() {  # publish_bg <key> <seconds>
  ffmpeg -hide_banner -loglevel error -re -f lavfi -i testsrc=size=320x240:rate=15 \
    -t "$2" -c:v libx264 -preset ultrafast -tune zerolatency -g 15 \
    -f flv "rtmp://$RTMP/in/$1" >/dev/null 2>&1 &
  echo $!
}

echo
echo "── two flights are live before the crash ───────────────────────────────────"
FFA=$(publish_bg "$KEY_A" 90)
FFB=$(publish_bg "$KEY_B" 90)
sleep 10
check "two flight containers are running"  2 "$(flight_containers)"
check "orchestrator tracks two flights"     2 "$(active_flights)"

FID_A=$(sql "SELECT flight_id FROM flights WHERE end_time IS NULL ORDER BY flight_id ASC LIMIT 1;")
FID_B=$(sql "SELECT flight_id FROM flights WHERE end_time IS NULL ORDER BY flight_id DESC LIMIT 1;")
CID_A=$(docker ps -q --filter "label=agrarian.flight_id=$FID_A")
CID_B=$(docker ps -q --filter "label=agrarian.flight_id=$FID_B")
[ -n "$CID_A" ] && [ -n "$CID_B" ] && [ "$FID_A" != "$FID_B" ] \
  || { echo "could not identify both flight containers (A=$FID_A/$CID_A B=$FID_B/$CID_B)"; exit 1; }

echo
echo "── the orchestrator is KILLED, not stopped — no graceful shutdown ──────────"
docker kill orchestrator >/dev/null 2>&1
docker rm -f orchestrator >/dev/null 2>&1
check "both containers survive the crash (nothing tore them down)" 2 "$(flight_containers)"

echo
echo "── flight B's container exits on its own while nobody is watching ─────────"
docker stop -t 2 "$CID_B" >/dev/null 2>&1
sleep 1
check "flight B's container is exited but not yet removed" 1 \
      "$(docker ps -aq --filter "id=$CID_B" | wc -l | tr -d ' ')"
check "flight A's container is still running"               1 \
      "$(docker ps -q --filter "id=$CID_A" | wc -l | tr -d ' ')"

echo
echo "── a fresh orchestrator starts and recovers before serving any request ────"
start_orchestrator || { echo "orchestrator failed to restart"; exit 1; }
sleep 6
[ "$(docker inspect -f '{{.State.Running}}' orchestrator 2>/dev/null)" = "true" ] \
  || { echo "orchestrator exited on restart:"; docker logs orchestrator 2>&1 | tail -30; exit 1; }

check "recovered orchestrator tracks flight A as active" 1 "$(active_flights)"
check "flight B's exited container was removed by recovery" 0 \
      "$(docker ps -aq --filter "id=$CID_B" | wc -l | tr -d ' ')"
check "flight B's row was closed by recovery (end_time set)" 1 \
      "$(sql "SELECT COUNT(*) FROM flights WHERE flight_id=$FID_B AND end_time IS NOT NULL;")"
check "flight A's row is still open (it is still running)" 1 \
      "$(sql "SELECT COUNT(*) FROM flights WHERE flight_id=$FID_A AND end_time IS NULL;")"
check "recovery did not spawn a duplicate container for flight A" 1 \
      "$(flight_containers)"

echo
echo "── the recovered flight A behaves exactly like a normal one afterwards ─────"
kill "$FFA" >/dev/null 2>&1; wait "$FFA" 2>/dev/null
kill "$FFB" >/dev/null 2>&1; wait "$FFB" 2>/dev/null
sleep $((GRACE + 12))
check "flight A's container is gone after landing" 0 "$(flight_containers)"
check "flight A's row is closed after landing"     1 \
      "$(sql "SELECT COUNT(*) FROM flights WHERE flight_id=$FID_A AND end_time IS NOT NULL;")"
check "no flights left open in the database" 0 "$(sql "SELECT COUNT(*) FROM flights WHERE end_time IS NULL;")"

echo
echo "=========================================================="
echo "$pass/$((pass+fail)) passed"
[ "$fail" -eq 0 ] || { echo; echo "--- orchestrator log ---"; docker logs orchestrator 2>&1 | tail -40; exit 1; }

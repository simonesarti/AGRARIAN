#!/usr/bin/env bash
# End-to-end flight lifecycle with the REAL GPU app container, not the sleeping stub.
#
# run_orchestrator.sh proves the orchestration (spawn/teardown/env injection) against
# a stub that only holds still. This proves the thing that file cannot: that the real
# app image, given the orchestrator-injected FLIGHT_ID/PUBLISHER_TOKEN/stream paths,
# actually reads in/<stream_key> from MediaMTX, runs the pipeline, and publishes
# annotated video to out/<public_uuid> — with real db-writer, ws-server and Redis
# behind it.
#
# Needs: a GPU + nvidia-container-toolkit (docker run --gpus all must work), ffmpeg
# on the host, Docker socket access, and the checkpoints in ./checkpoints (gitignored,
# expected to already be on disk — see checkpoints/.gitkeep).
#
# Host ports 41935/48002 are used so this cannot collide with a running compose stack
# or with run_orchestrator.sh.
#
# Usage:  ./run_orchestrator_real_app.sh
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NET=orchreal-net
APP_IMAGE=orchreal-app
DBW_IMAGE=orchreal-dbw
ORC_IMAGE=orchreal-orchestrator
WSS_IMAGE=orchreal-wss
RTMP=localhost:41935
API=http://localhost:48002
GRACE=10

cleanup() {
  docker rm -f orchreal-mediamtx orchestrator db-writer ws-server redis orchreal-pg >/dev/null 2>&1 || true
  docker ps -aq --filter "label=agrarian.flight_id" | xargs -r docker rm -f >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT

pass=0; fail=0
check() {  # check <name> <expected> <actual>
  if [ "$2" = "$3" ]; then echo "PASS  $1   [$3]"; pass=$((pass+1))
  else echo "FAIL  $1   [expected $2, got $3]"; fail=$((fail+1)); fi
}

command -v ffmpeg >/dev/null || { echo "missing required tool: ffmpeg"; exit 1; }
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi >/dev/null 2>&1 \
  || { echo "docker --gpus all does not work on this host — is nvidia-container-toolkit installed?"; exit 1; }
[ -f "$REPO/checkpoints/detection_1280_720_yolo11m.pt" ] && [ -f "$REPO/checkpoints/hm_ae_5fps.pt" ] \
  || { echo "missing checkpoints/*.pt — health_monitoring cannot start without them"; exit 1; }

echo "==> building images (app image reuses cached layers if already built)"
docker build -q -f "$REPO/app/Dockerfile" -t "$APP_IMAGE" "$REPO" >/dev/null || exit 1
docker build -q -t "$DBW_IMAGE" "$REPO/db_writer" >/dev/null || exit 1
docker build -q -t "$ORC_IMAGE" "$REPO/orchestrator" >/dev/null || exit 1
docker build -q -t "$WSS_IMAGE" "$REPO/ws_server" >/dev/null || exit 1

docker network create "$NET" >/dev/null 2>&1 || true

docker run -d --name orchreal-pg --network "$NET" \
  -e POSTGRES_PASSWORD=testpw -e POSTGRES_USER=testuser -e POSTGRES_DB=testdb \
  postgres:16-alpine >/dev/null
docker run -d --name redis --network "$NET" \
  redis:7-alpine redis-server --save "" --appendonly no >/dev/null

echo "==> waiting for postgres"
for _ in $(seq 1 30); do
  docker exec orchreal-pg pg_isready -U testuser >/dev/null 2>&1 && break
  sleep 1
done

SECRET=$(openssl rand -hex 32)
ENVV=(-e DB_SERVICE=postgresql -e DB_HOST=orchreal-pg -e DB_PORT=5432 -e DB_NAME=testdb
      -e DB_WORKER_NAME=testuser -e DB_WORKER_PASSWORD=testpw
      -e SESSION_JWT_SECRET="$SECRET")

echo "==> seeding a tenant"
SEED=$(docker run --rm --network "$NET" "${ENVV[@]}" "$DBW_IMAGE" \
  sh -c "echo yes | python rebuild_schema.py --drop --seed-user pilot@test.io --seed-password pw123" 2>&1)
KEY=$(echo "$SEED" | sed -n 's/.*stream key : \([a-z0-9]*\).*/\1/p')
[ -n "$KEY" ] || { echo "seeding failed:"; echo "$SEED"; exit 1; }
echo "    stream key = $KEY"

echo "==> starting db-writer, ws-server, orchestrator, mediamtx"
docker run -d --name db-writer --network "$NET" -p 48002:8000 "${ENVV[@]}" "$DBW_IMAGE" >/dev/null \
  || { echo "db-writer failed to start"; exit 1; }
docker run -d --name ws-server --network "$NET" \
  -e WS_PORT=8765 -e REDIS_URL=redis://redis:6379/0 -e SESSION_JWT_SECRET="$SECRET" \
  "$WSS_IMAGE" >/dev/null || { echo "ws-server failed to start"; exit 1; }
docker run -d --name orchestrator --network "$NET" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e APP_IMAGE="$APP_IMAGE" -e DB_WRITER_URL=http://db-writer:8000 \
  -e APP_NETWORK="$NET" -e APP_GPUS=all -e APP_SHM_SIZE=256m \
  -e RECONNECT_GRACE_S="$GRACE" \
  -e APP_ENV_APP_MODE=health_monitoring \
  -e APP_ENV_DB_WRITER_URL=http://db-writer:8000 \
  -e APP_ENV_WS_SERVER_URL=http://ws-server:8000 \
  -e APP_ENV_VIDEO_STREAM_READER_HOST=orchreal-mediamtx \
  -e APP_ENV_VIDEO_OUT_STREAM_HOST=orchreal-mediamtx \
  "$ORC_IMAGE" >/dev/null || { echo "orchestrator failed to start"; exit 1; }
docker run -d --name orchreal-mediamtx --network "$NET" -p 41935:1935 \
  -v "$REPO/configs/mediamtx/mediamtx.yaml:/mediamtx.yml:ro" \
  bluenviron/mediamtx:latest-ffmpeg >/dev/null \
  || { echo "mediamtx failed to start (is a previous run still holding 41935?)"; exit 1; }
sleep 6

for c in db-writer ws-server orchestrator orchreal-mediamtx; do
  [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = "true" ] \
    || { echo "$c exited during startup:"; docker logs "$c" 2>&1 | tail -20; exit 1; }
done

flight_containers() { docker ps -q --filter "label=agrarian.flight_id" | wc -l | tr -d ' '; }
sql() { docker exec orchreal-pg psql -U testuser -d testdb -tAc "$1" 2>/dev/null | tr -d ' '; }

echo
echo "── a stream going live spawns the real app container ──────────────────────"
check "no flight containers before takeoff" 0 "$(flight_containers)"

# 1920x1080 to match VIDEO_STREAM_READER_ORIGINAL_SHAPE / 16:9 expected aspect ratio.
ffmpeg -hide_banner -loglevel error -re -f lavfi -i testsrc=size=1920x1080:rate=30 \
  -t 90 -c:v libx264 -preset ultrafast -tune zerolatency -g 30 \
  -f flv "rtmp://$RTMP/in/$KEY" >/dev/null 2>&1 &
FFPID=$!

echo "==> waiting for the app container to appear and models to load (up to 60s)"
CID=""
for _ in $(seq 1 60); do
  CID=$(docker ps -q --filter "label=agrarian.flight_id" | head -1)
  [ -n "$CID" ] && break
  sleep 1
done
check "one flight container is running" 1 "$(flight_containers)"

if [ -n "$CID" ]; then
  echo "==> letting the pipeline run (model load + a few seconds of frames)"
  sleep 45
  echo "──── app container logs (tail) ────"
  docker logs "$CID" 2>&1 | tail -60
  echo "────────────────────────────────────"

  CRASHED=$(docker logs "$CID" 2>&1 | grep -c "CRITICAL")
  check "no CRITICAL lines in the app log" 0 "$CRASHED"
  check "app container still running (did not crash)" 1 "$(flight_containers)"

  UUID=$(sql 'SELECT public_uuid FROM flights ORDER BY flight_id DESC LIMIT 1;')
  PUBLISHED=$(docker logs orchreal-mediamtx 2>&1 | grep -c "is publishing to path 'out/$UUID'")
  check "app published annotated output to its own out/<uuid>" 1 "$( [ "$PUBLISHED" -ge 1 ] && echo 1 || echo "$PUBLISHED")"
else
  echo "FAIL  app container never appeared — dumping orchestrator log"
  docker logs orchestrator 2>&1 | tail -40
  fail=$((fail+1))
fi

kill $FFPID >/dev/null 2>&1; wait $FFPID 2>/dev/null

echo
echo "── the drone lands, the app container is torn down ────────────────────────"
sleep $((GRACE + 15))
check "flight container is gone"              0 "$(flight_containers)"
check "flight row is closed (end_time set)"   1 "$(sql 'SELECT COUNT(*) FROM flights WHERE end_time IS NOT NULL;')"

echo
echo "=========================================================="
echo "$pass/$((pass+fail)) passed"
[ "$fail" -eq 0 ] || { echo; echo "--- orchestrator log ---"; docker logs orchestrator 2>&1 | tail -30; exit 1; }

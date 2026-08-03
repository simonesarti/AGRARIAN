#!/usr/bin/env bash
# End-to-end flight lifecycle with the REAL GPU app container, not the sleeping stub.
#
# run_orchestrator.sh proves the orchestration (spawn/teardown/env injection) against
# a stub that only holds still. This proves the thing that file cannot: that the real
# app image, given the orchestrator-injected FLIGHT_ID/PUBLISHER_TOKEN/stream paths,
# actually reads in/<stream_key> from MediaMTX, runs the pipeline, and publishes
# annotated video to out/<public_uuid> — with real db-writer, ws-server, Mosquitto and
# Redis behind it.
#
# MODE. Takes the app mode as its first argument and defaults to danger_detection,
# which is the mode this file used to be unable to run at all: it hardcoded
# health_monitoring, so the primary product mode had never executed once and the
# telemetry plane had never carried a message from the real app. The two modes differ
# in more than a model — health_monitoring does not instantiate FrameTelemetryCombiner
# at all, so Mosquitto is dead weight there and load-bearing here.
#
# TELEMETRY. danger_detection's GeoWorker needs position, altitude and gimbal yaw for
# every frame; without them it emits empty masks and the whole geo/danger half of the
# pipeline is exercised in name only. So this starts a real broker with the real
# mosquitto-go-auth plugin against the real db-writer ACL endpoint, and a publisher
# authenticating as the drone with its stream key. Telemetry arriving is asserted, not
# assumed, because a broker that authenticates and then delivers nothing looks exactly
# like one that was never contacted.
#
# LOGS. The app configures no StreamHandler (app/main.py), so `docker logs` on a flight
# container shows almost nothing — only tracebacks that escape logging entirely. Every
# assertion about what the pipeline did therefore reads /app/logs/*.log out of the
# container while it is still alive. Checking `docker logs` for CRITICAL, as this file
# used to, is an assertion that cannot fail.
#
# DEM. dem/dem.tif is gitignored and usually absent. open_dem_tifs() returns None for a
# missing raster and the GeoWorker skips slope and no-data analysis, so danger_detection
# runs without it — geofencing and the safety radius still work. Reported below so a
# green run is not mistaken for full geo coverage.
#
# Needs: a GPU + nvidia-container-toolkit (docker run --gpus all must work), ffmpeg
# on the host, Docker socket access, and the checkpoints in ./checkpoints (gitignored,
# expected to already be on disk — see checkpoints/.gitkeep).
#
# Host ports 41935/48002 are used so this cannot collide with a running compose stack
# or with run_orchestrator.sh.
#
# Usage:  ./run_orchestrator_real_app.sh [danger_detection|health_monitoring]
set -uo pipefail

MODE="${1:-danger_detection}"
case "$MODE" in
  danger_detection|health_monitoring) ;;
  *) echo "unknown app mode '$MODE' — expected danger_detection or health_monitoring"; exit 1 ;;
esac

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# MediaMTX terminates its own TLS now (§7), and it EXITS at startup if the
# certificate its config points at is not on disk — "open /certs/server.crt: no
# such file or directory", then "[RTSP] closing". So every runner that mounts the
# real mediamtx.yaml has to supply one, whether or not it tests TLS: without it
# this whole file fails as a wall of connection errors that look like networking.
#
# Issued into a throwaway directory rather than into certificates/, whose CA may
# already be installed in somebody's browser.
CERTS="$(mktemp -d)"
CERT_DIR="$CERTS" "$REPO/scripts/generate_local_certs.sh" >/dev/null \
  || { echo "could not issue a local certificate for MediaMTX"; exit 1; }
NET=orchreal-net
APP_IMAGE=orchreal-app
DBW_IMAGE=orchreal-dbw
ORC_IMAGE=orchreal-orchestrator
WSS_IMAGE=orchreal-wss
MOSQ_IMAGE=iegomez/mosquitto-go-auth:2.1.0-mosquitto_2.0.15
RTMP=localhost:41935
API=http://localhost:48002
GRACE=10
LOGDIR=$(mktemp -d)

cleanup() {
  rm -rf "$CERTS"
  docker rm -f orchreal-mediamtx orchreal-mosquitto orchreal-telemetry \
                orchestrator db-writer ws-server redis orchreal-pg >/dev/null 2>&1 || true
  docker ps -aq --filter "label=agrarian.flight_id" | xargs -r docker rm -f >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -rf "$LOGDIR"
}
trap cleanup EXIT

pass=0; fail=0
check() {  # check <name> <expected> <actual>
  if [ "$2" = "$3" ]; then echo "PASS  $1   [$3]"; pass=$((pass+1))
  else echo "FAIL  $1   [expected $2, got $3]"; fail=$((fail+1)); fi
}
check_atleast() {  # check_atleast <name> <minimum> <actual>
  if [ "${3:-0}" -ge "$2" ] 2>/dev/null; then echo "PASS  $1   [$3 >= $2]"; pass=$((pass+1))
  else echo "FAIL  $1   [expected >= $2, got ${3:-0}]"; fail=$((fail+1)); fi
}
# grep -c prints 0 AND exits 1 when it matches nothing, so the usual `|| echo 0`
# emits two lines and every comparison against it fails on a count of zero. A
# missing file exits 2 and prints nothing at all, which is a different wrong
# answer. Both are normal here — a log file only exists if its worker started.
count_in() {  # count_in <file> <pattern>
  local n=""
  [ -f "$1" ] && n=$(grep -c "$2" "$1" 2>/dev/null || true)
  echo "${n:-0}"
}

echo "==> app mode: $MODE"

command -v ffmpeg >/dev/null || { echo "missing required tool: ffmpeg"; exit 1; }
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi >/dev/null 2>&1 \
  || { echo "docker --gpus all does not work on this host — is nvidia-container-toolkit installed?"; exit 1; }

# Checkpoints are per-mode. health_monitoring wants the trajectory autoencoder;
# danger_detection wants the segmentation ONNX. Both want the detector.
missing=""
[ -f "$REPO/checkpoints/detection_1280_720_yolo11m.pt" ] || missing="$missing detection_1280_720_yolo11m.pt"
if [ "$MODE" = "health_monitoring" ]; then
  [ -f "$REPO/checkpoints/hm_ae_5fps.pt" ] || missing="$missing hm_ae_5fps.pt"
else
  [ -f "$REPO/checkpoints/best_model_segunified_1280_720.onnx" ] \
    || missing="$missing best_model_segunified_1280_720.onnx"
fi
[ -z "$missing" ] || { echo "missing checkpoints/ files for $MODE:$missing"; exit 1; }

if [ "$MODE" = "danger_detection" ]; then
  if [ -f "$REPO/dem/dem.tif" ]; then
    echo "==> dem/dem.tif present — slope and no-data analysis will run"
  else
    echo "==> NOTE: no dem/dem.tif — the GeoWorker will skip slope and no-data masks."
    echo "    Geofencing and the safety radius still run; this is a supported degraded mode."
  fi
fi

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
  sh -c "echo yes | python rebuild_schema.py --drop --seed-user pilot@test.io --seed-password pw12345678" 2>&1)
KEY=$(echo "$SEED" | sed -n 's/.*stream key : \([a-z0-9]*\).*/\1/p')
[ -n "$KEY" ] || { echo "seeding failed:"; echo "$SEED"; exit 1; }
echo "    stream key = $KEY"

echo "==> starting db-writer, ws-server, orchestrator, mosquitto, mediamtx"
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
  -e APP_ENV_APP_MODE="$MODE" \
  -e APP_ENV_DB_WRITER_URL=http://db-writer:8000 \
  -e APP_ENV_WS_SERVER_URL=http://ws-server:8000 \
  -e APP_ENV_VIDEO_STREAM_READER_HOST=orchreal-mediamtx \
  -e APP_ENV_VIDEO_OUT_STREAM_HOST=orchreal-mediamtx \
  -e APP_ENV_TELEMETRY_LISTENER_HOST=orchreal-mosquitto \
  -e APP_ENV_LOG_LEVEL=INFO \
  "$ORC_IMAGE" >/dev/null || { echo "orchestrator failed to start"; exit 1; }

# The go-auth plugin calls db-writer, and mosquitto.conf names it 'db-writer' —
# which is why the container above carries that exact name on this network. The
# real config is mounted, not a test copy, so the ACLs proved by run_mqtt_auth.sh
# are the ones enforced here.
docker run -d --name orchreal-mosquitto --network "$NET" \
  -v "$REPO/configs/mosquitto/mosquitto.conf:/etc/mosquitto/mosquitto.conf:ro" \
  -v "$CERTS/server:/mosquitto/certs:ro" \
  "$MOSQ_IMAGE" >/dev/null || { echo "mosquitto failed to start"; exit 1; }

docker run -d --name orchreal-mediamtx --network "$NET" -p 41935:1935 \
  -v "$REPO/configs/mediamtx/mediamtx.yaml:/mediamtx.yml:ro" \
  -v "$CERTS/server:/certs:ro" \
  bluenviron/mediamtx:latest-ffmpeg >/dev/null \
  || { echo "mediamtx failed to start (is a previous run still holding 41935?)"; exit 1; }
sleep 6

for c in db-writer ws-server orchestrator orchreal-mosquitto orchreal-mediamtx; do
  [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = "true" ] \
    || { echo "$c exited during startup:"; docker logs "$c" 2>&1 | tail -20; exit 1; }
done

flight_containers() { docker ps -q --filter "label=agrarian.flight_id" | wc -l | tr -d ' '; }
sql() { docker exec orchreal-pg psql -U testuser -d testdb -tAc "$1" 2>/dev/null | tr -d ' '; }

echo
echo "── a stream going live spawns the real app container ──────────────────────"
check "no flight containers before takeoff" 0 "$(flight_containers)"

if [ "$MODE" = "danger_detection" ]; then
  # Starts BEFORE the video, so the combiner's buffer already holds snapshots when
  # the first frames arrive rather than dropping them for want of a match. Runs in
  # the app image because that is where aiomqtt already lives.
  echo "==> starting the telemetry publisher (drone credential = stream key)"
  docker run -d --name orchreal-telemetry --network "$NET" \
    -v "$REPO/tests/comms/telemetry_publisher.py:/telemetry_publisher.py:ro" \
    --entrypoint python3 "$APP_IMAGE" \
    /telemetry_publisher.py --host orchreal-mosquitto --stream-key "$KEY" --hz 10 >/dev/null \
    || { echo "telemetry publisher failed to start"; exit 1; }
  sleep 4
  TELCONN=$(docker logs orchreal-telemetry 2>&1 | grep -c "^connected to")
  check "the drone's telemetry client authenticated against the real ACL" 1 "$TELCONN"
  if [ "$TELCONN" != "1" ]; then
    echo "──── telemetry publisher log ────"; docker logs orchreal-telemetry 2>&1 | tail -20
    echo "──── mosquitto log ────";           docker logs orchreal-mosquitto 2>&1 | tail -20
  fi
fi

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

  # Copied out while the container is alive: teardown removes it, and these files
  # are the only place the pipeline says anything about itself.
  docker cp "$CID:/app/logs" "$LOGDIR/app" >/dev/null 2>&1 \
    || echo "WARNING: could not copy /app/logs out of the flight container"
  echo "──── main.log (tail) ────"
  tail -30 "$LOGDIR/app/main.log" 2>/dev/null || echo "(no main.log)"
  echo "──────────────────────────"

  applog() { cat "$LOGDIR/app"/*.log 2>/dev/null; }

  check "the app started the requested pipeline" 1 \
    "$(applog | grep -c "Starting ${MODE//_/ } pipeline")"

  CRIT=$(applog | grep -c "CRITICAL")
  if [ "$CRIT" != "0" ]; then
    echo "──── CRITICAL lines ────"; applog | grep "CRITICAL" | head -20; echo "────────────────────────"
  fi
  check "no CRITICAL lines in the app's own logs" 0 "$CRIT"
  check "no traceback escaped to the container's stderr" 0 \
    "$(docker logs "$CID" 2>&1 | grep -c "Traceback")"
  check "app container still running (did not crash)" 1 "$(flight_containers)"

  UUID=$(sql 'SELECT public_uuid FROM flights ORDER BY flight_id DESC LIMIT 1;')
  PUBLISHED=$(docker logs orchreal-mediamtx 2>&1 | grep -c "is publishing to path 'out/$UUID'")
  check "app published annotated output to its own out/<uuid>" 1 "$( [ "$PUBLISHED" -ge 1 ] && echo 1 || echo "$PUBLISHED")"

  if [ "$MODE" = "danger_detection" ]; then
    echo
    echo "── the telemetry plane carries a real message ─────────────────────────────"
    COMB="$LOGDIR/app/frame_telemetry_combiner.log"

    # The app subscribes with its PUBLISHER TOKEN, not the stream key: a different
    # credential from the publisher above, admitted to the same topics by the ACL.
    check_atleast "the app subscribed to all four of its telemetry topics" 4 \
      "$(count_in "$COMB" "subscribed to 'telemetry/$KEY/")"

    # The load-bearing assertion. 'No telemetry available for matching' is logged
    # whenever the buffer is empty at match time, so it is expected during startup
    # and must stop once snapshots are flowing. Still present in the last 200 lines
    # means the broker accepted the publisher and the app subscribed and nothing
    # crossed between them — the exact failure that authorisation tests cannot see.
    # Guarded on the file existing at all: a missing combiner log would otherwise
    # make an empty tail look like a healthy run.
    check "the combiner logged anything at all" 1 "$( [ -s "$COMB" ] && echo 1 || echo 0)"
    STARVED=$(tail -200 "$COMB" 2>/dev/null | grep -c "No telemetry available for matching" || true)
    check "telemetry is reaching the combiner (not starved at the end of the run)" 0 "${STARVED:-0}"

    check "no MQTT errors after the initial connect" 0 \
      "$(count_in "$COMB" "MQTT error\|MQTT connection refused\|MQTT unexpected error")"

    # The geo stage is what consumes telemetry; if it never ran, the mode was
    # exercised in name only however healthy everything upstream looks. This line
    # is logged on the first frame it actually receives, not at process start, so
    # it proves frames reached the far side of the pipeline.
    check_atleast "frames reached the geo worker" 1 \
      "$(count_in "$LOGDIR/app/danger_geo.log" "Geo worker setup with frame size")"
  fi
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
echo "$MODE: $pass/$((pass+fail)) passed"
[ "$fail" -eq 0 ] || { echo; echo "--- orchestrator log ---"; docker logs orchestrator 2>&1 | tail -30; exit 1; }

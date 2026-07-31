#!/usr/bin/env bash
# A live flight with a browser at the end of it. The one thing no automated test
# here can reach.
#
# Everything else in this directory asserts against logs, databases and HTTP status
# codes. Playback cannot be checked that way: the watch page embeds MediaMTX's own
# reader page in an iframe rather than negotiating WHEP itself, so whether a viewer
# sees video depends on MediaMTX's client JavaScript carrying the ?jwt= query
# through to its own WHEP request. That is a browser behaviour, and it needs eyes.
#
# So this script is not a test — it stands the whole product up, puts a flight in
# the air, prints a URL, and waits. It exits when you press Enter, cleaning up
# every container.
#
# PLAIN HTTP. The portal ships COOKIE_SECURE=true, and a browser will not return a
# Secure cookie over http:// — the symptom is a login that appears to work and then
# forgets you. There is no TLS terminator in this repo yet (CLOUD_ARCHITECTURE.md
# §7), so this runs with the local-HTTP affordance switched on. That is exactly the
# configuration §8 says is not deployable, and it is fine here and nowhere else.
#
# HOST ADDRESS. The portal composes playback URLs from MEDIA_PUBLIC_HOST, and
# MediaMTX advertises MTX_WEBRTCICEHOSTNAT1TO1IPS in its ICE candidates. Both must
# be an address YOUR BROWSER can dial. Defaults to 127.0.0.1 for a browser on this
# machine; pass the LAN IP as the first argument to watch from another device.
#
# Needs: a GPU + nvidia-container-toolkit, ffmpeg on the host, Docker socket access,
# and the checkpoints in ./checkpoints.
#
# Usage:  ./run_watch_live.sh [host-address]
set -uo pipefail

HOST="${1:-127.0.0.1}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NET=watch-net
APP_IMAGE=watch-app
DBW_IMAGE=watch-dbw
ORC_IMAGE=watch-orchestrator
WSS_IMAGE=watch-wss
PORTAL_IMAGE=watch-portal
MOSQ_IMAGE=iegomez/mosquitto-go-auth:2.1.0-mosquitto_2.0.15
EMAIL=pilot@test.io
PASSWORD=pw12345678

cleanup() {
  echo
  echo "==> cleaning up"
  kill "${FFPID:-}" >/dev/null 2>&1
  docker rm -f watch-mediamtx watch-mosquitto watch-telemetry watch-portal \
                watch-orchestrator watch-dbw watch-wss watch-redis watch-pg >/dev/null 2>&1 || true
  docker ps -aq --filter "label=agrarian.flight_id" | xargs -r docker rm -f >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT

command -v ffmpeg >/dev/null || { echo "missing required tool: ffmpeg"; exit 1; }
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi >/dev/null 2>&1 \
  || { echo "docker --gpus all does not work on this host"; exit 1; }
[ -f "$REPO/checkpoints/detection_1280_720_yolo11m.pt" ] \
  && [ -f "$REPO/checkpoints/best_model_segunified_1280_720.onnx" ] \
  || { echo "missing checkpoints/ files for danger_detection"; exit 1; }

for p in 8003 8888 8889 8765 1935; do
  if (exec 3<>"/dev/tcp/127.0.0.1/$p") 2>/dev/null; then
    exec 3<&- 3>&-
    echo "port $p is already in use — is a compose stack or a previous run still up?"
    exit 1
  fi
done

echo "==> building images"
for spec in "$DBW_IMAGE:db_writer" "$ORC_IMAGE:orchestrator" "$WSS_IMAGE:ws_server" "$PORTAL_IMAGE:portal"; do
  docker build -q -t "${spec%%:*}" "$REPO/${spec##*:}" >/dev/null || exit 1
done
docker build -q -f "$REPO/app/Dockerfile" -t "$APP_IMAGE" "$REPO" >/dev/null || exit 1

docker network create "$NET" >/dev/null 2>&1 || true
docker run -d --name watch-pg --network "$NET" \
  -e POSTGRES_PASSWORD=testpw -e POSTGRES_USER=testuser -e POSTGRES_DB=testdb \
  postgres:16-alpine >/dev/null
docker run -d --name watch-redis --network "$NET" \
  redis:7-alpine redis-server --save "" --appendonly no >/dev/null

echo "==> waiting for postgres"
for _ in $(seq 1 30); do
  docker exec watch-pg pg_isready -U testuser >/dev/null 2>&1 && break
  sleep 1
done

SECRET=$(openssl rand -hex 32)
ENVV=(-e DB_SERVICE=postgresql -e DB_HOST=watch-pg -e DB_PORT=5432 -e DB_NAME=testdb
      -e DB_WORKER_NAME=testuser -e DB_WORKER_PASSWORD=testpw
      -e SESSION_JWT_SECRET="$SECRET")

echo "==> seeding the account you will sign in as"
SEED=$(docker run --rm --network "$NET" "${ENVV[@]}" "$DBW_IMAGE" \
  sh -c "echo yes | python rebuild_schema.py --drop --seed-user $EMAIL --seed-password $PASSWORD" 2>&1)
KEY=$(echo "$SEED" | sed -n 's/.*stream key : \([a-z0-9]*\).*/\1/p')
[ -n "$KEY" ] || { echo "seeding failed:"; echo "$SEED"; exit 1; }

echo "==> starting the hub"
# db-writer is named 'db-writer' on this network because mosquitto.conf and
# mediamtx.yaml both name it that in their auth hooks.
docker run -d --name watch-dbw --network "$NET" --network-alias db-writer \
  "${ENVV[@]}" "$DBW_IMAGE" >/dev/null || { echo "db-writer failed"; exit 1; }
docker run -d --name watch-wss --network "$NET" --network-alias ws-server \
  -p 8765:8765 -e WS_PORT=8765 -e REDIS_URL=redis://watch-redis:6379/0 \
  -e SESSION_JWT_SECRET="$SECRET" "$WSS_IMAGE" >/dev/null || { echo "ws-server failed"; exit 1; }
docker run -d --name watch-mosquitto --network "$NET" \
  -v "$REPO/configs/mosquitto/mosquitto.conf:/etc/mosquitto/mosquitto.conf:ro" \
  "$MOSQ_IMAGE" >/dev/null || { echo "mosquitto failed"; exit 1; }

# The affordance §8 calls local-only. Without these two the login below succeeds
# and the very next page redirects you back to it.
docker run -d --name watch-portal --network "$NET" -p 8003:8000 \
  -e DB_WRITER_URL=http://db-writer:8000 \
  -e MEDIA_PUBLIC_HOST="$HOST" -e WS_PUBLIC_HOST="$HOST" -e WS_PORT=8765 \
  -e REDIS_URL=redis://watch-redis:6379/1 \
  -e COOKIE_SECURE=false -e PUBLIC_TLS=false \
  -e TRUSTED_PROXY_HOPS=0 \
  "$PORTAL_IMAGE" >/dev/null || { echo "portal failed"; exit 1; }

docker run -d --name watch-orchestrator --network "$NET" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  --network-alias orchestrator \
  -e APP_IMAGE="$APP_IMAGE" -e DB_WRITER_URL=http://db-writer:8000 \
  -e APP_NETWORK="$NET" -e APP_GPUS=all -e APP_SHM_SIZE=256m -e RECONNECT_GRACE_S=30 \
  -e APP_ENV_APP_MODE=danger_detection \
  -e APP_ENV_DB_WRITER_URL=http://db-writer:8000 \
  -e APP_ENV_WS_SERVER_URL=http://ws-server:8000 \
  -e APP_ENV_VIDEO_STREAM_READER_HOST=watch-mediamtx \
  -e APP_ENV_VIDEO_OUT_STREAM_HOST=watch-mediamtx \
  -e APP_ENV_TELEMETRY_LISTENER_HOST=watch-mosquitto \
  -e APP_ENV_LOG_LEVEL=INFO \
  "$ORC_IMAGE" >/dev/null || { echo "orchestrator failed"; exit 1; }

docker run -d --name watch-mediamtx --network "$NET" \
  -p 1935:1935 -p 8888:8888 -p 8889:8889 -p 8189:8189/udp \
  -e MTX_WEBRTCICEHOSTNAT1TO1IPS="$HOST" \
  -v "$REPO/configs/mediamtx/mediamtx.yaml:/mediamtx.yml:ro" \
  bluenviron/mediamtx:latest-ffmpeg >/dev/null || { echo "mediamtx failed"; exit 1; }
sleep 6

for c in watch-dbw watch-wss watch-mosquitto watch-portal watch-orchestrator watch-mediamtx; do
  [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = "true" ] \
    || { echo "$c exited during startup:"; docker logs "$c" 2>&1 | tail -20; exit 1; }
done

echo "==> starting telemetry"
docker run -d --name watch-telemetry --network "$NET" \
  -v "$REPO/tests/comms/telemetry_publisher.py:/telemetry_publisher.py:ro" \
  --entrypoint python3 "$APP_IMAGE" \
  /telemetry_publisher.py --host watch-mosquitto --stream-key "$KEY" --hz 10 >/dev/null

echo "==> taking off (a one-hour test pattern on rtmp://$HOST:1935/in/$KEY)"
ffmpeg -hide_banner -loglevel error -re -f lavfi -i testsrc=size=1920x1080:rate=30 \
  -t 3600 -c:v libx264 -preset ultrafast -tune zerolatency -g 30 \
  -f flv "rtmp://127.0.0.1:1935/in/$KEY" >/dev/null 2>&1 &
FFPID=$!

echo "==> waiting for the flight container and its first annotated frames (up to 90s)"
UUID=""; FLIGHT_ID=""
for _ in $(seq 1 90); do
  ROW=$(docker exec watch-pg psql -U testuser -d testdb -tAF' ' -c \
    'SELECT flight_id, public_uuid FROM flights ORDER BY flight_id DESC LIMIT 1;' 2>/dev/null)
  FLIGHT_ID=$(echo "$ROW" | awk '{print $1}')
  UUID=$(echo "$ROW" | awk '{print $2}')
  if [ -n "$UUID" ] && docker logs watch-mediamtx 2>&1 | grep -q "is publishing to path 'out/$UUID'"; then
    break
  fi
  sleep 1
done

if [ -z "$UUID" ]; then
  echo "no flight opened — dumping the orchestrator log"; docker logs watch-orchestrator 2>&1 | tail -30; exit 1
fi

cat <<BANNER

==============================================================================
  A flight is in the air.  out/$UUID

  1. Open   http://$HOST:8003
  2. Sign in with
         email     $EMAIL
         password  $PASSWORD
  3. The dashboard should show one stream slot with a "Live" badge.
     Click Watch.

  WHAT YOU ARE CHECKING — open devtools (F12) on the watch page first:

  a) Does video appear at all?
     That is the whole question. If it plays, browser playback is verified
     and CLOUD_ARCHITECTURE.md §9 loses its last product-path unknown.

  b) Network tab, filter "whep".
     The iframe loads MediaMTX's reader page at
         http://$HOST:8889/out/$UUID/?jwt=<token>
     and that page's own JavaScript then POSTs to .../whep. THE QUESTION IS
     WHETHER THAT POST CARRIES THE jwt. If it 401s, MediaMTX's reader dropped
     the query string, and the fix is to negotiate WHEP directly in
     portal/static/watch.js instead of iframing the reader.

  c) Network tab, filter "WS".
     One connection to ws://$HOST:8765/?token=... should sit at status 101.
     The alerts panel WILL STAY EMPTY — the input is a test pattern with
     nothing in it to detect. An open socket is the pass condition, not an
     alert. Send one by hand if you want to see the panel render — the
     channel is keyed by flight_id, not by the output path:

       docker exec watch-redis redis-cli -n 0 PUBLISH flight:$FLIGHT_ID \\
         '{"message":"hand-injected alert","timestamp":"now"}'

  d) Try the HLS URL directly if WebRTC fails — it is the same token on a
     different protocol, and tells you whether the problem is the credential
     or the WebRTC path:
         http://$HOST:8888/out/$UUID/index.m3u8?jwt=<token from the page>

  Logs while you look:
     docker logs -f watch-mediamtx        # auth decisions, reader connects
     docker logs -f watch-dbw             # the /auth/mediamtx answers

  Expected noise, not a fault: no recorder sidecar runs here, so MediaMTX's
  runOnRecordSegmentComplete hook has nowhere to POST. Recording upload has
  its own test (run_recording_upload.sh) and is not what you are checking.
==============================================================================

Press Enter to tear everything down.
BANNER

sleep 1200

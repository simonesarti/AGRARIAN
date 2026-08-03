#!/usr/bin/env bash
# End-to-end recording upload, against real MediaMTX, the recorder sidecar, db-writer
# and Postgres.
#
# Publishes directly to out/<public_uuid> (the annotated-output path) rather than
# spinning up the GPU app — the recording pipeline (segment -> hook -> recorder ->
# db-writer) does not depend on what publishes to that path, only on MediaMTX's
# `record: yes` config for it, which is identical either way.
#
# Two things are checked that the code alone cannot guarantee:
#   1. MediaMTX really calls the recorder on segment completion (on publisher
#      disconnect, since no test here runs long enough to hit recordSegmentDuration).
#   2. The recorder resolves the segment's output path back to a flight_id and the
#      row lands in db-writer's `recordings` table — not just that the file exists.
#
# Host ports 51935/58002 are used so this cannot collide with a running compose stack
# or with the other run_*.sh scripts. Needs ffmpeg on the host.
#
# Usage:  ./run_recording_upload.sh
set -uo pipefail

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
NET=recup-net
DBW_IMAGE=recup-dbw
REC_IMAGE=recorder
RTMP=localhost:51935
API=http://localhost:58002
VOL=recup-recordings

cleanup() {
  rm -rf "$CERTS"
  docker rm -f recup-mediamtx recorder db-writer recup-pg >/dev/null 2>&1 || true
  docker volume rm "$VOL" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
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
docker build -q -t "$REC_IMAGE" "$REPO/recorder" >/dev/null || exit 1

docker network create "$NET" >/dev/null 2>&1 || true
docker volume create "$VOL" >/dev/null

docker run -d --name recup-pg --network "$NET" \
  -e POSTGRES_PASSWORD=testpw -e POSTGRES_USER=testuser -e POSTGRES_DB=testdb \
  postgres:16-alpine >/dev/null

echo "==> waiting for postgres"
for _ in $(seq 1 30); do
  docker exec recup-pg pg_isready -U testuser >/dev/null 2>&1 && break
  sleep 1
done

SECRET=$(openssl rand -hex 32)
ENVV=(-e DB_SERVICE=postgresql -e DB_HOST=recup-pg -e DB_PORT=5432 -e DB_NAME=testdb
      -e DB_WORKER_NAME=testuser -e DB_WORKER_PASSWORD=testpw
      -e SESSION_JWT_SECRET="$SECRET")

echo "==> seeding a tenant"
SEED=$(docker run --rm --network "$NET" "${ENVV[@]}" "$DBW_IMAGE" \
  sh -c "echo yes | python rebuild_schema.py --drop --seed-user pilot@test.io --seed-password pw12345678" 2>&1)
KEY=$(echo "$SEED" | sed -n 's/.*stream key : \([a-z0-9]*\).*/\1/p')
[ -n "$KEY" ] || { echo "seeding failed:"; echo "$SEED"; exit 1; }

echo "==> starting db-writer, recorder, mediamtx"
docker run -d --name db-writer --network "$NET" -p 58002:8000 "${ENVV[@]}" "$DBW_IMAGE" >/dev/null \
  || { echo "db-writer failed to start"; exit 1; }
docker run -d --name recorder --network "$NET" \
  -e DB_WRITER_URL=http://db-writer:8000 -e RECORDING_STORE_SERVICE=local \
  -v "$VOL:/recordings" "$REC_IMAGE" >/dev/null \
  || { echo "recorder failed to start"; exit 1; }
docker run -d --name recup-mediamtx --network "$NET" -p 51935:1935 \
  -v "$REPO/configs/mediamtx/mediamtx.yaml:/mediamtx.yml:ro" \
  -v "$CERTS/server:/certs:ro" \
  -v "$VOL:/recordings" \
  bluenviron/mediamtx:latest-ffmpeg >/dev/null \
  || { echo "mediamtx failed to start (is a previous run still holding 51935?)"; exit 1; }
sleep 6

for c in db-writer recorder recup-mediamtx; do
  [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = "true" ] \
    || { echo "$c exited during startup:"; docker logs "$c" 2>&1 | tail -20; exit 1; }
done

jsonget() { python3 -c "import sys,json;print(json.load(sys.stdin).get('$1',''))"; }
sql() { docker exec recup-pg psql -U testuser -d testdb -tAc "$1" 2>/dev/null | tr -d ' '; }

echo "==> opening a flight — what the orchestrator does"
FLIGHT=$(curl -s -X POST "$API/flight/open" -H 'Content-Type: application/json' -d "{\"stream_key\":\"$KEY\"}")
UUID=$(echo "$FLIGHT" | jsonget public_uuid)
PUB=$(echo "$FLIGHT" | jsonget publisher_token)
FID=$(echo "$FLIGHT" | jsonget flight_id)
[ -n "$UUID" ] && [ -n "$PUB" ] || { echo "could not open flight: $FLIGHT"; exit 1; }
echo "    flight_id=$FID  public_uuid=$UUID"

echo
echo "── publishing to the annotated output, then disconnecting ─────────────────"
ffmpeg -hide_banner -loglevel error -re -f lavfi -i testsrc=size=320x240:rate=15 \
  -t 5 -c:v libx264 -preset ultrafast -tune zerolatency -g 15 \
  -f flv "rtmp://$RTMP/out/$UUID?token=$PUB" >/dev/null 2>&1
# The publisher disconnect above always flushes the current segment (see mediamtx.yaml)
# regardless of recordSegmentDuration, then MediaMTX's runOnRecordSegmentComplete hook
# fires and the recorder uploads it in a background task — give both a moment.
sleep 5

MTX_LOG=$(docker logs recup-mediamtx 2>&1)
REC_LOG=$(docker logs recorder 2>&1)

check "mediamtx fired the segment-complete hook" 1 \
      "$(echo "$MTX_LOG" | grep -c "runOnRecordSegmentComplete command launched" | awk '{print ($1>=1)?1:0}')"
check "recorder received the segment-complete hook" 1 \
      "$(echo "$REC_LOG" | grep -c "Segment complete" | awk '{print ($1>=1)?1:0}')"
check "recorder uploaded to the local backend" 1 \
      "$(echo "$REC_LOG" | grep -c "Uploading .* to 'local'" | awk '{print ($1>=1)?1:0}')"
check "recorder reported no error to db-writer" 0 \
      "$(echo "$REC_LOG" | grep -c "db-writer refused\|Could not report")"

echo
echo "── the segment landed in the recordings table against this flight ─────────"
check "exactly one recording row for this flight" 1 \
      "$(sql "SELECT COUNT(*) FROM recordings WHERE flight_id=$FID;")"
check "recording backend is 'local'" local \
      "$(sql "SELECT storage_backend FROM recordings WHERE flight_id=$FID LIMIT 1;")"
check "storage_location is empty for the local backend" "" \
      "$(sql "SELECT storage_location FROM recordings WHERE flight_id=$FID LIMIT 1;")"

echo
echo "── the file is really on the shared recordings volume ──────────────────────"
SEGMENT_FILE=$(docker run --rm -v "$VOL:/recordings" alpine sh -c "find /recordings/out/$UUID -type f 2>/dev/null | head -1")
check "a segment file exists under out/$UUID" 1 "$([ -n "$SEGMENT_FILE" ] && echo 1 || echo 0)"

echo
echo "=========================================================="
echo "$pass/$((pass+fail)) passed"
[ "$fail" -eq 0 ] || { echo; echo "--- recorder log ---"; echo "$REC_LOG" | tail -30; echo "--- mediamtx log ---"; echo "$MTX_LOG" | tail -30; exit 1; }

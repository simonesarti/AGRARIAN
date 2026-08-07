#!/usr/bin/env bash
# Drop the publisher under a running run_watch_live.sh, bring it back, and print the
# evidence needed to say what happened.
#
# Run it in a SECOND terminal while run_watch_live.sh is up and you are watching the
# page. It exists because doing this by hand is unreliable in two ways that both
# produce the same misleading result:
#
#   THE GAP IS WALL CLOCK. run_watch_live.sh sets RECONNECT_GRACE_S=30. Killing
#   ffmpeg, finding the stream key and pasting a command back easily takes longer
#   than that, at which point the flight legitimately ends and the page legitimately
#   says "nothing is flying". That is the page working, not failing — but it looks
#   identical to a broken reconnect. Here the gap is an argument and it is enforced.
#
#   FFMPEG'S EXIT CODE IS MEANINGLESS. §9 records this: it returns 0 whether
#   MediaMTX accepted the stream or refused it at authentication, because the FLV
#   muxer only reports that it could not rewrite a non-seekable header. So this
#   asserts on MediaMTX's own "is publishing to path" line instead, exactly as
#   run_mediamtx_auth.sh does, and it never discards ffmpeg's stderr.
#
# Usage:  ./probe_reconnect.sh [gap-seconds]     (default 10, i.e. inside grace)
#         ./probe_reconnect.sh 40                (outside grace: the flight should end)
#         ./probe_reconnect.sh stall             (freeze the publisher without
#                                                 dropping the connection)
set -uo pipefail

GAP="${1:-10}"
PG=watch-pg
MTX=watch-mediamtx
ORC=watch-orchestrator

for c in "$PG" "$MTX" "$ORC"; do
  docker inspect "$c" >/dev/null 2>&1 || {
    echo "'$c' is not running — start run_watch_live.sh first"; exit 1; }
done

KEY=$(docker exec "$PG" psql -U testuser -d testdb -tA \
        -c 'SELECT stream_key FROM streams ORDER BY stream_id LIMIT 1;' 2>/dev/null | tr -d '[:space:]')
[ -n "$KEY" ] || { echo "could not read the stream key from $PG"; exit 1; }
echo "==> stream key: $KEY"

flight_row() {
  docker exec "$PG" psql -U testuser -d testdb -tAF' ' \
    -c "SELECT flight_id, COALESCE(end_time::text, 'OPEN') FROM flights ORDER BY flight_id DESC LIMIT 1;" \
    2>/dev/null | tr -d '\r'
}

publish() {
  # stderr KEPT. A refused publish is the thing most worth seeing.
  ffmpeg -hide_banner -loglevel error -re -f lavfi -i testsrc=size=1920x1080:rate=30 \
    -t 3600 -c:v libx264 -preset ultrafast -tune zerolatency -g 30 \
    -f flv "rtmp://127.0.0.1:1935/in/$KEY" &
}

# Counted, and counted per PATH, because a flight has two publishers and only one of
# them is the drone. The app reads in/<key> and republishes the annotated stream to
# out/<uuid>, which is the path the browser actually watches. So dropping the drone
# takes the viewer's stream away INDIRECTLY: the app loses input, stops publishing,
# and MediaMTX drops out/ — and the browser cannot recover until the app has come
# back, whatever the page does. Attributing a slow recovery means knowing which of
# the two was slow, which is what the timestamps below are for.
MTX_BEFORE=$(docker logs "$MTX" 2>&1 | grep -c "is publishing to path")
echo "==> before: flight $(flight_row), $MTX_BEFORE publish(es) seen by MediaMTX"
echo "    (two per flight: the drone on in/<key>, the app on out/<uuid>)"

if [ "$GAP" = "stall" ]; then
  # The other failure mode: frames stop while the connection stays open. Only the
  # stall watchdog in watch.js can notice this — connectionState stays "connected".
  echo "==> freezing the publisher for 15s (socket stays open)"
  pkill -STOP -f "rtmp://127.0.0.1:1935/in/$KEY"
  sleep 15
  pkill -CONT -f "rtmp://127.0.0.1:1935/in/$KEY"
  echo "==> resumed. Expect: 'Video stalled — reconnecting…' then the picture back."
  sleep 8
  echo "==> after: flight $(flight_row)"
  exit 0
fi

echo "==> dropping the publisher for ${GAP}s"
pkill -f "rtmp://127.0.0.1:1935/in/$KEY"
sleep "$GAP"

echo "==> republishing at $(date +%T)"
publish

# Long enough for the app's own reconnect to complete. Its reader polls every
# VIDEO_STREAM_READER_RECONNECT_DELAY (5 s) and the pipeline has to spin back up
# before it republishes, so sampling at 8s would routinely miss it and blame the page.
sleep 25

MTX_AFTER=$(docker logs "$MTX" 2>&1 | grep -c "is publishing to path")
echo
echo "==> MediaMTX accepted the republish: $([ "$MTX_AFTER" -gt "$MTX_BEFORE" ] && echo YES || echo 'NO — the publish was refused, see ffmpeg output above')"
echo "==> flight now: $(flight_row)"
echo
echo "==> who published, and when — the gap between these two IS the recovery time,"
echo "    and it belongs to the app tier rather than to the browser:"
docker logs "$MTX" 2>&1 | grep "is publishing to path" | tail -4 \
  | sed -E "s/.*([0-9]{2}:[0-9]{2}:[0-9]{2}).*path '([^']+)'.*/    \1  \2/"
echo
echo "==> the app's own view of it (reader retries, then output republish):"
docker ps -q --filter "label=agrarian.flight_id" | head -1 | while read -r c; do
  [ -n "$c" ] && docker exec "$c" sh -c \
    "tail -n 400 /app/logs/*.log 2>/dev/null | grep -iE 'not available yet|connected|publish' | tail -8" \
    2>/dev/null || echo "    (flight container gone — it was torn down)"
done
echo
echo "==> orchestrator, last 12 lines:"
docker logs "$ORC" 2>&1 | tail -12
echo
if [ "$GAP" -lt 30 ] 2>/dev/null; then
  echo "Inside the 30s grace. Expect: flight still OPEN with the SAME id, orchestrator"
  echo "logging 'reconnected within grace', and the page recovering with no reload."
else
  echo "Outside the 30s grace. Expect: the old flight CLOSED and a NEW flight_id, and"
  echo "the page settling on 'Nothing is flying…' rather than retrying forever."
fi

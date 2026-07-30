#!/usr/bin/env bash
# End-to-end MediaMTX authorisation, against a real MediaMTX, db-writer and Postgres.
#
# test_mediamtx_auth.py checks the decision in isolation. This checks the thing that
# file cannot: that MediaMTX is actually consulting the endpoint and obeying it, for
# real publishes and real reads over RTMP and HLS.
#
# Two tenants exist throughout, so "denied" is proved against a *valid credential
# belonging to somebody else* rather than only against no credential at all.
#
# Host ports 11935/18888/18002 are used so this cannot collide with a running
# compose stack. Needs ffmpeg and curl on the host.
#
# Usage:  ./run_mediamtx_auth.sh
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NET=mtxauth-net
IMAGE=dbw-mtxauthtest
RTMP=localhost:11935
HLS=http://localhost:18888
API=http://localhost:18002

cleanup() {
  docker rm -f mtxa-mediamtx db-writer mtxa-pg >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  docker rmi "$IMAGE" >/dev/null 2>&1 || true
}
trap cleanup EXIT

pass=0; fail=0
check() {  # check <name> <expected> <actual>
  if [ "$2" = "$3" ]; then echo "PASS  $1   [$3]"; pass=$((pass+1))
  else echo "FAIL  $1   [expected $2, got $3]"; fail=$((fail+1)); fi
}
check_not() {  # check_not <name> <forbidden> <actual>
  if [ "$2" != "$3" ]; then echo "PASS  $1   [$3]"; pass=$((pass+1))
  else echo "FAIL  $1   [got the forbidden $3]"; fail=$((fail+1)); fi
}

for tool in ffmpeg curl python3; do
  command -v "$tool" >/dev/null || { echo "missing required tool: $tool"; exit 1; }
done

echo "==> building db-writer image"
docker build -q -t "$IMAGE" "$REPO/db_writer" >/dev/null || exit 1

docker network create "$NET" >/dev/null 2>&1 || true
docker run -d --name mtxa-pg --network "$NET" \
  -e POSTGRES_PASSWORD=testpw -e POSTGRES_USER=testuser -e POSTGRES_DB=testdb \
  postgres:16-alpine >/dev/null

echo "==> waiting for postgres"
for _ in $(seq 1 30); do
  docker exec mtxa-pg pg_isready -U testuser >/dev/null 2>&1 && break
  sleep 1
done

SECRET=$(openssl rand -hex 32)
ENVV=(-e DB_SERVICE=postgresql -e DB_HOST=mtxa-pg -e DB_PORT=5432 -e DB_NAME=testdb
      -e DB_WORKER_NAME=testuser -e DB_WORKER_PASSWORD=testpw
      -e SESSION_JWT_SECRET="$SECRET")

echo "==> seeding two tenants, one stream each"
SEED=$(docker run --rm --network "$NET" "${ENVV[@]}" "$IMAGE" \
  sh -c "echo yes | python rebuild_schema.py --drop --seed-user alice@test.io --seed-password pw123" 2>&1)
KEY_A=$(echo "$SEED" | sed -n 's/.*stream key : \([a-z0-9]*\).*/\1/p')

# Second tenant, so every denial can be tested against a real credential that simply
# belongs to the wrong user.
KEY_B=$(docker run --rm --network "$NET" "${ENVV[@]}" "$IMAGE" python -c "
from db_manager import UserDirectory, User
from sqlalchemy.orm import sessionmaker
import os
d = UserDirectory('postgresql://testuser:testpw@mtxa-pg:5432/testdb')
S = sessionmaker(bind=d._engine)
with S() as s:
    u = User(email='bob@test.io', password=User.hash_password('pw456'))
    s.add(u); s.commit()
    uid = u.user_id
print(d.create_stream(uid, 'bob stream')['stream_key'])
" 2>/dev/null | tail -1)

echo "    alice key=$KEY_A   bob key=$KEY_B"
[ -n "$KEY_A" ] && [ -n "$KEY_B" ] || { echo "seeding failed"; exit 1; }

echo "==> starting db-writer and mediamtx"
# Abort rather than continue if either fails to start. A MediaMTX that never came up
# — a port still held by a previous run, say — denies every request, which reads as a
# wall of convincing PASSes on the deny assertions and hides the real failure.
docker run -d --name db-writer --network "$NET" -p 18002:8000 "${ENVV[@]}" "$IMAGE" >/dev/null \
  || { echo "db-writer failed to start"; exit 1; }
docker run -d --name mtxa-mediamtx --network "$NET" \
  -p 11935:1935 -p 18888:8888 \
  -v "$REPO/configs/mediamtx/mediamtx.yaml:/mediamtx.yml:ro" \
  bluenviron/mediamtx:latest-ffmpeg >/dev/null \
  || { echo "mediamtx failed to start (is a previous run still holding 11935/18888?)"; exit 1; }
sleep 6

for c in db-writer mtxa-mediamtx; do
  [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = "true" ] \
    || { echo "$c exited during startup:"; docker logs "$c" 2>&1 | tail -20; exit 1; }
done

jsonget() { python3 -c "import sys,json;print(json.load(sys.stdin).get('$1',''))"; }

open_flight() {  # open_flight <stream_key>  -- what the orchestrator calls
  curl -s -X POST "$API/flight/open" -H 'Content-Type: application/json' \
    -d "{\"stream_key\":\"$1\"}"
}

viewer_token() {  # viewer_token <email> <password>  -- what the portal calls
  curl -s -X POST "$API/viewer/token" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$1\",\"password\":\"$2\"}"
}

viewer_token_call() {  # viewer_token_call <email> <password> [stream_id] -> "<body>\n<code>"
  if [ -n "${3:-}" ]; then
    curl -s -w $'\n%{http_code}' -X POST "$API/viewer/token" -H 'Content-Type: application/json' \
      -d "{\"email\":\"$1\",\"password\":\"$2\",\"stream_id\":$3}"
  else
    curl -s -w $'\n%{http_code}' -X POST "$API/viewer/token" -H 'Content-Type: application/json' \
      -d "{\"email\":\"$1\",\"password\":\"$2\"}"
  fi
}
vt_code() { echo "$1" | tail -1; }
vt_body() { echo "$1" | sed '$d'; }

A=$(open_flight "$KEY_A")
B=$(open_flight "$KEY_B")
UUID_A=$(echo "$A" | jsonget public_uuid); PUB_A=$(echo "$A" | jsonget publisher_token)
UUID_B=$(echo "$B" | jsonget public_uuid)
FID_A=$(echo "$A" | jsonget flight_id); SID_A=$(echo "$A" | jsonget stream_id)
FID_B=$(echo "$B" | jsonget flight_id); SID_B=$(echo "$B" | jsonget stream_id)
UID_A=$(echo "$A" | jsonget user_id)
[ -n "$UUID_A" ] && [ -n "$UUID_B" ] || { echo "could not open flights: $A / $B"; exit 1; }
echo "    alice flight uuid=$UUID_A   bob flight uuid=$UUID_B"

VA=$(viewer_token alice@test.io pw123)
VB=$(viewer_token bob@test.io pw456)
VIEW_A=$(echo "$VA" | jsonget viewer_token)
VIEW_B=$(echo "$VB" | jsonget viewer_token)
[ -n "$VIEW_A" ] && [ -n "$VIEW_B" ] || { echo "could not issue viewer tokens: $VA / $VB"; exit 1; }

# MediaMTX's HLS server answers the first request with a 302 to ?cookieCheck=1 and
# only authenticates the followed request, so -L and a cookie jar are required.
# Without them every result is 302 and the test measures nothing.
hls() {  # hls <uuid> [token] -> status code after redirects
  local jar; jar=$(mktemp)
  if [ -n "${2:-}" ]; then
    curl -sL -c "$jar" -b "$jar" -o /dev/null -w '%{http_code}' \
      -H "Authorization: Bearer $2" "$HLS/out/$1/index.m3u8"
  else
    curl -sL -c "$jar" -b "$jar" -o /dev/null -w '%{http_code}' "$HLS/out/$1/index.m3u8"
  fi
  rm -f "$jar"
}

# ffmpeg's exit code is NOT a signal here: it returns 0 whether MediaMTX accepted
# the stream or rejected it at authentication, because the FLV muxer only ever
# reports that it could not rewrite a non-seekable header. The authoritative signal
# is MediaMTX's own log line, so that is what is asserted on.
# Counted rather than offset by line number: the same path is published to more than
# once across these tests, and a line-offset baseline proved fragile.
publish() {  # publish <url> <expected-path> -> "published" | "rejected"
  local pattern="is publishing to path '$2'"
  local before after
  before=$(docker logs mtxa-mediamtx 2>&1 | grep -c "$pattern")
  ffmpeg -hide_banner -loglevel error -re -f lavfi -i testsrc=size=320x240:rate=15 \
    -t 3 -c:v libx264 -preset ultrafast -tune zerolatency -f flv "$1" >/dev/null 2>&1
  sleep 2
  after=$(docker logs mtxa-mediamtx 2>&1 | grep -c "$pattern")
  if [ "$after" -gt "$before" ]; then echo published; else echo rejected; fi
}

echo
echo "── read authorisation on the annotated output ─────────────────────────────"
# 401 vs 404 is the signal: 404 means MediaMTX accepted the credential and only then
# found nothing publishing, so authorisation ran and passed.
check     "no token cannot read the annotated stream"        401 "$(hls "$UUID_A")"
check_not "alice's viewer token is accepted on her flight"   401 "$(hls "$UUID_A" "$VIEW_A")"
check     "bob's viewer token REJECTED on alice's flight"    401 "$(hls "$UUID_A" "$VIEW_B")"
check     "publisher token REJECTED as a viewer (scope)"     401 "$(hls "$UUID_A" "$PUB_A")"
check     "garbage token rejected"                           401 "$(hls "$UUID_A" "not-a-jwt")"
check     "unknown output uuid rejected"                     401 "$(hls "00000000-0000-0000-0000-000000000000" "$VIEW_A")"

echo
echo "── publish authorisation on the ingest path ───────────────────────────────"
check "drone publishes on a live stream key" \
      published "$(publish "rtmp://$RTMP/in/$KEY_A" "in/$KEY_A")"
check "unknown stream key cannot publish" \
      rejected  "$(publish "rtmp://$RTMP/in/zzzzzzzzzzzzzzzz" "in/zzzzzzzzzzzzzzzz")"
check "legacy 'drone' path no longer accepts a publish" \
      rejected  "$(publish "rtmp://$RTMP/drone" "drone")"

echo
echo "── publish authorisation on the annotated output ──────────────────────────"
check "app publishes to its own output path with its token" \
      published "$(publish "rtmp://$RTMP/out/$UUID_A?token=$PUB_A" "out/$UUID_A")"
check "no token cannot publish to an output path" \
      rejected  "$(publish "rtmp://$RTMP/out/$UUID_A" "out/$UUID_A")"
check "alice's token cannot publish into bob's output" \
      rejected  "$(publish "rtmp://$RTMP/out/$UUID_B?token=$PUB_A" "out/$UUID_B")"

echo
echo "── an authorised viewer really receives the video ─────────────────────────"
ffmpeg -hide_banner -loglevel error -re -f lavfi -i testsrc=size=320x240:rate=15 \
  -t 20 -c:v libx264 -preset ultrafast -tune zerolatency -g 15 \
  -f flv "rtmp://$RTMP/out/$UUID_A?token=$PUB_A" >/dev/null 2>&1 &
FFPID=$!
sleep 8
check     "authorised viewer gets the HLS manifest while live"  200 "$(hls "$UUID_A" "$VIEW_A")"
check     "unauthorised viewer still refused while live"        401 "$(hls "$UUID_A" "$VIEW_B")"
check     "no-token viewer still refused while live"            401 "$(hls "$UUID_A")"
kill $FFPID >/dev/null 2>&1; wait $FFPID 2>/dev/null

echo
echo "── revocation takes effect on the next connection ─────────────────────────"
docker run --rm --network "$NET" "${ENVV[@]}" "$IMAGE" python -c "
from db_manager import UserDirectory
d = UserDirectory('postgresql://testuser:testpw@mtxa-pg:5432/testdb')
s = d.resolve_stream_key('$KEY_A')
d.revoke_stream(s['stream_id'], s['user_id'])
" >/dev/null 2>&1
check "a revoked key can no longer publish" \
      rejected "$(publish "rtmp://$RTMP/in/$KEY_A" "in/$KEY_A")"

echo
echo "── viewer/token: disambiguation once a user has two active flights ────────"
resp=$(viewer_token_call alice@test.io pw123)
check "one active flight: 200, no stream_id needed" 200 "$(vt_code "$resp")"
check "one active flight: resolves to it" "$FID_A" "$(echo "$(vt_body "$resp")" | jsonget flight_id)"

KEY_A2=$(docker run --rm --network "$NET" "${ENVV[@]}" "$IMAGE" python -c "
from db_manager import UserDirectory
d = UserDirectory('postgresql://testuser:testpw@mtxa-pg:5432/testdb')
print(d.create_stream($UID_A, 'alice second stream')['stream_key'])
" 2>/dev/null | tail -1)
A2=$(open_flight "$KEY_A2")
FID_A2=$(echo "$A2" | jsonget flight_id); SID_A2=$(echo "$A2" | jsonget stream_id)
[ -n "$FID_A2" ] || { echo "could not open alice's second flight: $A2"; exit 1; }

resp=$(viewer_token_call alice@test.io pw123)
check "two active flights, no stream_id: 409 (ambiguous, not a guess)" 409 "$(vt_code "$resp")"
resp=$(viewer_token_call alice@test.io pw123 "$SID_A")
check "two active flights, first stream_id: resolves to that flight" "$FID_A" \
      "$(echo "$(vt_body "$resp")" | jsonget flight_id)"
resp=$(viewer_token_call alice@test.io pw123 "$SID_A2")
check "two active flights, second stream_id: resolves to that flight" "$FID_A2" \
      "$(echo "$(vt_body "$resp")" | jsonget flight_id)"
resp=$(viewer_token_call alice@test.io pw123 "$SID_B")
check "alice cannot get a token for bob's stream_id" 404 "$(vt_code "$resp")"

echo
echo "=========================================================="
echo "$pass/$((pass+fail)) passed"
[ "$fail" -eq 0 ] || exit 1

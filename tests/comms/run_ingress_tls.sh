#!/usr/bin/env bash
# The drone-facing half of the ingress tier: MediaMTX terminating RTMPS and RTSPS,
# and Mosquitto terminating MQTTS, against a real db-writer and Postgres.
#
# WHY THIS RUNNER EXISTS
# ----------------------
# run_traefik_tls.sh closed the browser's four surfaces. The drone's three were
# still in the clear, which mattered more than the count suggests: a stream key is
# BOTH the ingest path and the credential (§3), it is typed into a controller
# before every flight, and it never expires. In plain RTMP it crossed the internet
# readable by anyone on the wire, and the same key is the drone's MQTT password.
#
# What this asserts is therefore two separate things, and the second is the one a
# TLS change is most likely to break:
#
#   1. The transport is really encrypted, really with our leaf, and really floored
#      at TLS 1.2 — asserted with a client that can actually speak TLS 1.1.
#   2. Authorisation is UNCHANGED by the move. MediaMTX's auth hook and
#      Mosquitto's ACL callback must reach db-writer and be obeyed exactly as they
#      are on the plaintext listeners, because "encrypted" and "authorised" are
#      independent properties and it is entirely possible to gain one and silently
#      lose the other.
#
# THE PLAINTEXT LISTENERS ARE ASSERTED TOO, and deliberately: §7 keeps them as a
# narrow fallback for drone firmware that cannot do TLS, so a change that broke
# them would ground exactly the drones the fallback exists for.
#
# Host ports 31936/38322/38883/38002 are used so this cannot collide with a
# running compose stack. The certificate is issued into a throwaway directory, not
# into certificates/ — the CA there may already be installed in a browser.
#
# Usage:  ./run_ingress_tls.sh
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NET=ingresstls-net
DOMAIN=agrarian.local
IMAGE=dbw-ingresstlstest
MOSQ_IMAGE=iegomez/mosquitto-go-auth:2.1.0-mosquitto_2.0.15
MTX_IMAGE=bluenviron/mediamtx:latest-ffmpeg
# The one client here that can still send a TLS 1.1 ClientHello. Modern curl and
# OpenSSL 3 refuse to send one at all, so a floor assertion written with those
# passes against a server that happily accepts 1.1 — it measures the client.
OLD_TLS_IMAGE=alpine:3.9
CERTS="$(mktemp -d)"
API=http://localhost:38002

cleanup() {
  docker rm -f it-mediamtx it-mosquitto db-writer it-pg >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  docker rmi "$IMAGE" >/dev/null 2>&1 || true
  rm -rf "$CERTS"
}
trap cleanup EXIT

pass=0; fail=0
check() {  # check <name> <expected> <actual>
  if [ "$2" = "$3" ]; then echo "PASS  $1   [$3]"; pass=$((pass+1))
  else echo "FAIL  $1   [expected $2, got $3]"; fail=$((fail+1)); fi
}

for tool in curl python3 openssl; do
  command -v "$tool" >/dev/null || { echo "missing required tool: $tool"; exit 1; }
done

echo "==> issuing a certificate from a throwaway local CA into $CERTS"
CERT_DIR="$CERTS" "$REPO/scripts/generate_local_certs.sh" "$DOMAIN" >/dev/null
echo "    leaf expires: $(openssl x509 -in "$CERTS/server/server.crt" -noout -enddate | cut -d= -f2)"

echo "==> building db-writer image"
docker build -q -t "$IMAGE" "$REPO/db_writer" >/dev/null || exit 1

docker network create "$NET" >/dev/null 2>&1 || true
docker run -d --name it-pg --network "$NET" \
  -e POSTGRES_PASSWORD=testpw -e POSTGRES_USER=testuser -e POSTGRES_DB=testdb \
  postgres:16-alpine >/dev/null

echo "==> waiting for postgres"
for _ in $(seq 1 30); do
  docker exec it-pg pg_isready -U testuser >/dev/null 2>&1 && break
  sleep 1
done

SECRET=$(openssl rand -hex 32)
ENVV=(-e DB_SERVICE=postgresql -e DB_HOST=it-pg -e DB_PORT=5432 -e DB_NAME=testdb
      -e DB_WORKER_NAME=testuser -e DB_WORKER_PASSWORD=testpw
      -e SESSION_JWT_SECRET="$SECRET")

echo "==> seeding two tenants, one stream each"
SEED=$(docker run --rm --network "$NET" "${ENVV[@]}" "$IMAGE" \
  sh -c "echo yes | python rebuild_schema.py --drop --seed-user alice@test.io --seed-password pw12345678" 2>&1)
KEY_A=$(echo "$SEED" | sed -n 's/.*stream key : \([a-z0-9]*\).*/\1/p')

# A second tenant, so every denial below is proved against a real credential that
# simply belongs to somebody else rather than only against no credential at all.
KEY_B=$(docker run --rm --network "$NET" "${ENVV[@]}" "$IMAGE" python -c "
from db_manager import UserDirectory
d = UserDirectory('postgresql://testuser:testpw@it-pg:5432/testdb')
uid = d.create_user('bob@test.io', 'pw12345678')['user_id']
print(d.create_stream(uid, 'bob stream')['stream_key'])
" 2>/dev/null | tail -1)

echo "    alice key=$KEY_A   bob key=$KEY_B"
[ -n "$KEY_A" ] && [ -n "$KEY_B" ] || { echo "seeding failed"; exit 1; }

# db-writer MUST answer to exactly that name: mediamtx.yaml hardcodes
# http://db-writer:8000/auth/mediamtx and mosquitto.conf hardcodes
# auth_opt_http_host db-writer. The auth hook only resolves if this container is
# reachable under it, which is the same convention the other runners follow.
echo "==> starting db-writer, mediamtx and mosquitto (the repo's real configs)"
docker run -d --name db-writer --network "$NET" -p 38002:8000 "${ENVV[@]}" "$IMAGE" >/dev/null \
  || { echo "db-writer failed to start"; exit 1; }

# The aliases are names the wildcard leaf actually covers, so certificate
# validation below is the validation a real client performs rather than a hostname
# check switched off — which is the difference between testing TLS and testing that
# a socket accepted bytes.
docker run -d --name it-mediamtx --network "$NET" --network-alias "media.$DOMAIN" \
  -p 31936:1936 -p 38322:8322 -p 31935:1935 \
  -v "$REPO/configs/mediamtx/mediamtx.yaml:/mediamtx.yml:ro" \
  -v "$CERTS/server:/certs:ro" \
  "$MTX_IMAGE" >/dev/null \
  || { echo "mediamtx failed to start (is a previous run still holding 31936/38322?)"; exit 1; }

docker run -d --name it-mosquitto --network "$NET" --network-alias "mqtt.$DOMAIN" \
  -p 38883:8883 -p 31883:1883 \
  -v "$REPO/configs/mosquitto/mosquitto.conf:/etc/mosquitto/mosquitto.conf:ro" \
  -v "$CERTS/server:/mosquitto/certs:ro" \
  "$MOSQ_IMAGE" >/dev/null \
  || { echo "mosquitto failed to start (is a previous run still holding 38883?)"; exit 1; }
sleep 6

for c in db-writer it-mediamtx it-mosquitto; do
  [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = "true" ] \
    || { echo "$c exited during startup:"; docker logs "$c" 2>&1 | tail -20; exit 1; }
done

# The listeners MediaMTX says it opened. A missing cert path or an unreadable key
# does not stop MediaMTX starting — it starts without the encrypted listener, and
# every assertion below would then fail with a connection refused that looks like
# a networking problem rather than a configuration one.
echo
echo "── the encrypted listeners actually opened ─────────────────────────────────"
MTXLOG=$(docker logs it-mediamtx 2>&1)
check "MediaMTX opened the RTMPS listener on 1936" 1 \
      "$(echo "$MTXLOG" | grep -c '\[RTMPS\] started with listener on :1936')"
check "MediaMTX opened the RTSPS listener on 8322" 1 \
      "$(echo "$MTXLOG" | grep -c '\[RTSPS\] started with listeners on :8322')"
check "and the plaintext pair is still there — §7 keeps it as the fallback" 2 \
      "$(echo "$MTXLOG" | grep -cE '\[RTMP\] started with listener on :1935|\[RTSP\] started with listeners on :8554')"

jsonget() { python3 -c "import sys,json;print(json.load(sys.stdin).get('$1',''))"; }

open_flight() {  # open_flight <stream_key>  -- what the orchestrator calls
  curl -s -X POST "$API/flight/open" -H 'Content-Type: application/json' \
    -d "{\"stream_key\":\"$1\"}"
}

A=$(open_flight "$KEY_A")
B=$(open_flight "$KEY_B")
UUID_A=$(echo "$A" | jsonget public_uuid); PUB_A=$(echo "$A" | jsonget publisher_token)
PUB_B=$(echo "$B" | jsonget publisher_token)
[ -n "$UUID_A" ] && [ -n "$PUB_A" ] && [ -n "$PUB_B" ] \
  || { echo "could not open flights: $A / $B"; exit 1; }

# ── The transport itself ─────────────────────────────────────────────────────
#
# One container for all of it, because it has to install an OpenSSL old enough to
# still send a TLS 1.1 ClientHello and doing that per assertion would be slow.
#
# What is read is whether the handshake COMPLETED, not which version came back.
# "New, TLSv1.2" is a trap: that line names the version the negotiated CIPHER was
# introduced in, not the connection's protocol, so a TLS 1.1 connection can
# print TLSv1.0 and read as a mismatch for the wrong reason. "New, (NONE)" means
# no handshake, which is exactly the question being asked of each -tlsN flag.
echo
echo "── the TLS floor, measured with a client that can speak TLS 1.1 ────────────"
FLOOR=$(docker run --rm --network "$NET" -v "$CERTS/server:/certs:ro" "$OLD_TLS_IMAGE" \
  sh -c "apk add -q openssl >/dev/null 2>&1

         hs() {  # hs <host> <port> <flag...> -> ok | refused | unreachable
           r=\$(echo Q | openssl s_client -connect \$1:\$2 -CAfile /certs/ca.crt \$3 \$4 \$5 2>&1)
           case \"\$r\" in
             *'New, (NONE)'*) echo refused ;;
             *'New, '*)       echo ok ;;
             *)               echo unreachable ;;
           esac
         }

         # The control. Everything below claims a server REFUSED an old protocol,
         # which is only a claim about the server if this client can still offer
         # one — and modern curl and OpenSSL 3 cannot, so the same assertion
         # written with those passes against a server that accepts TLS 1.1 and
         # measures nothing but the client.
         openssl s_server -accept 14433 -cert /certs/server.crt -key /certs/server.key \
           -min_protocol TLSv1.1 -max_protocol TLSv1.1 -quiet >/dev/null 2>&1 &
         sleep 1
         echo \"control tls1_1 \$(hs 127.0.0.1 14433 -tls1_1)\"

         for t in 'rtmps media.$DOMAIN 1936' 'rtsps media.$DOMAIN 8322' 'mqtts mqtt.$DOMAIN 8883'; do
           set -- \$t
           for v in tls1 tls1_1 tls1_2 tls1_3; do
             echo \"\$1 \$v \$(hs \$2 \$3 -\$v)\"
           done
           r=\$(echo Q | openssl s_client -connect \$2:\$3 -CAfile /certs/ca.crt 2>&1)
           echo \"\$1 verify \$(echo \"\$r\" | grep -oE 'Verify return code: [0-9]+' | head -1)\"
           # The same connection with the PUBLIC trust store instead of our CA. It
           # must fail: a certificate that validates against the public roots is
           # not the one this run issued, and an assertion that passed either way
           # would be asserting nothing at all.
           r=\$(echo Q | openssl s_client -connect \$2:\$3 2>&1)
           echo \"\$1 public \$(echo \"\$r\" | grep -oE 'Verify return code: [0-9]+' | head -1)\"
         done" 2>/dev/null)

floor() { echo "$FLOOR" | grep "^$1 $2 " | sed "s/^$1 $2 //"; }

check "the probe client can still complete a TLS 1.1 handshake at all" ok \
      "$(floor control tls1_1)"

# The public-trust probe passes when verification FAILED, so it is reported as the
# verify code being anything but 0 rather than compared to a particular error.
untrusted() {
  case "$(floor "$1" public)" in
    "Verify return code: 0") echo "it validated against the public roots" ;;
    "")                      echo "no verify result at all" ;;
    *)                       echo "refused" ;;
  esac
}

for svc in rtmps rtsps mqtts; do
  check "$svc refuses TLS 1.0"  refused "$(floor $svc tls1)"
  check "$svc refuses TLS 1.1"  refused "$(floor $svc tls1_1)"
  check "$svc accepts TLS 1.2"  ok      "$(floor $svc tls1_2)"
  check "$svc accepts TLS 1.3"  ok      "$(floor $svc tls1_3)"
  check "$svc serves the leaf this run issued" "Verify return code: 0" "$(floor $svc verify)"
  check "$svc is refused by a client without our CA" refused "$(untrusted $svc)"
done

# ── MediaMTX: authorisation over the encrypted listeners ─────────────────────
#
# ffmpeg's exit code is NOT a signal — it returns 0 whether MediaMTX accepted the
# stream or rejected it at authentication, because the FLV muxer only ever reports
# that it could not rewrite a non-seekable header. MediaMTX's own log line is the
# authoritative one, counted before and after rather than diffed by line number:
# the same path is published to more than once here.
ff() {  # ff <args...>  -- ffmpeg on the test network, holding our CA
  docker run --rm --network "$NET" -v "$CERTS/server:/certs:ro" \
    --entrypoint ffmpeg "$MTX_IMAGE" "$@" >/dev/null 2>&1
}

publish_rtmps() {  # publish_rtmps <url> <expected-path> [extra ffmpeg args]
  local url="$1" path="$2"; shift 2
  local pattern="is publishing to path '$path'"
  local before after
  before=$(docker logs it-mediamtx 2>&1 | grep -c "$pattern")
  ff -hide_banner -loglevel error -re -f lavfi -i testsrc=size=320x240:rate=15 \
     -t 3 -c:v libx264 -preset ultrafast -tune zerolatency \
     -f flv -tls_verify 1 -ca_file /certs/ca.crt "$@" "$url"
  sleep 2
  after=$(docker logs it-mediamtx 2>&1 | grep -c "$pattern")
  if [ "$after" -gt "$before" ]; then echo published; else echo rejected; fi
}

publish_rtsps() {  # publish_rtsps <url> <expected-path>
  local pattern="is publishing to path '$2'"
  local before after
  before=$(docker logs it-mediamtx 2>&1 | grep -c "$pattern")
  ff -hide_banner -loglevel error -re -f lavfi -i testsrc=size=320x240:rate=15 \
     -t 3 -c:v libx264 -preset ultrafast -tune zerolatency \
     -f rtsp -rtsp_transport tcp -ca_file /certs/ca.crt "$1"
  sleep 2
  after=$(docker logs it-mediamtx 2>&1 | grep -c "$pattern")
  if [ "$after" -gt "$before" ]; then echo published; else echo rejected; fi
}

MEDIA="media.$DOMAIN"

echo
echo "── RTMPS: the URL the portal actually prints ───────────────────────────────"
check "a drone publishes over RTMPS with a live stream key" published \
      "$(publish_rtmps "rtmps://$MEDIA:1936/in/$KEY_A" "in/$KEY_A")"
check "an unknown stream key is refused over RTMPS too" rejected \
      "$(publish_rtmps "rtmps://$MEDIA:1936/in/zzzzzzzzzzzzzzzz" "in/zzzzzzzzzzzzzzzz")"
check "the app publishes its annotated output over RTMPS with its token" published \
      "$(publish_rtmps "rtmps://$MEDIA:1936/out/$UUID_A?token=$PUB_A" "out/$UUID_A")"
check "no token still cannot publish to an output path over RTMPS" rejected \
      "$(publish_rtmps "rtmps://$MEDIA:1936/out/$UUID_A" "out/$UUID_A")"

# Encryption that a client can opt out of verifying is encryption against a
# passive eavesdropper only, which is not the threat here: whoever can read a
# stream key can also publish with it, so they can equally well be in the middle.
# Verified by pointing ffmpeg at the CA of a DIFFERENT run.
echo
echo "── the certificate is verified, not merely offered ─────────────────────────"
OTHER="$(mktemp -d)"
CERT_DIR="$OTHER" "$REPO/scripts/generate_local_certs.sh" "$DOMAIN" >/dev/null 2>&1

# Counted from MediaMTX's log exactly like every other publish here. Reading
# ffmpeg's exit code instead would assert nothing: it is 0 whether the stream was
# accepted, refused at authentication, or never connected at all.
PATTERN="is publishing to path 'in/$KEY_A'"
before=$(docker logs it-mediamtx 2>&1 | grep -c "$PATTERN")
docker run --rm --network "$NET" -v "$OTHER/server:/certs:ro" \
  --entrypoint ffmpeg "$MTX_IMAGE" \
  -hide_banner -loglevel error -re -f lavfi -i testsrc=size=320x240:rate=15 \
  -t 3 -c:v libx264 -preset ultrafast -f flv -tls_verify 1 -ca_file /certs/ca.crt \
  "rtmps://$MEDIA:1936/in/$KEY_A" >/dev/null 2>&1
sleep 2
after=$(docker logs it-mediamtx 2>&1 | grep -c "$PATTERN")
check "a publisher holding somebody else's CA cannot connect" 0 "$((after - before))"

# ...and the same key over the same listener DOES publish when the CA is ours, so
# the assertion above is a statement about the certificate rather than about
# anything else that might have gone wrong in that container.
check "while the same publish with our CA succeeds" published \
      "$(publish_rtmps "rtmps://$MEDIA:1936/in/$KEY_A" "in/$KEY_A")"
rm -rf "$OTHER"

echo
echo "── RTSPS ───────────────────────────────────────────────────────────────────"
check "a drone publishes over RTSPS with a live stream key" published \
      "$(publish_rtsps "rtsps://$MEDIA:8322/in/$KEY_A" "in/$KEY_A")"
check "an unknown stream key is refused over RTSPS too" rejected \
      "$(publish_rtsps "rtsps://$MEDIA:8322/in/zzzzzzzzzzzzzzzz" "in/zzzzzzzzzzzzzzzz")"

echo
echo "── the plaintext fallback still works ──────────────────────────────────────"
# encryption: optional, not strict. A drone whose firmware cannot do TLS is
# otherwise a drone that cannot fly, so breaking this would ground exactly the
# aircraft the fallback exists for.
check "plain RTMP still publishes on a live key" published \
      "$(publish_rtmps "rtmp://$MEDIA:1935/in/$KEY_A" "in/$KEY_A")"

echo
echo "── revocation still bites on the encrypted listener ────────────────────────"
# The property stream keys are built on (§3): no expiry, instant revocation. A
# cached authorisation decision would break it, and so would a TLS listener that
# somehow authorised differently from the plaintext one.
docker run --rm --network "$NET" "${ENVV[@]}" "$IMAGE" python -c "
from db_manager import UserDirectory
d = UserDirectory('postgresql://testuser:testpw@it-pg:5432/testdb')
s = d.resolve_stream_key('$KEY_A')
d.revoke_stream(s['stream_id'], s['user_id'])
" >/dev/null 2>&1
check "a revoked key cannot publish over RTMPS" rejected \
      "$(publish_rtmps "rtmps://$MEDIA:1936/in/$KEY_A" "in/$KEY_A")"
check "nor over RTSPS" rejected \
      "$(publish_rtsps "rtsps://$MEDIA:8322/in/$KEY_A" "in/$KEY_A")"

# ── Mosquitto: the same credential, the other plane ──────────────────────────
#
# The drone's MQTT password IS its stream key, so plain 1883 leaked the same
# secret the video plane leaked. KEY_A is revoked by now, so bob's key carries
# the rest of this section.
MQTT="mqtt.$DOMAIN"

mqtts() {  # mqtts <sub|pub> <user> <topic> [extra args...]
  local cmd="$1" user="$2" topic="$3"; shift 3
  docker run --rm --network "$NET" -v "$CERTS/server:/certs:ro" eclipse-mosquitto:2 \
    "mosquitto_$cmd" -h "$MQTT" -p 8883 --cafile /certs/ca.crt \
    -u "$user" -P x -t "$topic" "$@" 2>&1
}

denied_count() { docker logs it-mosquitto 2>&1 | grep -c 'error code: 401'; }

echo
echo "── MQTTS: CONNECT ──────────────────────────────────────────────────────────"
mqtts pub "$KEY_B" "telemetry/$KEY_B/latitude" -m 1 >/dev/null; rc=$?
check "a drone connects over MQTTS with a live stream key" 0 "$rc"
mqtts pub zzzzzzzzzzzzzzzz "telemetry/zzzzzzzzzzzzzzzz/latitude" -m 1 >/dev/null; rc=$?
check "an unknown stream key is refused at CONNECT over MQTTS" 5 "$rc"
mqtts pub "$KEY_A" "telemetry/$KEY_A/latitude" -m 1 >/dev/null; rc=$?
check "the key revoked above is refused at CONNECT over MQTTS" 5 "$rc"

# TLS with verification switched off would let anyone in the path read the key
# out of the CONNECT packet, which is the whole thing this listener protects.
out=$(docker run --rm --network "$NET" eclipse-mosquitto:2 \
      mosquitto_pub -h "$MQTT" -p 8883 --cafile /etc/ssl/certs/ca-certificates.crt \
      -u "$KEY_B" -P x -t "telemetry/$KEY_B/latitude" -m 1 2>&1; echo "rc=$?")
check "a client without our CA cannot connect over MQTTS" 1 \
      "$(echo "$out" | grep -cE 'rc=[^0]')"

echo
echo "── MQTTS: the ACL is unchanged by the transport ────────────────────────────"
# PUBLISH at QoS 0 gives no client-side signal for a denial — the broker just
# drops it — so the broker's own log line is the authority, the same trap
# run_mqtt_auth.sh documents.
before=$(denied_count)
mqtts pub "$KEY_B" "telemetry/$KEY_B/latitude" -m 44.0 >/dev/null
check "a drone publishing under its own key over MQTTS is not denied" "$before" "$(denied_count)"

before=$(denied_count)
mqtts pub "$KEY_B" "telemetry/somebodyelsekey/latitude" -m 44.0 >/dev/null
after=$(denied_count)
check "it still cannot publish under another key over MQTTS" 1 "$((after - before))"

out=$(mqtts sub "$PUB_B" "telemetry/$KEY_B/latitude" -C 1 -W 2 -d)
check "the app subscribes to its own flight's telemetry over MQTTS" 0 \
      "$(echo "$out" | grep -c 'All subscription requests were denied')"

# The other half of that pair, and the reason it is a pair: a subscribe that is
# never denied for ANY topic reads identically to one that is correctly allowed.
# A live slot for the other tenant, because KEY_A is revoked by now and "denied
# because revoked" would not be the same claim as "denied because it is not yours".
KEY_A2=$(docker run --rm --network "$NET" "${ENVV[@]}" "$IMAGE" python -c "
from db_manager import UserDirectory
d = UserDirectory('postgresql://testuser:testpw@it-pg:5432/testdb')
print(d.create_stream(d.authenticate('alice@test.io', 'pw12345678'), 'alice second')['stream_key'])
" 2>/dev/null | tail -1)
[ -n "$KEY_A2" ] || { echo "could not mint a second live stream for alice"; exit 1; }
out=$(mqtts sub "$PUB_B" "telemetry/$KEY_A2/latitude" -C 1 -W 2 -d)
check "but not to another tenant's, over MQTTS either" 1 \
      "$(echo "$out" | grep -c 'All subscription requests were denied')"

echo
echo "── the plaintext MQTT fallback still works ─────────────────────────────────"
docker run --rm --network "$NET" eclipse-mosquitto:2 \
  mosquitto_pub -h "$MQTT" -p 1883 -u "$KEY_B" -P x \
  -t "telemetry/$KEY_B/latitude" -m 44.0 >/dev/null 2>&1
check "plain MQTT on 1883 still connects and publishes" 0 "$?"

echo
echo "=========================================================="
echo "$pass/$((pass+fail)) passed"
if [ "$fail" -ne 0 ]; then
  echo; echo "--- mediamtx log (tail) ---"; docker logs it-mediamtx 2>&1 | tail -25
  echo; echo "--- mosquitto log (tail) ---"; docker logs it-mosquitto 2>&1 | tail -20
  exit 1
fi

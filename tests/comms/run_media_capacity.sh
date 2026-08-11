#!/usr/bin/env bash
# How many flights does ONE MediaMTX carry before it starts dropping?
#
# WHY THIS EXISTS
# ---------------
# CLOUD_ARCHITECTURE.md §10.8 calls this "the first thing to do here", and it is the
# prerequisite that turns §10 from a sketch into a configuration. Every number in
# §10.5 and §10.6 depends on it:
#
#   - §10.5 sets scale-up at "headroom = peak arrival rate x provisioning time",
#     which is a fraction OF A CELL'S CAPACITY and meaningless without one.
#   - §10.6 says cell capacity is `total flows / 5`. That is arithmetic once you
#     know total flows, and nothing more than a shape until you do.
#   - §9 says MediaMTX sharding is "not urgent" because "the GPU tier saturates
#     first, by orders of magnitude". That is a comparison between one measured
#     number (a GPU runs one flight) and one unmeasured one.
#
# THE MODEL, straight from §10.6. One flight is five flows:
#
#     drone  -> MediaMTX   publish in/<n>      1 flow in
#     MediaMTX -> app      read    in/<n>      1 flow out
#     app    -> MediaMTX   publish out/<n>     1 flow in
#     MediaMTX -> viewers  read    out/<n> x2  2 flows out
#
# Simulated with ffmpeg: a publisher looping a pre-encoded asset, a relay standing in
# for the GPU app, and two readers. Every one of them uses `-c copy`, so no encoder
# runs during the measurement — otherwise this measures the host's CPU rather than
# MediaMTX's forwarding, which is the whole point.
#
# WHAT THIS MEASURES, AND WHAT IT DOES NOT
# ----------------------------------------
# It measures MediaMTX FORWARDING over RTMP, unencrypted, on one host.
#
# It is therefore an UPPER BOUND on the real thing, and the gap is not small. §10.6
# is explicit that "WebRTC is per-peer DTLS-SRTP rather than a multicast fan-out, so
# every viewer genuinely costs its own encryption". Real viewers arrive over WebRTC
# and cost more than the RTMP readers here. Real drones arrive over RTMPS and cost
# more than the plaintext publisher here. So: a number this harness reports as
# comfortable may not be, and a number it reports as saturated definitely is.
#
# Both ends of the path are on one machine, so the network between them is a loopback
# bridge rather than the internet. That flatters throughput and removes jitter, packet
# loss and reordering — the conditions under which a reader actually falls behind.
#
# HOW DEGRADATION IS DETECTED
# ---------------------------
# Not by watching for a crash: MediaMTX under strain drops frames to readers long
# before it fails, and a harness looking for an error would report "fine" right up to
# the point it reports nothing. Instead, per path per sample window:
#
#     rx_rate = d(bytesReceived)/dt      what the publisher is putting in
#     tx_rate = d(bytesSent)/dt          what all readers are getting out
#     ratio   = tx_rate / (readers x rx_rate)
#
# A healthy path has ratio ~= 1.0: every reader receives everything the publisher
# sent. Below DEGRADE_RATIO some reader is being starved, which is what "frames start
# dropping" means from the server's side.
#
# Usage:  ./run_media_capacity.sh [max_flights]
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="${CAPACITY_WORKDIR:-/tmp/agrarian-capacity}"
NET=agrarian-capnet
MTX=agrarian-capmtx
IMAGE=bluenviron/mediamtx:1.19.3-ffmpeg

MAX_FLIGHTS="${1:-32}"
STEPS="${CAPACITY_STEPS:-1 2 4 8 12 16 24 32 48 64}"
SAMPLE_S="${CAPACITY_SAMPLE_S:-10}"     # window over which rates are measured
SETTLE_S="${CAPACITY_SETTLE_S:-8}"      # let new flows reach steady state first
DEGRADE_RATIO="${CAPACITY_DEGRADE_RATIO:-0.95}"
BITRATE_K=4000

cleanup() {
  echo
  echo "cleaning up..."
  docker ps -aq --filter "name=agrarian-cap" | xargs -r docker rm -f >/dev/null 2>&1
  docker network rm "$NET" >/dev/null 2>&1
  echo "done"
}
trap cleanup EXIT INT TERM

mtx_api() { docker exec "$MTX" wget -qO- "http://localhost:9997/v3/$1" 2>/dev/null; }

echo "=============================================================="
echo " One MediaMTX: how many 5-flow flights before it drops?"
echo " model: publish in/N | relay in/N->out/N | 2 readers of out/N"
echo " asset: 1920x1080 @30fps, ${BITRATE_K}kbps, stream-copied (no encoding)"
echo "=============================================================="

mkdir -p "$WORK"
docker network create "$NET" >/dev/null 2>&1

# ── The asset, built once and reused ──────────────────────────────────────────
# Encoding happens HERE and never during a measurement. -stream_loop -1 -c copy
# then costs a publisher almost nothing, so the ffmpeg processes are not competing
# with MediaMTX for CPU.
if [[ ! -f "$WORK/src.mp4" ]]; then
  echo "building test asset (once)..."
  docker run --rm -v "$WORK:/out" --entrypoint ffmpeg "$IMAGE" \
    -f lavfi -i "testsrc2=size=1920x1080:rate=30" -t 20 \
    -c:v libx264 -preset veryfast -b:v ${BITRATE_K}k -pix_fmt yuv420p -g 60 \
    -y /out/src.mp4 >/dev/null 2>&1 || { echo "asset build failed"; exit 1; }
fi
echo "asset: $(du -h "$WORK/src.mp4" | cut -f1)"

docker rm -f "$MTX" >/dev/null 2>&1
docker run -d --name "$MTX" --network "$NET" \
  -e MTX_API=yes -e MTX_LOGLEVEL=warn -e MTX_SRT=no -e MTX_MOQ=no \
  "$IMAGE" >/dev/null || { echo "mediamtx failed to start"; exit 1; }
sleep 3

# Wait until a path exists and is ready, rather than sleeping and hoping. Each stage
# of a flight depends on the previous one being live: the relay cannot read in/<n>
# until the publisher is up, and a viewer cannot read out/<n> until the relay is
# publishing. A blind sleep here samples a half-built flight and reports it as a
# starved publisher — which is what the first version of this script did.
wait_path_ready() {
  local name="$1" tries=0
  while [[ $tries -lt 40 ]]; do
    if mtx_api paths/list | python3 -c "
import json,sys
want='$name'
for p in json.load(sys.stdin)['items']:
    if p['name']==want and p['ready']:
        sys.exit(0)
sys.exit(1)" 2>/dev/null; then
      return 0
    fi
    sleep 0.5; tries=$((tries + 1))
  done
  echo "  WARNING: $name never became ready" >&2
  return 1
}

start_flight() {
  local n="$1"
  docker run -d --name "agrarian-cap-pub-$n" --network "$NET" -v "$WORK:/a:ro" \
    --entrypoint ffmpeg "$IMAGE" \
    -re -stream_loop -1 -i /a/src.mp4 -c copy -f flv "rtmp://$MTX/in/$n" >/dev/null 2>&1
  wait_path_ready "in/$n" || return 1

  # The relay stands in for the GPU app: it reads the raw feed and republishes an
  # "annotated" one. Stream copy, because the app's GPU work is not what is being
  # measured and putting an encoder here would measure the host instead.
  docker run -d --name "agrarian-cap-rly-$n" --network "$NET" \
    --entrypoint ffmpeg "$IMAGE" \
    -i "rtmp://$MTX/in/$n" -c copy -f flv "rtmp://$MTX/out/$n" >/dev/null 2>&1
  wait_path_ready "out/$n" || return 1

  # Two viewers, the cap §10.6 sets. Both flags here were learned the hard way and
  # both fail as an under-loaded server rather than as an error:
  #
  #   -f flv   the mp4 muxer needs a seekable output and /dev/null is not one, so it
  #            exits with "I/O error" and the reader never attaches at all.
  #   -y       without it ffmpeg asks "File '/dev/null' already exists. Overwrite?",
  #            gets no answer, and quits a few seconds AFTER attaching. That is the
  #            worse of the two: the reader shows up in the API, a readiness gate
  #            passes, and it is gone by the time rates are sampled — so the ramp
  #            silently measures one reader per flight instead of three.
  for v in 1 2; do
    docker run -d --name "agrarian-cap-view-$n-$v" --network "$NET" \
      --entrypoint ffmpeg "$IMAGE" \
      -i "rtmp://$MTX/out/$n" -c copy -f flv -y /dev/null >/dev/null 2>&1
  done
}

# Every flight must present 2 paths and 3 readers before a sample means anything.
# Sampling an incompletely attached ramp understates load and overstates capacity,
# which is the direction that matters: it would report headroom that is not there.
wait_all_attached() {
  local want_paths=$(( $1 * 2 )) want_readers=$(( $1 * 3 )) tries=0
  while [[ $tries -lt 60 ]]; do
    read -r np nr <<<"$(mtx_api paths/list | python3 -c "
import json,sys
d=json.load(sys.stdin)['items']
print(len(d), sum(len(p['readers']) for p in d))" 2>/dev/null)"
    [[ "${np:-0}" -ge "$want_paths" && "${nr:-0}" -ge "$want_readers" ]] && return 0
    sleep 1; tries=$((tries + 1))
  done
  echo "  WARNING: only $np/$want_paths paths and $nr/$want_readers readers attached" >&2
  return 1
}

# ── Sample: two API reads SAMPLE_S apart, rates computed between them ─────────
sample() {
  local before after
  before="$(mtx_api paths/list)"
  sleep "$SAMPLE_S"
  after="$(mtx_api paths/list)"
  python3 - "$SAMPLE_S" "$DEGRADE_RATIO" <<PYEOF
import json, sys
sample_s = float(sys.argv[1]); degrade = float(sys.argv[2])
before = json.loads('''$before''')["items"]
after  = json.loads('''$after''')["items"]
b = {p["name"]: p for p in before}
worst, worst_path, total_tx, total_rx, flows = 1.0, "-", 0.0, 0.0, 0
for p in after:
    n = p["name"]
    if n not in b:
        continue
    rx = (p["bytesReceived"] - b[n]["bytesReceived"]) * 8 / sample_s / 1e6
    tx = (p["bytesSent"]     - b[n]["bytesSent"])     * 8 / sample_s / 1e6
    readers = len(p["readers"])
    total_rx += rx; total_tx += tx
    flows += 1 + readers
    if readers and rx > 0.1:
        ratio = tx / (readers * rx)
        if ratio < worst:
            worst, worst_path = ratio, n
print(f"{worst:.4f} {worst_path} {total_rx:.1f} {total_tx:.1f} {flows}")
PYEOF
}

printf "\n%-8s %-7s %-9s %-9s %-8s %-8s %-9s %s\n" \
  "flights" "flows" "in Mbps" "out Mbps" "cpu%" "mem" "worst" "verdict"
printf -- "---------------------------------------------------------------------------------\n"

started=0
saturated=""
host_bound=""
last_clean=0
for target in $STEPS; do
  [[ "$target" -gt "$MAX_FLIGHTS" ]] && break
  while [[ $started -lt $target ]]; do
    started=$((started + 1))
    start_flight "$started"
  done
  wait_all_attached "$started"
  sleep "$SETTLE_S"

  read -r ratio path rx tx flows <<<"$(sample)"
  stats="$(docker stats --no-stream --format "{{.CPUPerc}}|{{.MemUsage}}" "$MTX" 2>/dev/null)"
  cpu="${stats%%|*}"; mem="${stats##*|}"; mem="${mem%% /*}"

  # A publisher that cannot keep up means the HOST ran out, not MediaMTX — a
  # different finding and one that must not be reported as cell capacity.
  expected_rx=$(python3 -c "print(f'{$started * $BITRATE_K / 1000 * 2:.1f}')")
  # Two different endings, and conflating them is the easy mistake: one is the
  # answer this harness exists to find, the other is the harness itself running out.
  verdict="ok"
  if python3 -c "import sys; sys.exit(0 if $ratio < $DEGRADE_RATIO else 1)"; then
    verdict="DEGRADED on $path"
    [[ -z "$saturated" ]] && saturated="$started"
  elif python3 -c "import sys; sys.exit(0 if $rx < $expected_rx * 0.9 else 1)"; then
    verdict="load generator starved — HOST limit, not MediaMTX"
    [[ -z "$host_bound" ]] && host_bound="$started"
  fi

  printf "%-8s %-7s %-9s %-9s %-8s %-8s %-9s %s\n" \
    "$started" "$flows" "$rx" "$tx" "$cpu" "$mem" "$ratio" "$verdict"

  [[ "$verdict" == "ok" ]] && last_clean="$started"
  [[ -n "$saturated" || -n "$host_bound" ]] && break
done

echo
warn_count="$(docker logs "$MTX" 2>&1 | grep -ciE "wrote|too slow|dropp|queue|overflow" || true)"
echo "MediaMTX warnings in log: $warn_count"
docker logs "$MTX" 2>&1 | grep -iE "too slow|dropp|queue|overflow" | tail -5 | sed 's/^/  /'

echo
if [[ -n "$saturated" ]]; then
  echo "RESULT: MediaMTX degraded at $saturated concurrent flights ($((saturated * 5)) flows)."
  echo "        Readers stopped receiving everything the publisher sent. This is the"
  echo "        number this harness exists to find."
elif [[ -n "$host_bound" ]]; then
  echo "RESULT: NOT FOUND — the load generator gave out first, at $host_bound flights."
  echo "        MediaMTX was still clean at $last_clean flights ($((last_clean * 5)) flows):"
  echo "        every reader received everything, and the server logged nothing."
  echo "        $last_clean is therefore a FLOOR on cell capacity, not the capacity."
  echo "        Raising it needs load generated from more than one machine."
else
  echo "RESULT: no degradation up to $started concurrent flights ($((started * 5)) flows)."
  echo "        A FLOOR, not the capacity — the ramp ran out before MediaMTX did."
fi
echo
echo "Read this as an upper bound: RTMP readers, no TLS, loopback network."
echo "Real viewers arrive over WebRTC with per-peer DTLS-SRTP and cost more (§10.6)."

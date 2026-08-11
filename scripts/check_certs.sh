#!/usr/bin/env bash
# Is the certificate this deployment is ACTUALLY SERVING still good?
#
# WHY THIS EXISTS
# ---------------
# scripts/renew_certs.sh checks the serial being served on a fresh connection, which
# is the right check — but it only runs when renewal runs. Between renewals nothing
# looks, and CLOUD_ARCHITECTURE.md §9 carried that as an open item: the first report
# of an expired or mismatched leaf would be a browser warning, or a drone that will
# not connect while somebody is standing in a field with it.
#
# IT ASKS THE LISTENER, NOT THE FILE. A correct certificate on disk and a stale one
# in a running process are indistinguishable from the filesystem, and that gap is not
# hypothetical here: §7 measured that Traefik does not reload a replaced leaf on its
# own, and Mosquitto does not either without SIGHUP. Reading certificates/server/
# would therefore report health that no client can observe. Every check below opens a
# real TLS connection.
#
# WHAT IT CHECKS, per terminator:
#   - a handshake completes at all
#   - the chain validates against the SYSTEM trust store, with no --cacert, which is
#     what a browser or a drone's TLS stack will do
#   - the served name matches, via -verify_hostname rather than by eye
#   - expiry is further out than WARN_DAYS
#
# EXIT CODES, so cron mail and a monitoring system can tell these apart:
#   0  all good
#   1  a certificate expires within WARN_DAYS, or a terminator did not answer
#   2  a certificate is invalid, untrusted, or for the wrong name  (act now)
#
# A terminator that is not running is reported and counted as a failure rather than
# skipped: "nothing answered on 8883" is exactly the state this is meant to notice,
# and treating it as absence of news would defeat the point.
#
# USAGE
#   ./scripts/check_certs.sh                 # uses RENEW_DOMAIN or the default
#   ./scripts/check_certs.sh --host <name>   # check a different name
#   WARN_DAYS=30 ./scripts/check_certs.sh
#
# From cron, daily. It is cheap, and the failure it looks for arrives on a schedule
# nobody watches.
set -uo pipefail

HOST="${RENEW_DOMAIN:-agrarianlivestock.com}"
# The stack is reachable on loopback; the NAME is what must match the certificate,
# so SNI and hostname verification use it while the connection goes to 127.0.0.1.
CONNECT="${CHECK_CONNECT_HOST:-127.0.0.1}"
WARN_DAYS="${WARN_DAYS:-21}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --connect) CONNECT="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# port:label — the three terminators §7 names. 8322 is MediaMTX's other listener and
# shares the same leaf, so it is checked too: it is a separate listener and could
# fail on its own.
TERMINATORS=(
  "1936:mediamtx-rtmps"
  "8322:mediamtx-rtsps"
  "8883:mosquitto-mqtts"
  "443:traefik-https"
)

worst=0
now_epoch="$(date +%s)"

for spec in "${TERMINATORS[@]}"; do
  port="${spec%%:*}"; label="${spec#*:}"

  # One connection, reused for every question below. -verify_return_error makes a
  # verification failure a nonzero exit rather than a line of text further down that
  # is easy to skim past.
  chain="$(echo | timeout 10 openssl s_client \
             -connect "$CONNECT:$port" \
             -servername "$HOST" \
             -verify_hostname "$HOST" \
             -verify_return_error \
             -CApath /etc/ssl/certs 2>/dev/null)"
  rc=$?

  if [[ $rc -ne 0 || -z "$chain" ]]; then
    # Distinguish "nothing there" from "there, but refused" — different problems.
    if timeout 5 bash -c "cat < /dev/null > /dev/tcp/$CONNECT/$port" 2>/dev/null; then
      printf '  %-18s INVALID   handshake or verification failed (untrusted, or wrong name)\n' "$label"
      worst=2
    else
      printf '  %-18s NO ANSWER nothing listening on %s:%s\n' "$label" "$CONNECT" "$port"
      [[ $worst -lt 1 ]] && worst=1
    fi
    continue
  fi

  not_after="$(echo "$chain" | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)"
  if [[ -z "$not_after" ]]; then
    printf '  %-18s INVALID   served something that is not a parseable certificate\n' "$label"
    worst=2
    continue
  fi

  end_epoch="$(date -d "$not_after" +%s 2>/dev/null)"
  days=$(( (end_epoch - now_epoch) / 86400 ))
  serial="$(echo "$chain" | openssl x509 -noout -serial 2>/dev/null | cut -d= -f2)"

  if [[ $days -lt 0 ]]; then
    printf '  %-18s EXPIRED   %s days ago  (%s)\n' "$label" "$((-days))" "$serial"
    worst=2
  elif [[ $days -lt $WARN_DAYS ]]; then
    printf '  %-18s DUE       %s days left  (%s)\n' "$label" "$days" "$serial"
    [[ $worst -lt 1 ]] && worst=1
  else
    printf '  %-18s OK        %s days left  (%s)\n' "$label" "$days" "$serial"
  fi
done

# All four serve the same leaf (§7 — one certificate, three terminators). If they
# disagree, a reload was missed somewhere, which is the specific failure the renewal
# hook exists to prevent and therefore the specific thing worth noticing.
serials="$(for spec in "${TERMINATORS[@]}"; do
             port="${spec%%:*}"
             echo | timeout 10 openssl s_client -connect "$CONNECT:$port" \
               -servername "$HOST" 2>/dev/null \
             | openssl x509 -noout -serial 2>/dev/null
           done | sort -u | grep -c . )"
if [[ "$serials" -gt 1 ]]; then
  echo "  MISMATCH          terminators are serving $serials different certificates —"
  echo "                    a reload was missed; see scripts/renew_certs.sh"
  worst=2
fi

case $worst in
  0) echo "  all good (warn threshold ${WARN_DAYS}d)" ;;
  1) echo "  ATTENTION: renewal due or a terminator is down" ;;
  2) echo "  URGENT: a served certificate is expired, untrusted, or for the wrong name" ;;
esac
exit $worst

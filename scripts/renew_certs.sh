#!/usr/bin/env bash
# Renew the public certificate and make all three terminators pick it up.
#
# WHY THIS EXISTS
# ---------------
# The leaf in certificates/server/ is a real Let's Encrypt wildcard obtained over
# DNS-01 against Cloudflare (CLOUD_ARCHITECTURE.md §7). Let's Encrypt issues for 90
# days, not the 397 the local generator used, so renewal stopped being theoretical
# the day the real certificate landed. A certificate on disk is only half an answer:
# a service that does not reread it turns renewal into a restart, and on MediaMTX a
# restart drops every flight in the air.
#
# THE THREE TERMINATORS DO THREE DIFFERENT THINGS, and two of the three were once
# assumed wrong. Measured in tests/comms/run_cert_renewal.sh:
#
#   MediaMTX   rereads the file itself, per handshake, within seconds. Needs
#              NOTHING. A flight already in the air is undisturbed — it keeps the
#              certificate it negotiated while new connections get the new one.
#              DO NOT SIGHUP IT. That kills the process, and with it every flight
#              in the air. The test asserts this precisely so nobody reaches for
#              the obvious symmetry with Mosquitto.
#
#   Mosquitto  needs SIGHUP. Behaves as documented, the only one that does.
#
#   Traefik    does NOT notice, despite `watch: true`. What is watched is
#              providers.file.directory — configs/traefik/dynamic — and the
#              certificate is deliberately mounted outside it, so replacing the leaf
#              fires no event and Traefik serves the old one indefinitely. Touching
#              any file in that directory reloads the configuration and rereads the
#              certificate with it: no restart, no dropped connections.
#
# ON KUBERNETES NONE OF THIS SCRIPT APPLIES except the Mosquitto line. cert-manager
# rewrites the agrarian-tls Secret, the kubelet refreshes the projected files in
# place, MediaMTX rereads as always and Traefik's file watcher sees the Secret
# directly. Mosquitto's SIGHUP still has no answer there — nothing in the manifests
# sends one.
#
# USAGE
#   CF_DNS_API_TOKEN=... ./scripts/renew_certs.sh            # renew if due
#   CF_DNS_API_TOKEN=... ./scripts/renew_certs.sh --force    # renew regardless
#   ./scripts/renew_certs.sh --reload-only                   # skip ACME, just reload
#
# CF_DNS_API_TOKEN is read from the environment or from .env. It needs Zone:DNS:Edit
# and Zone:Zone:Read on this zone, and nothing else.
#
# Run it from cron often enough that a missed day does not matter — weekly is
# plenty for a 90-day certificate, and lego declines politely when renewal is not
# yet due.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOMAIN="${RENEW_DOMAIN:-agrarianlivestock.com}"
EMAIL="${RENEW_EMAIL:-simonesarti98@gmail.com}"
ACME_DIR="$REPO/certificates/acme"
LEAF_DIR="$REPO/certificates/server"
LEGO_IMAGE="${LEGO_IMAGE:-goacme/lego:latest}"

FORCE=false
RELOAD_ONLY=false
for arg in "$@"; do
  case "$arg" in
    --force)       FORCE=true ;;
    --reload-only) RELOAD_ONLY=true ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

log() { printf '%s  %s\n' "$(date +%H:%M:%S)" "$*"; }
fail() { log "FAILED: $*"; exit 1; }

# ── 1. Renew ──────────────────────────────────────────────────────────────────
if [[ "$RELOAD_ONLY" == false ]]; then
  # The token may live in .env, which is gitignored and holds it alongside the
  # database password. Read it without sourcing the file: .env is not a shell
  # script and sourcing it executes whatever is in it.
  if [[ -z "${CF_DNS_API_TOKEN:-}" ]]; then
    CF_DNS_API_TOKEN="$(sed -n 's/^CF_TOKEN=//p' "$REPO/.env" 2>/dev/null | head -1)"
  fi
  [[ -n "${CF_DNS_API_TOKEN:-}" ]] || fail "CF_DNS_API_TOKEN not set and CF_TOKEN not found in .env"

  # The serial BEFORE, so we can tell a real renewal from a no-op. Without this
  # every check below passes while measuring nothing — the same trap
  # run_cert_renewal.sh guards against by asserting the serial changed.
  serial_before="$(openssl x509 -in "$LEAF_DIR/server.crt" -noout -serial 2>/dev/null || echo none)"

  renew_flag=()
  [[ "$FORCE" == true ]] && renew_flag=(--renew-force)

  # `run`, not `renew`: lego v5 has no `renew` subcommand — `run` is documented as
  # "Get or renew a certificate" and carries the renewal flags. Given an existing
  # certificate it checks whether renewal is due and exits successfully having done
  # nothing if it is not, which is exactly the behaviour a weekly cron wants.
  #
  # The corollary is the trap that cost an hour when this certificate was first
  # issued: with a certificate already present, `run` NEVER re-issues on its own
  # judgement of your intent. A staging leaf left in place made the production run a
  # silent no-op — it read it as a renewal that was not yet due and exited 0. Which
  # is why the serial comparison below is not optional.
  log "requesting renewal for $DOMAIN (force=$FORCE)"
  # --user keeps lego from writing root-owned 0600 files the copy below cannot read.
  # --key-type RSA2048 because the default is EC256, and one leaf serves the
  # drone-facing listeners too, where the TLS floor is 1.2 for old ground-station
  # software — the weakest client governs (§7).
  docker run --rm \
    --user "$(id -u):$(id -g)" \
    -e CLOUDFLARE_DNS_API_TOKEN="$CF_DNS_API_TOKEN" \
    -v "$ACME_DIR:/data" \
    "$LEGO_IMAGE" run \
      --accept-tos \
      --email "$EMAIL" \
      --dns cloudflare \
      --domains "$DOMAIN" \
      --domains "*.$DOMAIN" \
      --path /data \
      --key-type RSA2048 \
      --dns.propagation.disable-rns \
      "${renew_flag[@]}" || fail "lego renew"
  # --dns.propagation.disable-rns: the recursive half of the propagation check asks
  # the system resolver, and a home router caches NXDOMAIN for _acme-challenge for
  # the zone's negative TTL (1800s here) between runs — so it reports "not
  # propagated" while Cloudflare is serving the record. The authoritative check
  # still runs, against the servers that actually hold the answer.

  issued="$ACME_DIR/certificates/$DOMAIN.crt"
  [[ -f "$issued" ]] || fail "expected $issued to exist after renewal"

  serial_after="$(openssl x509 -in "$issued" -noout -serial)"
  if [[ "$serial_before" == "$serial_after" ]]; then
    log "certificate unchanged ($serial_after) — not yet due, nothing to reload"
    exit 0
  fi
  log "new certificate: $serial_after (was $serial_before)"

  # THREE FILES, NOT TWO. mosquitto.conf names ca.crt as its cafile, so copying only
  # the leaf and key leaves one terminator pointing at a CA unrelated to the
  # certificate beside it. require_certificate is false, so nothing would verify
  # against it and nothing would complain — which is exactly why it is easy to miss.
  cp "$issued"                                   "$LEAF_DIR/server.crt" || fail "copy crt"
  cp "$ACME_DIR/certificates/$DOMAIN.key"        "$LEAF_DIR/server.key" || fail "copy key"
  cp "$ACME_DIR/certificates/$DOMAIN.issuer.crt" "$LEAF_DIR/ca.crt"     || fail "copy issuer"
  chmod 600 "$LEAF_DIR/server.key"
  log "installed into $LEAF_DIR"
fi

# ── 2. Make each terminator notice ────────────────────────────────────────────
cd "$REPO" || fail "cd $REPO"

if ! docker compose ps --status running --quiet 2>/dev/null | grep -q .; then
  log "stack is not running — nothing to reload; it will read the new leaf at start"
  exit 0
fi

# Traefik: touch a file in the WATCHED directory. The certificate is not in it.
touch "$REPO/configs/traefik/dynamic/routers.yml" && log "traefik: touched dynamic config"

# Mosquitto: SIGHUP, the only one of the three that behaves as documented.
docker compose kill -s HUP mosquitto >/dev/null 2>&1 && log "mosquitto: SIGHUP sent" \
  || log "mosquitto: SIGHUP failed (not running?)"

# MediaMTX: deliberately nothing. See the header.
log "mediamtx: nothing to do — rereads per handshake, and SIGHUP would kill it"

# ── 3. Confirm what is actually being served ──────────────────────────────────
# On a fresh connection, not from the file on disk: the whole point of this script
# is that a correct file and a stale listener look identical from the filesystem.
sleep 3
for spec in "1936:mediamtx" "8883:mosquitto" "443:traefik"; do
  port="${spec%%:*}"; name="${spec#*:}"
  serving="$(echo | timeout 10 openssl s_client -connect "127.0.0.1:$port" \
              -servername "$DOMAIN" 2>/dev/null \
            | openssl x509 -noout -serial 2>/dev/null)"
  printf '  %-10s %s\n' "$name" "${serving:-NO RESPONSE}"
done

log "done"

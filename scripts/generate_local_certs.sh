#!/usr/bin/env bash
# A local certificate authority and one wildcard leaf, for running the ingress
# tier before a public hostname exists.
#
# WHY THIS EXISTS
# ---------------
# Let's Encrypt will not issue for an IP address, and MEDIAMTX_HOST is an IP
# today (CLOUD_ARCHITECTURE.md §9). But nothing in the ingress tier actually
# needs a *publicly trusted* certificate to be built and tested: Traefik,
# MediaMTX and Mosquitto all read a certificate and a key from disk and none of
# them knows or cares who signed it. The only thing public trust buys is a
# browser that belongs to somebody else, and there is not one of those yet.
#
# So this script stands in for cert-manager. When a hostname arrives, ACME
# replaces it and nothing else in the stack changes — which is the point.
#
# ONE LEAF FOR ALL THREE TERMINATORS
# ----------------------------------
# §7 has three of them: Traefik for the HTTP family, and MediaMTX and Mosquitto
# terminating their own. A single wildcard leaf covers all three, so the later
# RTMPS and MQTTS steps mount the same files this script already wrote. The
# names are server.crt / server.key because that is what mosquitto.conf's
# commented-out block already expects at /mosquitto/certs/.
#
# THE LEAF IS DELIBERATELY SHORT-LIVED
# ------------------------------------
# 397 days, the maximum a browser will accept, rather than the ten years a
# throwaway local cert usually gets. A leaf that never expires is a way to never
# find out what happens when one does.
#
# --renew-leaf reissues against the same CA, which is exactly the file swap a
# renewal performs, and it is what tests/comms/run_cert_renewal.sh drives. The
# answers are in §7 and they are not symmetric: MediaMTX rereads the file by
# itself (and does NOT survive a SIGHUP), Mosquitto rereads on SIGHUP, and
# Traefik notices nothing until a file in its watched dynamic directory is
# touched. Whatever writes the real renewal hook needs all three.
#
# Usage:
#   ./scripts/generate_local_certs.sh [domain]        # CA + leaf (default: agrarian.local)
#   ./scripts/generate_local_certs.sh --renew-leaf [domain]
#
# Then trust certificates/ca/ca.crt in the browser, or pass it as --cacert to curl.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Overridable so the test runner can issue into a throwaway directory rather than
# overwriting the CA somebody has already installed in their browser.
CERTS="${CERT_DIR:-$REPO/certificates}"
CA_DIR="$CERTS/ca"
LEAF_DIR="$CERTS/server"

RENEW_ONLY=false
if [[ "${1:-}" == "--renew-leaf" ]]; then
  RENEW_ONLY=true
  shift
fi

DOMAIN="${1:-agrarian.local}"

# The IPs the leaf is valid for as well as the names. Without these, reaching the
# stack as https://192.168.1.10/ fails certificate validation even with the CA
# trusted — and an IP is exactly what MEDIAMTX_HOST holds today, so leaving them
# out would make the local setup fail in the one configuration it exists to serve.
HOST_IP="${HOST_IP:-$(hostname -I 2>/dev/null | awk '{print $1}')}"

mkdir -p "$CA_DIR" "$LEAF_DIR"

# ── The CA ────────────────────────────────────────────────────────────────────
# Reused across renewals, because the whole value of a local CA is that it is
# installed in a trust store once. Regenerating it would mean reinstalling it.
if [[ ! -f "$CA_DIR/ca.crt" || ! -f "$CA_DIR/ca.key" ]]; then
  if [[ "$RENEW_ONLY" == true ]]; then
    echo "error: --renew-leaf needs an existing CA in $CA_DIR" >&2
    exit 1
  fi
  echo "==> creating the local CA (10 years)"
  openssl req -x509 -newkey rsa:4096 -sha256 -days 3650 -nodes \
    -keyout "$CA_DIR/ca.key" -out "$CA_DIR/ca.crt" \
    -subj "/O=AGRARIAN/CN=AGRARIAN local CA" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
  chmod 600 "$CA_DIR/ca.key"
else
  echo "==> reusing the existing CA in $CA_DIR"
fi

# ── The leaf ──────────────────────────────────────────────────────────────────
# A wildcard, because the ingress tier separates services by hostname the moment
# a real domain arrives: portal.<domain>, media.<domain>, ws.<domain>. Covering
# them now means that switch is a routing change rather than a certificate one.
#
# localhost and 127.0.0.1 are here for the test runner, which reaches Traefik
# through a published port rather than through a name.
SAN="DNS:${DOMAIN},DNS:*.${DOMAIN},DNS:localhost,IP:127.0.0.1"
if [[ -n "$HOST_IP" ]]; then
  SAN="${SAN},IP:${HOST_IP}"
fi

echo "==> issuing the leaf for ${DOMAIN} (397 days)"
echo "    SANs: ${SAN}"

openssl req -newkey rsa:2048 -nodes \
  -keyout "$LEAF_DIR/server.key" -out "$LEAF_DIR/server.csr" \
  -subj "/O=AGRARIAN/CN=${DOMAIN}" 2>/dev/null

# extendedKeyUsage=serverAuth is not optional decoration: a leaf without it is
# rejected outright by Chrome and by Go's TLS client, which is what Traefik's own
# health checks and half the tooling here are written in.
openssl x509 -req -in "$LEAF_DIR/server.csr" -sha256 -days 397 \
  -CA "$CA_DIR/ca.crt" -CAkey "$CA_DIR/ca.key" -CAcreateserial \
  -out "$LEAF_DIR/server.crt" \
  -extfile <(printf 'basicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=%s\n' "$SAN") 2>/dev/null

rm -f "$LEAF_DIR/server.csr"
chmod 600 "$LEAF_DIR/server.key"

# The CA next to the leaf, so a container that needs to VERIFY the chain can
# mount one directory instead of two. Traefik itself never reads it.
cp "$CA_DIR/ca.crt" "$LEAF_DIR/ca.crt"

echo
echo "==> done"
echo "    CA    $CA_DIR/ca.crt        (trust this in the browser)"
echo "    leaf  $LEAF_DIR/server.crt"
echo "          $LEAF_DIR/server.key"
echo
echo "    Expires: $(openssl x509 -in "$LEAF_DIR/server.crt" -noout -enddate | cut -d= -f2)"
echo
echo "    Add to /etc/hosts to reach the stack by name:"
echo "      127.0.0.1  portal.${DOMAIN} media.${DOMAIN} ws.${DOMAIN}"

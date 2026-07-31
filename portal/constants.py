# ── Service ───────────────────────────────────────────────────────────────────

API_HOST = "0.0.0.0"
API_PORT = 8000

# ── Session cookie ────────────────────────────────────────────────────────────

SESSION_COOKIE = "agrarian_session"

# Matches SESSION_TOKEN_TTL_S in db_writer/constants.py. Deliberately not longer:
# the cookie outliving the token it carries would give the browser a credential
# db-writer has already stopped accepting, which shows up as a working page that
# 401s on its first click.
SESSION_COOKIE_MAX_AGE_S = 8 * 60 * 60

# ── Public endpoints the browser dials directly ───────────────────────────────
#
# The portal composes these URLs but is never on the path they name. Video is
# WebRTC (DTLS-SRTP, end to end) and alerts are a WebSocket to ws-server; both go
# straight from the browser to the hub, authorised by a flight-scoped viewer
# token. See §8 — these are the ports that are meant to be externally reachable.

RTMPS_PORT = 1936      # what the operator types into the drone controller
WEBRTC_PORT = 8889     # MediaMTX WHEP signalling and its built-in reader page
HLS_PORT = 8888        # MediaMTX HLS, the fallback for browsers that need it
WS_PORT = 8765         # ws-server's viewer WebSocket

# ── Ingest path naming ────────────────────────────────────────────────────────
#
# `in/<stream_key>` is decided in db-writer (/flight/open derives it there); this
# is the same scheme spelled a second time, because the portal has to show the
# operator a URL before any flight exists to ask about.
INGEST_PATH_PREFIX = "in"

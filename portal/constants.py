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
# WHEP signalling. MediaMTX also serves a reader page at /<path>/ on this port,
# which this portal deliberately does not use: it 401s without ever consulting
# the auth hook, so no viewer token can open it. watch.js speaks WHEP directly.
WEBRTC_PORT = 8889
HLS_PORT = 8888        # MediaMTX HLS, the fallback for browsers that need it
WS_PORT = 8765         # ws-server's viewer WebSocket

# ── Rate limits ───────────────────────────────────────────────────────────────
#
# The two anonymous endpoints, and the only two that cannot be protected by a
# credential. See rate_limit.py for why login is counted twice.
#
# All three limits are per fixed window. Chosen to be invisible to a person and
# ruinous to a script: a real user mistypes a password three or four times, not
# ten, and nobody signs up twenty times an hour.

RATE_LIMIT_WINDOW_S = 15 * 60

# Per account, across every source. What credential stuffing runs into — one
# account tried from a thousand hosts is still one account.
LOGIN_MAX_FAILURES_PER_ACCOUNT = 10

# Per source address, across every account. What password spraying runs into —
# one common password tried against a thousand accounts is still one source.
# Higher than the per-account limit because an office behind one NAT shares it.
LOGIN_MAX_FAILURES_PER_IP = 30

# Registration counts every attempt, not just successes: an endpoint that says
# "already registered" is an account-existence oracle whether or not it creates
# anything.
REGISTER_MAX_PER_IP = 20
REGISTER_WINDOW_S = 60 * 60

# ── Ingest path naming ────────────────────────────────────────────────────────
#
# `in/<stream_key>` is decided in db-writer (/flight/open derives it there); this
# is the same scheme spelled a second time, because the portal has to show the
# operator a URL before any flight exists to ask about.
INGEST_PATH_PREFIX = "in"

# ── Processing modes ──────────────────────────────────────────────────────────
#
# Display names for the slot dropdown, and ONLY that. db-writer owns the list of
# modes it will accept and answers 400 for anything else, so this is presentation
# rather than a second validator: a value here that db-writer has dropped fails
# safe as a rejected form post, not as a container that exits at startup.
APP_MODE_CHOICES = (
    ("danger_detection", "Danger detection"),
    ("health_monitoring", "Herd monitoring"),
)

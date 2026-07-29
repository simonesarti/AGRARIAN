# ── Env-configurable defaults ─────────────────────────────────────────────────

WS_PORT  = 8765
REDIS_URL = "redis://redis:6379/0"

# ── Fixed tuning values ───────────────────────────────────────────────────────

WS_HOST  = "0.0.0.0"
API_PORT = 8000

WS_MANAGER_BROADCAST_TIMEOUT    = 2.0      # 2.0 s
WS_MANAGER_PING_INTERVAL        = 5.0      # 5.0 s
WS_MANAGER_PING_TIMEOUT         = 20.0     # 20.0 s

# ── Redis fan-out ─────────────────────────────────────────────────────────────

REDIS_CHANNEL_PREFIX = "flight"
REDIS_POLL_TIMEOUT   = 1.0                 # 1.0 s — also bounds shutdown latency
REDIS_RETRY_DELAY    = 1.0                 # 1.0 s — backoff after a reader error

# Nothing is cached: a viewer receives only alerts raised while it is connected.
# Replaying the last one would assert something about the field that may no longer
# hold. History lives in the database, timestamped.

# ── Auth ──────────────────────────────────────────────────────────────────────

JWT_ALGORITHM = "HS256"

# Must match db_writer/constants.py — db-writer mints, ws-server validates. Both
# token kinds share a signing key, so the scope claim is what keeps a viewer token
# from being accepted as a publisher token for the same flight.
TOKEN_SCOPE_VIEW    = "view"
TOKEN_SCOPE_PUBLISH = "publish"

# Close codes sent to rejected viewers (4000-4999 is the application range).
WS_CLOSE_UNAUTHORIZED = 4401

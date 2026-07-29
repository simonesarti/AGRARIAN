# The alert queue is process-wide, not per-flight: one writer thread drains alerts
# for every flight this replica handles. It is therefore sized for the busiest
# moment across all concurrent flights, not for one — the old per-flight value of 5
# would have had a single active flight starving every other one on the replica.
ALERT_QUEUE_SIZE = 500

DB_MANAGER_POOL_SIZE = 5
DB_MANAGER_MAX_OVERFLOW = 10

DB_MANAGER_QUEUE_WAIT_TIMEOUT = 0.1        # 100 ms
DB_MANAGER_THREAD_CLOSE_TIMEOUT = 5.0      # 5.0 s

# ── Viewer session tokens ─────────────────────────────────────────────────────
# Signed here, validated by ws-server. The TTL bounds how long a leaked token is
# useful — tokens travel in the WebSocket query string and so reach proxy logs.

JWT_ALGORITHM = "HS256"
VIEWER_TOKEN_TTL_S = 12 * 60 * 60          # 12 h — longer than any plausible flight

# Both token kinds are signed with the same secret, so the scope claim is what keeps
# them apart. Without it a viewer token would be accepted as a publisher token and a
# viewer could inject alerts into the flight they are watching.
TOKEN_SCOPE_VIEW    = "view"
TOKEN_SCOPE_PUBLISH = "publish"

# Publisher tokens are minted when a flight opens and live only as long as the app
# container processing it. A flight outlasting this is far more likely to be a stuck
# container than a genuinely long sortie.
PUBLISHER_TOKEN_TTL_S = 24 * 60 * 60       # 24 h

# ── Drone stream keys ─────────────────────────────────────────────────────────
# The operator types the ingest URL into the drone controller by hand before every
# flight, so the key has to be short enough to transcribe without error. That rules
# out a JWT and is why these are persistent-until-revoked rather than expiring.
#
# Alphabet is Crockford base32 (no i/l/o/u), which removes the character pairs
# people misread off a screen. 16 chars over a 32-symbol alphabet = 80 bits.

STREAM_KEY_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
STREAM_KEY_LENGTH = 16

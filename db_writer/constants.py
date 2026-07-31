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

# All three token kinds are signed with the same secret, so the scope claim is what
# keeps them apart. Without it a viewer token would be accepted as a publisher token
# and a viewer could inject alerts into the flight they are watching.
TOKEN_SCOPE_VIEW    = "view"
TOKEN_SCOPE_PUBLISH = "publish"

# The portal's credential. Unlike the other two it names a USER, not a flight, and
# is the only one that grants authority over an account rather than over one flight's
# data. That is also why it is the shortest-lived: it is the most valuable of the
# three, and there is no refresh mechanism, so this is the whole session.
TOKEN_SCOPE_SESSION = "session"
SESSION_TOKEN_TTL_S = 8 * 60 * 60          # 8 h — a working day

# Publisher tokens are minted when a flight opens and live only as long as the app
# container processing it. A flight outlasting this is far more likely to be a stuck
# container than a genuinely long sortie.
PUBLISHER_TOKEN_TTL_S = 24 * 60 * 60       # 24 h

# ── Account registration ──────────────────────────────────────────────────────
# Registration is open to anyone, so create_user is reachable from the public
# internet and every argument it takes is untrusted.

# NIST SP 800-63B's floor for a user-chosen secret. Deliberately no composition
# rules (a digit, a symbol, a capital): they cost users more than they cost an
# attacker, and the same document advises against them.
MIN_PASSWORD_LENGTH = 8

# bcrypt's hard limit, and not a policy choice. Everything past the 72nd BYTE is
# ignored by the algorithm — bcrypt 5.x raises rather than truncating, so without
# this check a long passphrase is a 500 instead of a clear error. Bytes, not
# characters: one emoji is four of these.
MAX_PASSWORD_BYTES = 72

# RFC 5321's maximum forward-path length.
MAX_EMAIL_LENGTH = 254

# ── Stream slots ──────────────────────────────────────────────────────────────
# A stream is a concurrency slot, and a slot is what lets a GPU container come
# into existence. With registration open (§3), this cap is the only thing between
# an anonymous signup and unbounded GPU spend, so POST /streams must not be an
# unbounded resource-creation endpoint.
#
# Counts ACTIVE slots only. A retired one cannot publish, so it is not a slot —
# and a user who wants a retired slot back rotates its key rather than adding
# another, which is why churn does not defeat the cap.
MAX_STREAMS_PER_USER = 10

# Matches the streams.label column, so an over-long label is a clear 400 rather
# than a driver-dependent truncation or an opaque database error.
MAX_STREAM_LABEL_LENGTH = 128

# ── Drone stream keys ─────────────────────────────────────────────────────────
# The operator types the ingest URL into the drone controller by hand before every
# flight, so the key has to be short enough to transcribe without error. That rules
# out a JWT and is why these are persistent-until-revoked rather than expiring.
#
# Alphabet is Crockford base32 (no i/l/o/u), which removes the character pairs
# people misread off a screen. 16 chars over a 32-symbol alphabet = 80 bits.

STREAM_KEY_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
STREAM_KEY_LENGTH = 16

# ── MediaMTX auth hook ────────────────────────────────────────────────────────
# MediaMTX POSTs every connection attempt to /auth/mediamtx and obeys the status
# code: 2xx allows, anything else denies. These are the action values it sends.
#
# 'api', 'metrics' and 'pprof' are deliberately absent: they are excluded in
# mediamtx.yml via authHTTPExclude, and anything not listed here is denied, so a
# future MediaMTX action arrives closed rather than open.

MEDIAMTX_ACTION_PUBLISH = "publish"

# 'playback' serves recorded segments through the playback server. It is gated
# exactly like a live read — same path, same viewer token — so that enabling the
# playback server later cannot quietly expose recordings.
MEDIAMTX_READ_ACTIONS = ("read", "playback")

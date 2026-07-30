# ── Service ───────────────────────────────────────────────────────────────────

API_HOST = "0.0.0.0"
API_PORT = 8000

# ── Flight containers ─────────────────────────────────────────────────────────

# The app installs SIGTERM/SIGINT handlers and drains its queues on the way down, so
# teardown gives it room to flush rather than killing it outright.
STOP_TIMEOUT_S = 20

# The annotation worker takes SIGBUS on Docker's 64 MB default once the frame buffers
# live in shared memory.
DEFAULT_SHM_SIZE = "256m"

# ── Stream lifecycle ──────────────────────────────────────────────────────────

# MediaMTX fires runOnUnavailable the moment a publisher drops, including for a
# momentary radio glitch the drone recovers from seconds later. Tearing the container
# down instantly would mean a cold GPU start — model weights reloaded — for what was a
# blip, so teardown waits this long for the same key to come back.
#
# Set to 0 to tear down immediately.
RECONNECT_GRACE_S = 30

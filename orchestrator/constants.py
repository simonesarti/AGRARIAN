# ── Service ───────────────────────────────────────────────────────────────────

API_HOST = "0.0.0.0"
API_PORT = 8000

# ── Flight containers ─────────────────────────────────────────────────────────

# The app installs SIGTERM/SIGINT handlers and drains its queues on the way down, so
# teardown gives it room to flush rather than killing it outright.
STOP_TIMEOUT_S = 20

# The annotation worker takes SIGBUS on Docker's 64 MB default once the frame buffers
# live in shared memory. Written in Docker's spelling under both backends; the
# Kubernetes one translates it to a quantity (256m → 256Mi) rather than making the
# operator remember which platform they are configuring.
DEFAULT_SHM_SIZE = "256m"

# Marks everything this orchestrator started, as a container label under Docker and a
# Job label under Kubernetes. It is the only state a crash cannot lose, and recover()
# is built entirely on finding it again — so the two backends must spell it the same.
FLIGHT_LABEL = "agrarian.flight_id"

# ── Kubernetes ────────────────────────────────────────────────────────────────

DEFAULT_NAMESPACE = "agrarian-flights"

# Deletion is asynchronous: the API server accepts it and the garbage collector
# catches up. start() waits this long for a stale Job's name to come free before
# giving up and letting create() raise the name conflict itself.
JOB_DELETE_TIMEOUT_S = 30

# ── Stream lifecycle ──────────────────────────────────────────────────────────

# MediaMTX fires runOnUnavailable the moment a publisher drops, including for a
# momentary radio glitch the drone recovers from seconds later. Tearing the container
# down instantly would mean a cold GPU start — model weights reloaded — for what was a
# blip, so teardown waits this long for the same key to come back.
#
# Set to 0 to tear down immediately.
RECONNECT_GRACE_S = 30

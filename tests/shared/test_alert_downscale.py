"""
The alert image is reduced before it is encoded, and the stored dimensions say so.

Alert JPEGs used to be the full annotated frame — 1920x1080, ~400 KB — written into a
LargeBinary column on a 1.0 s cooldown. §9 recorded that as a known weakness and named
this fix. It has a second consequence that is easier to miss: db-writer's alert queue is
bounded by COUNT (500), not by bytes, so a full queue of full-HD frames is ~200 MB of
resident objects against a 512Mi pod limit — the queue could hit the memory ceiling
before the length ceiling it was sized by.

The assertion that matters is not that the bytes shrank. It is that image_width and
image_height describe what was ENCODED: those columns are what the history page renders
with, and labelling every row with a size no stored image has would be a silent defect
that no byte-count check can see.

Run with no stack and no GPU. Runs from /tmp rather than the source tree because
output_alert_streamer opens ./logs/alert_out.log at import time, and the mount is
read-only:

  docker run --rm -v "$PWD:/src:ro" -v "$PWD/tests/shared:/tests:ro" -w /tmp \
    -e APP_SRC=/src python:3.11-slim \
    sh -c "pip install -q numpy opencv-python-headless pydantic requests; \
           mkdir -p logs; python /tests/test_alert_downscale.py"
"""

import os
import sys
import cv2
import numpy as np

sys.path.insert(0, os.environ.get("APP_SRC", "."))

from app.shared.processes.output_alert_streamer import (
    NotificationsStreamWriter, NotificationsStreamWriterConfig,
)
from app.shared.processes.constants import ALERTS_MAX_IMAGE_EDGE_PX

results = []


def check(name, ok, extra=""):
    results.append(ok)
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{extra}]" if extra else ""))


def writer(max_edge):
    """A writer with only the fields _downscale/_compress_frame touch."""
    cfg = NotificationsStreamWriterConfig(
        alerts_jpeg_quality=85,
        alerts_max_image_edge_px=max_edge,
        flight_id=1,
        publisher_token="t",
        ws_server_url="http://ws:8000",
        db_writer_url="http://db:8000",
    )
    w = NotificationsStreamWriter.__new__(NotificationsStreamWriter)
    w.config = cfg
    return w


def frame(h, w):
    """
    Pure noise. Correct for the GEOMETRY assertions and wrong for the byte ones:
    it is the worst case JPEG can be handed, and encodes about four times larger
    than the real annotated frames §9 measured at ~400 KB.
    """
    rng = np.random.default_rng(0)
    return rng.integers(0, 255, (h, w, 3), dtype=np.uint8)


def realistic_frame(h, w):
    """
    A frame that compresses the way an annotated one does: smooth ground gradients
    with hard-edged detection boxes drawn over them.

    Used for every assertion about SIZE. Measuring those against noise would report a
    memory figure four times worse than anything this pipeline ever produces — a test
    that fails for a reason the code is not responsible for is as useless as one that
    passes for the wrong reason.
    """
    y = np.broadcast_to(np.linspace(0, 255, h, dtype=np.float32)[:, None], (h, w))
    x = np.broadcast_to(np.linspace(0, 255, w, dtype=np.float32)[None, :], (h, w))
    img = np.stack([(y + x) / 2, y * 0.6 + 40, x * 0.4 + 60], axis=-1)
    img = np.clip(img, 0, 255).astype(np.uint8)
    # Annotation boxes: the hard edges INTER_AREA has to handle without aliasing.
    for i in range(6):
        top, left = (i * h) // 8 + 20, (i * w) // 9 + 30
        cv2.rectangle(img, (left, top), (left + w // 7, top + h // 9), (0, 0, 255), 3)
    return img


# ── the default actually reduces a real frame ─────────────────────────────────
w = writer(ALERTS_MAX_IMAGE_EDGE_PX)
out = w._downscale(frame(1080, 1920))
check("a 1920x1080 annotated frame is reduced", out.shape[:2] == (540, 960), f"{out.shape[1]}x{out.shape[0]}")
check("...and the aspect ratio is preserved exactly",
      abs((1920 / 1080) - (out.shape[1] / out.shape[0])) < 1e-9)

# ── never upscales ────────────────────────────────────────────────────────────
small = frame(180, 320)
check("a frame already below the limit is untouched", w._downscale(small) is small)
check("a frame exactly at the limit is untouched", w._downscale(frame(960, 960)).shape[:2] == (960, 960))

# ── portrait: the LONGEST edge is what is capped ───────────────────────────────
tall = w._downscale(frame(1920, 1080))
check("a portrait frame is capped on its height, not its width",
      max(tall.shape[:2]) == ALERTS_MAX_IMAGE_EDGE_PX, f"{tall.shape[1]}x{tall.shape[0]}")

# ── disabling it stores the frame as it arrives ────────────────────────────────
raw = frame(1080, 1920)
check("max edge 0 disables resizing entirely", writer(0)._downscale(raw) is raw)

# ── the stored dimensions describe the STORED image ───────────────────────────
_, big_bytes, bw, bh = writer(0)._compress_frame(realistic_frame(1080, 1920))
_, small_bytes, sw, sh = w._compress_frame(realistic_frame(1080, 1920))

check("_compress_frame reports the encoded width/height", (sw, sh) == (960, 540), f"{sw}x{sh}")
check("...and reports the original when resizing is off", (bw, bh) == (1920, 1080), f"{bw}x{bh}")

# This is the control. Without it the assertion above passes against an
# implementation that resizes and then reports frame.shape of the incoming array —
# which is exactly the bug this signature change exists to make impossible.
check("...and those differ, so the dimensions are not just the input's",
      (sw, sh) != (bw, bh))

# ── the point of the exercise ─────────────────────────────────────────────────
check("the stored JPEG is materially smaller", len(small_bytes) < len(big_bytes) / 2,
      f"{len(big_bytes)} -> {len(small_bytes)} bytes")

# A full db-writer queue of these must fit in the pod's memory limit. 500 is
# ALERT_QUEUE_SIZE; 512Mi is the limit in configs/k8s/hub/db-writer.yaml. The
# before/after are printed together because the ratio is the durable claim — the
# absolute figure moves with the imagery, and real aerial frames compress worse than
# this synthetic one.
before_mb = (len(big_bytes) * 500) / (1024 * 1024)
queue_mb = (len(small_bytes) * 500) / (1024 * 1024)
check("a full 500-item alert queue fits well inside a 512Mi pod", queue_mb < 128,
      f"{before_mb:.0f} MB -> {queue_mb:.0f} MB")

print()
print("=" * 60)
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)

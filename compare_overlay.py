"""
Visual comparison of draw_dangerous_area approaches.
Downloads a random image, applies both methods with a square (danger) and
circle (intersection) mask on the right side of the frame, saves results.
"""

import urllib.request
import numpy as np
import cv2

# ── download a random 1280×720 image ────────────────────────────────────────
url = "https://picsum.photos/1280/720"
tmp = "/tmp/test_frame.jpg"
print(f"Downloading {url} ...")
urllib.request.urlretrieve(url, tmp)
frame_orig = cv2.imread(tmp)
frame_orig = cv2.resize(frame_orig, (1280, 720))
print(f"Frame shape: {frame_orig.shape}")

H, W = 720, 1280

# ── masks: square (danger) and circle (intersection), non-overlapping ────────
danger_mask   = np.zeros((H, W), dtype=np.uint8)
intersect_mask = np.zeros((H, W), dtype=np.uint8)

cv2.rectangle(danger_mask,   (900, 120), (1200, 480), 1, cv2.FILLED)
cv2.circle(intersect_mask,   (1060, 590), 100,        1, cv2.FILLED)

# ── pre-allocated colour frames and buffers ──────────────────────────────────
RED    = (0, 0, 255)
YELLOW = (0, 255, 255)
red_quarter    = tuple(int(c * 0.25) for c in RED)
yellow_quarter = tuple(int(c * 0.25) for c in YELLOW)

shape = (H, W, 3)
color_danger_frame    = np.full(shape, red_quarter,    dtype=np.uint8)
color_intersect_frame = np.full(shape, yellow_quarter, dtype=np.uint8)
danger_buf  = np.zeros(shape, dtype=np.uint8)
overlay_buf = np.zeros(shape, dtype=np.uint8)

# ── approach 1: current (fill + bitwise_and + add) ───────────────────────────
frame_current = frame_orig.copy()
danger_buf.fill(0)
overlay_buf.fill(0)
cv2.bitwise_and(color_danger_frame,    color_danger_frame,    dst=danger_buf,  mask=danger_mask)
cv2.bitwise_and(color_intersect_frame, color_intersect_frame, dst=overlay_buf, mask=intersect_mask)
cv2.add(danger_buf, overlay_buf, dst=overlay_buf)
cv2.add(frame_current, overlay_buf, dst=frame_current)

# ── approach 2: add (full-frame) + copyTo ────────────────────────────────────
frame_new = frame_orig.copy()
cv2.add(frame_new, color_danger_frame,    dst=danger_buf)
cv2.add(frame_new, color_intersect_frame, dst=overlay_buf)
cv2.copyTo(danger_buf,  danger_mask,    frame_new)
cv2.copyTo(overlay_buf, intersect_mask, frame_new)

# ── pixel-level diff ─────────────────────────────────────────────────────────
diff = cv2.absdiff(frame_current, frame_new)
max_diff = int(diff.max())
nonzero  = int(np.count_nonzero(diff))
print(f"Max pixel difference: {max_diff}   Non-zero diff pixels: {nonzero}")

# ── save ─────────────────────────────────────────────────────────────────────
out_dir = "processing_results"

cv2.imwrite(f"{out_dir}/overlay_original.png", frame_orig)
cv2.imwrite(f"{out_dir}/overlay_current.png",  frame_current)
cv2.imwrite(f"{out_dir}/overlay_new.png",       frame_new)

# side-by-side with labels
def labelled(img, text):
    out = img.copy()
    cv2.putText(out, text, (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255,255,255), 3, cv2.LINE_AA)
    cv2.putText(out, text, (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0,0,0),       1, cv2.LINE_AA)
    return out

comparison = np.hstack([
    labelled(frame_orig,    "original"),
    labelled(frame_current, "current (fill+bitwise_and)"),
    labelled(frame_new,     "add+copyTo"),
])
cv2.imwrite(f"{out_dir}/overlay_comparison.png", comparison)
print(f"Saved to {out_dir}/overlay_*.png")

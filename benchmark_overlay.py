"""
Benchmark: draw_dangerous_area approaches.

Approaches compared:
  1. current  — fill + cv2.bitwise_and + cv2.add  (6 full-frame ops)
  2. np_mul   — np.multiply + np.add + cv2.add    (4 full-frame ops, no fills)

Run: python benchmark_overlay.py
"""

import time
import numpy as np
import cv2

# ── frame dimensions matching the pipeline ──────────────────────────────────
H, W = 720, 1280

RED    = (0, 0, 255)
YELLOW = (0, 255, 255)

red_quarter    = tuple(int(c * 0.25) for c in RED)
yellow_quarter = tuple(int(c * 0.25) for c in YELLOW)

# ── pre-allocated buffers (same as get_danger_intersect_colored_frames) ──────
shape = (H, W, 3)
color_danger_frame    = np.full(shape, red_quarter,    dtype=np.uint8)
color_intersect_frame = np.full(shape, yellow_quarter, dtype=np.uint8)
danger_buf  = np.zeros(shape, dtype=np.uint8)
overlay_buf = np.zeros(shape, dtype=np.uint8)

# ── benchmark helpers ────────────────────────────────────────────────────────

def make_frame():
    return np.random.randint(100, 200, shape, dtype=np.uint8)

def make_mask(density: float):
    """Binary uint8 mask with approximately `density` fraction of pixels set."""
    return (np.random.rand(H, W) < density).astype(np.uint8)

def run(fn, frame, danger_mask, intersect_mask, n_warmup=50, n_runs=500):
    for _ in range(n_warmup):
        fn(frame.copy(), danger_mask, intersect_mask)
    times = []
    for _ in range(n_runs):
        f = frame.copy()
        t0 = time.perf_counter()
        fn(f, danger_mask, intersect_mask)
        times.append((time.perf_counter() - t0) * 1000)
    times.sort()
    n = len(times)
    return dict(
        mean   = sum(times) / n,
        median = times[n // 2],
        p95    = times[int(n * 0.95)],
        p99    = times[int(n * 0.99)],
        min    = times[0],
        max    = times[-1],
    )

# ── approach 1: current (fill + bitwise_and + add) ───────────────────────────

def approach_current(frame, danger_mask, intersect_mask):
    danger_buf.fill(0)
    overlay_buf.fill(0)
    cv2.bitwise_and(color_danger_frame,    color_danger_frame,    dst=danger_buf,  mask=danger_mask)
    cv2.bitwise_and(color_intersect_frame, color_intersect_frame, dst=overlay_buf, mask=intersect_mask)
    cv2.add(danger_buf, overlay_buf, dst=overlay_buf)
    cv2.add(frame, overlay_buf, dst=frame)

# ── approach 2: numpy multiply (no fills) ────────────────────────────────────

def approach_np_mul(frame, danger_mask, intersect_mask):
    np.multiply(color_danger_frame,    danger_mask[:, :, np.newaxis],    out=overlay_buf)
    np.multiply(color_intersect_frame, intersect_mask[:, :, np.newaxis], out=danger_buf)
    np.add(overlay_buf, danger_buf, out=overlay_buf)
    cv2.add(frame, overlay_buf, dst=frame)

# ── approach 3: cv2.multiply — pre-allocated 3-ch mask buffers ───────────────
# cv2.multiply does not broadcast (H,W,1) × (H,W,3), so masks must be expanded
# to 3 channels first. Two extra pre-allocated buffers hold the expanded masks.

danger_mask_3ch   = np.zeros(shape, dtype=np.uint8)
intersect_mask_3ch = np.zeros(shape, dtype=np.uint8)

def approach_cv2_mul(frame, danger_mask, intersect_mask):
    # expand single-channel mask to 3 channels in-place
    danger_mask_3ch[:, :, 0]   = danger_mask
    danger_mask_3ch[:, :, 1]   = danger_mask
    danger_mask_3ch[:, :, 2]   = danger_mask
    intersect_mask_3ch[:, :, 0] = intersect_mask
    intersect_mask_3ch[:, :, 1] = intersect_mask
    intersect_mask_3ch[:, :, 2] = intersect_mask
    cv2.multiply(color_danger_frame,    danger_mask_3ch,    dst=overlay_buf)
    cv2.multiply(color_intersect_frame, intersect_mask_3ch, dst=danger_buf)
    cv2.add(overlay_buf, danger_buf, dst=overlay_buf)
    cv2.add(frame, overlay_buf, dst=frame)

# ── approach 4: add (full-frame, no mask) + copyTo ───────────────────────────
# cv2.copyTo(src, mask, dst) preserves dst where mask==0 — unlike cv2.add(mask=)
# which zeros dst where mask==0. No fills needed: the unmasked add overwrites
# danger_buf and overlay_buf completely each frame.

def approach_add_copyto(frame, danger_mask, intersect_mask):
    cv2.add(frame, color_danger_frame,    dst=danger_buf)
    cv2.add(frame, color_intersect_frame, dst=overlay_buf)
    cv2.copyTo(danger_buf,  danger_mask,   frame)
    cv2.copyTo(overlay_buf, intersect_mask, frame)

# ── run benchmarks at several mask densities ─────────────────────────────────

densities = {
    "no danger  (0%)":   (0.00, 0.00),
    "sparse     (2%)":   (0.02, 0.01),
    "moderate  (15%)":   (0.12, 0.03),
    "heavy     (40%)":   (0.30, 0.10),
}

print(f"Frame size: {W}×{H}   shape: {shape}")
print(f"{'':30s}  {'mean':>7}  {'median':>7}  {'p95':>7}  {'min':>7}  {'max':>7}")
print("-" * 75)

for label, (d_density, i_density) in densities.items():
    frame         = make_frame()
    danger_mask   = make_mask(d_density)
    intersect_mask = make_mask(i_density)

    r1 = run(approach_current,    frame, danger_mask, intersect_mask)
    r2 = run(approach_np_mul,     frame, danger_mask, intersect_mask)
    r3 = run(approach_cv2_mul,    frame, danger_mask, intersect_mask)
    r4 = run(approach_add_copyto, frame, danger_mask, intersect_mask)

    def speedup(r): return r1['mean'] / r['mean']

    print(f"\n  {label}")
    print(f"  {'current (fill+bitwise_and)':32s}  {r1['mean']:6.2f}ms  {r1['median']:6.2f}ms  {r1['p95']:6.2f}ms  {r1['min']:6.2f}ms  speedup: 1.00x")
    print(f"  {'np.multiply':32s}  {r2['mean']:6.2f}ms  {r2['median']:6.2f}ms  {r2['p95']:6.2f}ms  {r2['min']:6.2f}ms  speedup: {speedup(r2):.2f}x")
    print(f"  {'cv2.multiply':32s}  {r3['mean']:6.2f}ms  {r3['median']:6.2f}ms  {r3['p95']:6.2f}ms  {r3['min']:6.2f}ms  speedup: {speedup(r3):.2f}x")
    print(f"  {'add+copyTo':32s}  {r4['mean']:6.2f}ms  {r4['median']:6.2f}ms  {r4['p95']:6.2f}ms  {r4['min']:6.2f}ms  speedup: {speedup(r4):.2f}x")

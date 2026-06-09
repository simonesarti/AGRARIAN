"""
Visual test for map_window_onto_drone_frame().

Setup
-----
  DEM   : 80 × 80 pixels at 5 cm/px  →  covers 400 cm × 400 cm
  Frame : 100 × 100 pixels at ~1 cm/px  (higher resolution than DEM)

DEM pattern
-----------
  Diagonal gradient: dark at top-left, bright at bottom-right.
  Cross at the DEM centre (x=200, y=200) with arms coloured by direction:
    N (up)   → deep purple  (value  10)
    S (down) → lime-yellow  (value 210)
    E (right)→ bright yellow(value 240)
    W (left) → green        (value 150)
    Centre   → max yellow   (value 255)
  - Axis-aligned frame → cross appears as  +  with N up, E right.
  - 45°-rotated frame  → cross appears as  ×; arm colours reveal true orientation.

Each PNG shows two panels (same colour scale = same geographic values):
  LEFT  – DEM with VIRIDIS colormap + yellow frame footprint outline
  RIGHT – Reprojected result at frame resolution

Note: nearest-neighbour resampling from 5 cm/px → 1 cm/px produces visible
5 × 5-pixel blocks in the reprojected frame — each block is one DEM pixel.

Run:
    python tests/danger_detection/visual_test_map_window.py
"""

import math
import os
import sys

import cv2
import numpy as np
from affine import Affine

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from src.danger_detection.utils import map_window_onto_drone_frame, get_frame_transform

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output", "visual_map_window")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Synthetic DEM
# ---------------------------------------------------------------------------
DEM_N   = 80      # pixels
DEM_RES = 5       # cm per pixel
DEM_CM  = DEM_N * DEM_RES          # 400 cm total extent

# Top-left corner at geographic (x=0, y=DEM_CM).  x right, y up.
DEM_TRANSFORM = Affine(DEM_RES, 0, 0,   0, -DEM_RES, DEM_CM)

FRAME_N = 100     # pixels (frame is higher resolution: ~1 cm/px)

# Unique constant value per arm so you can identify cardinal direction in
# the reprojected frame even when it is rotated.
#   N=10  deep-purple  |  S=210  lime-yellow  |  E=240  bright-yellow  |  W=150  green
_ARM_N, _ARM_S, _ARM_E, _ARM_W = 10, 210, 240, 150

def _make_dem() -> np.ndarray:
    """Return (1, DEM_N, DEM_N) float32 DEM with a diagonal gradient and a
    directionally-coloured cross at the geographic centre (x=200, y=200)."""
    rows_f = np.arange(DEM_N, dtype=np.float32).reshape(DEM_N, 1)
    cols_f = np.arange(DEM_N, dtype=np.float32).reshape(1, DEM_N)
    dem = cols_f / (DEM_N - 1) * 160 + rows_f / (DEM_N - 1) * 80   # 0 .. 240
    mid = DEM_N // 2    # pixel 40 → geographic x=200, y=200
    bar = slice(mid - 1, mid + 2)   # 3-pixel-wide bar (rows/cols 39-41)
    dem[0:mid-1,  bar]  = _ARM_N   # North arm  (rows above centre)
    dem[mid+2:,   bar]  = _ARM_S   # South arm  (rows below centre)
    dem[bar,  mid+2:]   = _ARM_E   # East arm   (cols right of centre)
    dem[bar,  0:mid-1]  = _ARM_W   # West arm   (cols left of centre)
    dem[bar,  bar]      = 255      # bright centre intersection
    return dem[np.newaxis].astype(np.float32)

DEM     = _make_dem()
DEM_MIN = float(DEM.min())
DEM_MAX = float(DEM.max())

# ---------------------------------------------------------------------------
# Display constants
# ---------------------------------------------------------------------------
DEM_SCALE   = 5   # 80 px * 5 = 400 display pixels
FRAME_SCALE = 4   # 100 px * 4 = 400 display pixels  (both panels same size)

# ---------------------------------------------------------------------------
# Visualisation helpers
# ---------------------------------------------------------------------------

def _colorise(arr2d: np.ndarray) -> np.ndarray:
    """Normalise to [0,255] using shared DEM min/max, apply VIRIDIS → BGR."""
    norm = np.clip(
        (arr2d - DEM_MIN) / (DEM_MAX - DEM_MIN + 1e-9) * 255, 0, 255
    ).astype(np.uint8)
    return cv2.applyColorMap(norm, cv2.COLORMAP_VIRIDIS)


def _draw_footprint(dem_img: np.ndarray, frame_transform) -> None:
    """Overlay yellow frame footprint on an upscaled DEM image (in-place)."""
    frame_corners = [(0, 0), (FRAME_N-1, 0), (FRAME_N-1, FRAME_N-1), (0, FRAME_N-1)]
    pts = []
    for fc, fr in frame_corners:
        x, y = frame_transform * (fc, fr)
        dc, dr = ~DEM_TRANSFORM * (x, y)
        pts.append((int(round(dc * DEM_SCALE)), int(round(dr * DEM_SCALE))))
    arr = np.array(pts, dtype=np.int32).reshape((-1, 1, 2))
    cv2.polylines(dem_img, [arr], isClosed=True, color=(0, 230, 230), thickness=2)


def _draw_direction_labels(dem_img: np.ndarray) -> None:
    """Draw N/S/E/W labels at the arm ends on the upscaled DEM image (in-place)."""
    mid_px = DEM_N // 2 * DEM_SCALE    # 200 — centre of cross in display pixels
    edge   = DEM_N * DEM_SCALE         # 400 — image edge
    labels = [
        ("N", mid_px - 6, 16),
        ("S", mid_px - 6, edge - 5),
        ("E", edge - 16,  mid_px + 6),
        ("W", 3,           mid_px + 6),
    ]
    for text, x, y in labels:
        cv2.putText(dem_img, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5, (0, 0, 0), 3, cv2.LINE_AA)    # dark outline
        cv2.putText(dem_img, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5, (255, 255, 255), 1, cv2.LINE_AA)


def _label(img: np.ndarray, text: str, y: int = 18) -> None:
    cv2.putText(img, text, (6, y), cv2.FONT_HERSHEY_SIMPLEX, 0.48,
                (255, 255, 255), 1, cv2.LINE_AA)


def _save(name: str, dem_panel: np.ndarray, frame_panel: np.ndarray, note: str = "") -> None:
    gap = np.full((dem_panel.shape[0], 14, 3), 35, dtype=np.uint8)
    out = np.hstack([dem_panel, gap, frame_panel])
    if note:
        cv2.putText(out, note, (6, out.shape[0] - 8), cv2.FONT_HERSHEY_SIMPLEX,
                    0.40, (170, 170, 170), 1, cv2.LINE_AA)
    path = os.path.join(OUTPUT_DIR, f"{name}.png")
    cv2.imwrite(path, out)
    print(f"  DONE  {name}")
    print(f"        → {path}")

# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

def run_test(name: str, frame_transform, assert_cross_at_centre: bool = False, note: str = "") -> np.ndarray:
    result = map_window_onto_drone_frame(
        DEM, DEM_TRANSFORM, frame_transform,
        output_shape=(1, FRAME_N, FRAME_N),
    )

    if assert_cross_at_centre:
        centre = FRAME_N // 2 - 1   # pixel 49 for a 100-px frame
        val = result[0, centre, centre]
        assert val == 255.0, (
            f"[{name}] frame centre pixel ({centre},{centre}) should sample "
            f"DEM cross (255.0), got {val}"
        )

    # --- DEM panel ---
    dem_bgr = cv2.resize(
        _colorise(DEM[0]),
        (DEM_N * DEM_SCALE, DEM_N * DEM_SCALE),
        interpolation=cv2.INTER_NEAREST,
    )
    _draw_footprint(dem_bgr, frame_transform)
    _draw_direction_labels(dem_bgr)
    _label(dem_bgr, "DEM  5 cm/px  |  yellow = frame footprint")

    # --- Frame panel ---
    frame_bgr = cv2.resize(
        _colorise(result[0]),
        (FRAME_N * FRAME_SCALE, FRAME_N * FRAME_SCALE),
        interpolation=cv2.INTER_NEAREST,
    )
    _label(frame_bgr, "Reprojected frame  ~1 cm/px")

    _save(name, dem_bgr, frame_bgr, note)
    return result

# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

def test_axis_aligned_centered():
    """
    Frame centred in DEM, axis-aligned.
    Expected: cross (+) at frame centre; 5×5-pixel blocks visible (DEM resolution).
    """
    cx, cy = DEM_CM / 2, DEM_CM / 2    # 200, 200
    half   = FRAME_N / 2               # 50 cm
    ul = (cx - half, cy + half)        # (150, 250)
    ur = (cx + half, cy + half)        # (250, 250)
    bl = (cx - half, cy - half)        # (150, 150)
    t = get_frame_transform(FRAME_N, FRAME_N, ul, ur, bl)
    run_test("1_axis_aligned_centered", t,
             assert_cross_at_centre=True,
             note="cross (+) must be centred | gradient dark TL → bright BR | 5×5 blocks = DEM pixels")


def test_axis_aligned_top_left():
    """
    Frame at the top-left corner of the DEM.
    Expected: dark values (low col+row gradient), upper-left arms of cross.
    """
    ul = (0,       DEM_CM)
    ur = (FRAME_N, DEM_CM)
    bl = (0,       DEM_CM - FRAME_N)
    t = get_frame_transform(FRAME_N, FRAME_N, ul, ur, bl)
    run_test("2_axis_aligned_top_left", t,
             note="top-left corner of DEM — darkest colours, top-left cross arms visible")


def test_axis_aligned_bottom_right():
    """
    Frame at the bottom-right corner of the DEM.
    Expected: bright values (high col+row), lower-right arms of cross.
    DEM pixel centres stop at x=395, y=5; frame corners kept safely inside.
    """
    ul = (DEM_CM - FRAME_N, FRAME_N)   # (300, 100)
    ur = (DEM_CM - DEM_RES, FRAME_N)   # (395, 100)
    bl = (DEM_CM - FRAME_N, DEM_RES)   # (300, 5)
    t = get_frame_transform(FRAME_N, FRAME_N, ul, ur, bl)
    run_test("3_axis_aligned_bottom_right", t,
             note="bottom-right corner of DEM — brightest colours, bottom-right cross arms visible")


def test_rotated_45_centered():
    """
    Frame rotated 45°, centred on DEM.
    Expected: diamond footprint on DEM; cross appears as × at frame centre;
    gradient runs diagonally across the frame.
    """
    cx, cy  = DEM_CM / 2, DEM_CM / 2
    hw = hh = FRAME_N / 2              # 50 cm half-extents
    theta   = math.radians(45)
    cos_t, sin_t = math.cos(theta), math.sin(theta)

    def rot(dx, dy):
        return (cx + dx*cos_t - dy*sin_t, cy + dx*sin_t + dy*cos_t)

    ul = rot(-hw, +hh)
    ur = rot(+hw, +hh)
    bl = rot(-hw, -hh)
    t = get_frame_transform(FRAME_N, FRAME_N, ul, ur, bl)
    run_test("4_rotated_45_centered", t,
             assert_cross_at_centre=True,
             note="45° rotation — diamond footprint, cross as × at centre, gradient diagonal")


def test_rotated_30_centered():
    """
    Frame rotated 30°, centred on DEM.
    """
    cx, cy  = DEM_CM / 2, DEM_CM / 2
    hw = hh = FRAME_N / 2
    theta   = math.radians(30)
    cos_t, sin_t = math.cos(theta), math.sin(theta)

    def rot(dx, dy):
        return (cx + dx*cos_t - dy*sin_t, cy + dx*sin_t + dy*cos_t)

    ul = rot(-hw, +hh)
    ur = rot(+hw, +hh)
    bl = rot(-hw, -hh)
    t = get_frame_transform(FRAME_N, FRAME_N, ul, ur, bl)
    run_test("5_rotated_30_centered", t,
             assert_cross_at_centre=True,
             note="30° rotation — tilted footprint, cross still centred in frame")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"\nWriting PNGs to: {OUTPUT_DIR}\n")
    test_axis_aligned_centered()
    test_axis_aligned_top_left()
    test_axis_aligned_bottom_right()
    test_rotated_45_centered()
    test_rotated_30_centered()
    print("\nAll visual tests passed.\n")

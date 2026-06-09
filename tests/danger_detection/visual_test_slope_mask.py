"""
Visual test for compute_slope_mask_horn().

Each PNG shows three panels for the same DEM:
  LEFT   – elevation  (VIRIDIS)
  MIDDLE – slope in degrees  (INFERNO, 0–90° range)  — intermediate verification
  RIGHT  – binary mask  (WHITE = slope > threshold / DANGEROUS,  BLACK = safe)

Assertion markers drawn on every panel:
  RED  dot  – pixel expected to be STEEP  (mask == 1)
  BLUE dot  – pixel expected to be SAFE   (mask == 0)

Run:
    python tests/danger_detection/visual_test_slope_mask.py
"""

import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from src.danger_detection.utils import compute_slope_mask_horn

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output", "visual_slope_mask")
os.makedirs(OUTPUT_DIR, exist_ok=True)

GRID_N  = 100   # DEM pixels (100 × 100)
PIXEL_M = 1.0   # metres per pixel
SCALE   = 3     # display upscale: 100 → 300 px per panel


# ---------------------------------------------------------------------------
# Slope helper — same Horn maths, returns float degrees for visualisation
# ---------------------------------------------------------------------------

def _slope_deg(elev: np.ndarray, pixel_size: float) -> np.ndarray:
    """Return Horn slope in degrees (2-D). Input shape (1, H, W)."""
    e = elev[0].astype(np.float32)
    kx = np.array([[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]], dtype=np.float32) / (8 * pixel_size)
    ky = np.array([[-1, -2, -1], [0, 0, 0], [1, 2, 1]], dtype=np.float32) / (8 * pixel_size)
    dx = cv2.filter2D(e, -1, kx, borderType=cv2.BORDER_REPLICATE)
    dy = cv2.filter2D(e, -1, ky, borderType=cv2.BORDER_REPLICATE)
    return np.degrees(np.arctan(np.sqrt(dx**2 + dy**2)))


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def _colorise_dem(arr: np.ndarray) -> np.ndarray:
    mn, mx = arr.min(), arr.max()
    norm = np.clip((arr - mn) / (mx - mn + 1e-9) * 255, 0, 255).astype(np.uint8)
    return cv2.applyColorMap(norm, cv2.COLORMAP_VIRIDIS)


def _colorise_slope(slope: np.ndarray) -> np.ndarray:
    norm = np.clip(slope / 90.0 * 255, 0, 255).astype(np.uint8)
    return cv2.applyColorMap(norm, cv2.COLORMAP_INFERNO)


def _mask_bgr(mask2d: np.ndarray) -> np.ndarray:
    out = np.zeros((*mask2d.shape, 3), dtype=np.uint8)
    out[mask2d == 1] = (255, 255, 255)
    return out


def _draw_marker(img: np.ndarray, row: int, col: int, color: tuple, scale: int) -> None:
    cx = col * scale + scale // 2
    cy = row * scale + scale // 2
    cv2.circle(img, (cx, cy), 5, (0, 0, 0), -1)
    cv2.circle(img, (cx, cy), 4, color, -1)


def _header(img: np.ndarray, text: str) -> np.ndarray:
    bar = np.full((24, img.shape[1], 3), 40, dtype=np.uint8)
    cv2.putText(bar, text, (5, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.46,
                (210, 210, 210), 1, cv2.LINE_AA)
    return np.vstack([bar, img])


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

RED  = (50,  50,  220)   # BGR
BLUE = (200, 80,  30)    # BGR


def run_test(
    name:          str,
    elev:          np.ndarray,
    threshold_deg: float,
    steep_pts:     list = None,   # (row, col) where mask must == 1
    safe_pts:      list = None,   # (row, col) where mask must == 0
    note:          str  = "",
) -> np.ndarray:
    steep_pts = steep_pts or []
    safe_pts  = safe_pts  or []

    mask  = compute_slope_mask_horn(elev, PIXEL_M, threshold_deg)
    slope = _slope_deg(elev, PIXEL_M)
    mask2d = mask[0]

    # --- assertions ---
    for r, c in steep_pts:
        assert mask2d[r, c] == 1, (
            f"[{name}] ({r},{c}) should be STEEP (mask=1), "
            f"got {mask2d[r,c]};  slope={slope[r,c]:.1f}°"
        )
    for r, c in safe_pts:
        assert mask2d[r, c] == 0, (
            f"[{name}] ({r},{c}) should be SAFE (mask=0), "
            f"got {mask2d[r,c]};  slope={slope[r,c]:.1f}°"
        )

    # --- render panels (grid resolution, then upscale) ---
    p_dem   = _colorise_dem(elev[0])
    p_slope = _colorise_slope(slope)
    p_mask  = _mask_bgr(mask2d)

    for r, c in steep_pts:
        for p in (p_dem, p_slope, p_mask):
            p[r, c] = RED[::-1]   # tiny 1-px seed before upscale

    for r, c in safe_pts:
        for p in (p_dem, p_slope, p_mask):
            p[r, c] = BLUE[::-1]

    p_dem   = cv2.resize(p_dem,   (GRID_N*SCALE, GRID_N*SCALE), interpolation=cv2.INTER_NEAREST)
    p_slope = cv2.resize(p_slope, (GRID_N*SCALE, GRID_N*SCALE), interpolation=cv2.INTER_NEAREST)
    p_mask  = cv2.resize(p_mask,  (GRID_N*SCALE, GRID_N*SCALE), interpolation=cv2.INTER_NEAREST)

    for r, c in steep_pts:
        for p in (p_dem, p_slope, p_mask):
            _draw_marker(p, r, c, RED, SCALE)

    for r, c in safe_pts:
        for p in (p_dem, p_slope, p_mask):
            _draw_marker(p, r, c, BLUE, SCALE)

    steep_pct = int(mask2d.mean() * 100)
    p_dem   = _header(p_dem,   "Elevation  (VIRIDIS)")
    p_slope = _header(p_slope, f"Slope in °  (max {slope.max():.1f}°)  [INFERNO 0–90°]")
    p_mask  = _header(p_mask,  f"Mask  threshold={threshold_deg}°  [{steep_pct}% steep]")

    gap = np.full((p_dem.shape[0], 10, 3), 28, dtype=np.uint8)
    out = np.hstack([p_dem, gap, p_slope, gap, p_mask])

    if note:
        foot = np.full((20, out.shape[1], 3), 22, dtype=np.uint8)
        cv2.putText(foot, note, (5, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.38,
                    (150, 150, 150), 1, cv2.LINE_AA)
        out = np.vstack([out, foot])

    path = os.path.join(OUTPUT_DIR, f"{name}.png")
    cv2.imwrite(path, out)
    print(f"  PASS  {name}  (max slope {slope.max():.1f}°, {steep_pct}% steep)")
    print(f"        → {path}")
    return mask


# ---------------------------------------------------------------------------
# DEM factories
# ---------------------------------------------------------------------------

def _flat() -> np.ndarray:
    return np.zeros((1, GRID_N, GRID_N), dtype=np.float32)


def _ramp(slope_deg: float) -> np.ndarray:
    """Uniform inclined plane in the x-direction."""
    c = np.arange(GRID_N, dtype=np.float32)
    elev = np.outer(np.ones(GRID_N), c) * PIXEL_M * np.tan(np.radians(slope_deg))
    return elev[np.newaxis].astype(np.float32)


def _gaussian_dome(height: float = 20.0, sigma: float = 15.0) -> np.ndarray:
    cx = cy = GRID_N // 2
    y, x = np.ogrid[:GRID_N, :GRID_N]
    elev = height * np.exp(-((x - cx)**2 + (y - cy)**2) / (2 * sigma**2))
    return elev[np.newaxis].astype(np.float32)


def _two_zone(flat_side: str = "left", ramp_deg: float = 35.0) -> np.ndarray:
    """Left half flat, right half uniform ramp (or vice-versa)."""
    elev = np.zeros((1, GRID_N, GRID_N), dtype=np.float32)
    half = GRID_N // 2
    ramp = np.arange(half, dtype=np.float32) * PIXEL_M * np.tan(np.radians(ramp_deg))
    if flat_side == "left":
        elev[0, :, half:] = ramp.reshape(1, -1)
    else:
        elev[0, :, :half] = ramp[::-1].reshape(1, -1)
    return elev


def _cliff(height: float = 10.0) -> np.ndarray:
    """Step function: left half at 0 m, right half at height m."""
    elev = np.zeros((1, GRID_N, GRID_N), dtype=np.float32)
    elev[0, :, GRID_N // 2:] = height
    return elev


def _sinusoidal(amplitude: float = 5.0, periods: float = 3.0) -> np.ndarray:
    """Rolling hills in the x-direction."""
    c = np.arange(GRID_N, dtype=np.float32)
    wave = amplitude * np.sin(2 * np.pi * periods * c / GRID_N)
    return np.tile(wave, (GRID_N, 1))[np.newaxis].astype(np.float32)


# --- Y-direction mirrors ---

def _ramp_y(slope_deg: float) -> np.ndarray:
    """Uniform inclined plane in the y-direction (row-based)."""
    r = np.arange(GRID_N, dtype=np.float32)
    elev = np.outer(r, np.ones(GRID_N)) * PIXEL_M * np.tan(np.radians(slope_deg))
    return elev[np.newaxis].astype(np.float32)


def _two_zone_y(flat_side: str = "top", ramp_deg: float = 35.0) -> np.ndarray:
    """Top half flat, bottom half uniform ramp (or vice-versa)."""
    elev = np.zeros((1, GRID_N, GRID_N), dtype=np.float32)
    half = GRID_N // 2
    ramp = np.arange(half, dtype=np.float32) * PIXEL_M * np.tan(np.radians(ramp_deg))
    if flat_side == "top":
        elev[0, half:, :] = ramp.reshape(-1, 1)
    else:
        elev[0, :half, :] = ramp[::-1].reshape(-1, 1)
    return elev


def _cliff_y(height: float = 10.0) -> np.ndarray:
    """Step function: top half at 0 m, bottom half at height m."""
    elev = np.zeros((1, GRID_N, GRID_N), dtype=np.float32)
    elev[0, GRID_N // 2:, :] = height
    return elev


def _sinusoidal_y(amplitude: float = 5.0, periods: float = 3.0) -> np.ndarray:
    """Rolling hills in the y-direction (row-based)."""
    r = np.arange(GRID_N, dtype=np.float32)
    wave = amplitude * np.sin(2 * np.pi * periods * r / GRID_N)
    return np.tile(wave.reshape(-1, 1), (1, GRID_N))[np.newaxis].astype(np.float32)


def _diagonal_ramp(slope_deg: float) -> np.ndarray:
    """Plane inclined at 45° (NE direction): dx = dy = tan(slope)/√2.
    Combined magnitude → arctan(tan(slope)) = slope_deg, same as x- or y-only ramp."""
    r, c = np.ogrid[:GRID_N, :GRID_N]
    elev = (r + c) / np.sqrt(2) * PIXEL_M * np.tan(np.radians(slope_deg))
    return elev[np.newaxis].astype(np.float32)


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

def test_flat():
    """
    All-zero elevation → slope = 0° everywhere → mask must be entirely 0.
    """
    mask = run_test(
        "1_flat",
        _flat(), threshold_deg=10.0,
        safe_pts=[(50, 50), (0, 0), (99, 99)],
        note="flat terrain: slope panel should be all black, mask panel all black",
    )
    assert mask.max() == 0, "Flat DEM: expected mask all-zero"


def test_ramp_below_threshold():
    """
    Uniform 20° ramp, threshold = 30° → interior slope (20°) < threshold → mask = 0.
    """
    run_test(
        "2_ramp_below_threshold",
        _ramp(slope_deg=20.0), threshold_deg=30.0,
        safe_pts=[(50, 50)],
        note="20° ramp, threshold 30° — slope panel uniform mid-orange, mask all black",
    )


def test_ramp_above_threshold():
    """
    Same 20° ramp, threshold = 10° → interior slope (20°) > threshold → mask = 1.
    """
    run_test(
        "3_ramp_above_threshold",
        _ramp(slope_deg=20.0), threshold_deg=10.0,
        steep_pts=[(50, 50)],
        note="20° ramp, threshold 10° — same DEM as test 2, mask now all white",
    )


def test_gaussian_dome():
    """
    Smooth dome: flat at peak, steep on the flanks.
    Expected mask: hollow circle (0 at top + at base, 1 on the ring of max slope).
    """
    # At (50, 50) peak: slope ≈ 0°  → safe
    # At (50, 65) (~1 sigma): slope ≈ 39° > 25° → steep
    # At (50, 90) (far base): slope ≈ 0°  → safe
    run_test(
        "4_gaussian_dome",
        _gaussian_dome(height=20.0, sigma=15.0), threshold_deg=25.0,
        steep_pts=[(50, 65), (50, 35), (35, 50), (65, 50)],
        safe_pts=[(50, 50), (50, 90), (0, 0)],
        note="dome: mask should show a ring/annulus — flat top and far base are safe",
    )


def test_two_zone():
    """
    Left half flat (0°), right half steep ramp (35°), threshold = 20°.
    Expected: clean left-right split — all black on left, all white on right.
    """
    run_test(
        "5_two_zone",
        _two_zone(flat_side="left", ramp_deg=35.0), threshold_deg=20.0,
        steep_pts=[(50, 75)],
        safe_pts=[(50, 25)],
        note="left: flat → safe (black) | right: 35° ramp → steep (white) | split at col 50",
    )


def test_cliff_edge():
    """
    Step function (cliff): left=0 m, right=10 m.
    Horn sees a large gradient only at the 2 pixels straddling the edge.
    Expected mask: thin white band at the cliff, black everywhere else.
    """
    # Cliff boundary: cols 49-50 are the transition pixels (slope ≈ 79°)
    run_test(
        "6_cliff_edge",
        _cliff(height=10.0), threshold_deg=60.0,
        steep_pts=[(50, 49), (50, 50)],
        safe_pts=[(50, 25), (50, 75)],
        note="cliff: thin white band at col≈49-50 only — flat zones on both sides are safe",
    )


def test_sinusoidal():
    """
    Rolling sinusoidal hills: steep at wave crests/troughs, flat at inflections.
    Expected mask: alternating bands of white (steep) and black (flat).
    """
    run_test(
        "7_sinusoidal",
        _sinusoidal(amplitude=5.0, periods=3.0), threshold_deg=15.0,
        note="3 full waves: steep bands near each crest/trough, flat bands at inflection points",
    )


# ---------------------------------------------------------------------------
# Y-direction mirrors  (exercise kernel_y independently)
# ---------------------------------------------------------------------------

def test_ramp_y_below_threshold():
    """
    Uniform 20° ramp in the Y direction, threshold = 30° → mask = 0.
    Mirror of test 2: kernel_y should give the same slope as kernel_x did.
    """
    run_test(
        "9_ramp_y_below_threshold",
        _ramp_y(slope_deg=20.0), threshold_deg=30.0,
        safe_pts=[(50, 50)],
        note="Y-ramp 20°, threshold 30° — slope panel should look like test 2 rotated 90°",
    )


def test_ramp_y_above_threshold():
    """
    Same Y ramp, threshold = 10° → mask = 1.  Mirror of test 3.
    """
    run_test(
        "10_ramp_y_above_threshold",
        _ramp_y(slope_deg=20.0), threshold_deg=10.0,
        steep_pts=[(50, 50)],
        note="Y-ramp 20°, threshold 10° — mask all white, same as test 3",
    )


def test_two_zone_y():
    """
    Top half flat (0°), bottom half steep Y-ramp (35°), threshold = 20°.
    Expected: clean top-black / bottom-white split. Mirror of test 5.
    """
    run_test(
        "11_two_zone_y",
        _two_zone_y(flat_side="top", ramp_deg=35.0), threshold_deg=20.0,
        steep_pts=[(75, 50)],
        safe_pts=[(25, 50)],
        note="top: flat → safe | bottom: 35° Y-ramp → steep | split at row 50",
    )


def test_cliff_edge_y():
    """
    Horizontal step (cliff): top=0 m, bottom=10 m.
    Expected: thin white band at rows 49-50, black everywhere else. Mirror of test 6.
    """
    run_test(
        "12_cliff_edge_y",
        _cliff_y(height=10.0), threshold_deg=60.0,
        steep_pts=[(49, 50), (50, 50)],
        safe_pts=[(25, 50), (75, 50)],
        note="horizontal cliff: thin white band at row≈49-50 — same geometry as test 6, rotated 90°",
    )


def test_sinusoidal_y():
    """
    Rolling hills in the Y direction.
    Expected: alternating horizontal bands (vs. vertical bands in test 7).
    """
    run_test(
        "13_sinusoidal_y",
        _sinusoidal_y(amplitude=5.0, periods=3.0), threshold_deg=15.0,
        note="Y-sinusoidal: horizontal steep/flat bands — rotate test 7 image 90° and they should match",
    )


def test_diagonal_ramp():
    """
    Plane inclined at 45° (NE diagonal): dx = dy = tan(20°)/√2.
    Both kernels contribute equally. Combined magnitude → arctan(tan(20°)) = 20°,
    identical to the pure x- or y-ramp at the same angle.
    """
    run_test(
        "14_diagonal_ramp_below_threshold",
        _diagonal_ramp(slope_deg=20.0), threshold_deg=30.0,
        safe_pts=[(50, 50)],
        note="diagonal 45° ramp 20°, threshold 30° — slope = 20° despite dx=dy=tan/√2",
    )
    run_test(
        "15_diagonal_ramp_above_threshold",
        _diagonal_ramp(slope_deg=20.0), threshold_deg=10.0,
        steep_pts=[(50, 50)],
        note="same diagonal ramp, threshold 10° — mask all white, same as x- and y-ramp tests",
    )


def test_threshold_90_guard():
    """
    threshold=90° must always return all-zero (guard in the function).
    Uses the steep Gaussian dome to ensure the guard is doing the work,
    not just absence of steep pixels.
    """
    mask = run_test(
        "8_threshold_90_guard",
        _gaussian_dome(height=20.0, sigma=15.0), threshold_deg=90.0,
        safe_pts=[(50, 65)],   # would be steep at any lower threshold
        note="threshold=90°: function guard returns zeros regardless of terrain",
    )
    assert mask.max() == 0, "threshold=90°: expected mask all-zero (guard)"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"\nWriting PNGs to: {OUTPUT_DIR}\n")
    print("--- X-direction ---")
    test_flat()
    test_ramp_below_threshold()
    test_ramp_above_threshold()
    test_gaussian_dome()
    test_two_zone()
    test_cliff_edge()
    test_sinusoidal()
    test_threshold_90_guard()
    print("\n--- Y-direction mirrors ---")
    test_ramp_y_below_threshold()
    test_ramp_y_above_threshold()
    test_two_zone_y()
    test_cliff_edge_y()
    test_sinusoidal_y()
    print("\n--- Diagonal (both kernels) ---")
    test_diagonal_ramp()
    print("\nAll visual tests passed.\n")

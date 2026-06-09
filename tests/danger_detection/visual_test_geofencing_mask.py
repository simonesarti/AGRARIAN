"""
Visual test for create_geofencing_mask_runtime.

Each test case renders a colour-coded PNG to
    tests/danger_detection/output/visual_geofencing_mask/

  GREEN  : mask == 0  → inside polygon (safe / OK zone)
  RED    : mask == 1  → outside polygon (OUT OF BOUNDS)
  YELLOW : expected polygon boundary projected into pixel space
  CYAN + : polygon centroid in pixel space
  WHITE* : sample pixels expected to be INSIDE  (assertion checked)
  MAGENTA×: sample pixels expected to be OUTSIDE (assertion checked)

Run:
    python tests/danger_detection/visual_test_geofencing_mask.py
"""

import math
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from src.danger_detection.utils import create_geofencing_mask_runtime, get_frame_transform

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output", "visual_geofencing_mask")
os.makedirs(OUTPUT_DIR, exist_ok=True)

FRAME_W, FRAME_H = 640, 480


# ---------------------------------------------------------------------------
# Polygon helpers  (no shapely dependency — plain GeoJSON dicts)
# ---------------------------------------------------------------------------

def make_polygon(ring_coords):
    """Create a GeoJSON-like polygon dict from a list of (lon, lat) tuples."""
    coords = list(ring_coords)
    if coords[0] != coords[-1]:
        coords.append(coords[0])   # close the ring
    return {"type": "Polygon", "coordinates": [coords]}


def poly_exterior(polygon):
    """Return the exterior ring coords (without the closing duplicate)."""
    coords = polygon["coordinates"][0]
    return coords[:-1] if coords[0] == coords[-1] else coords


def poly_centroid(polygon):
    """Arithmetic centroid of the exterior ring."""
    pts = poly_exterior(polygon)
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    return sum(lons) / len(lons), sum(lats) / len(lats)


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def lonlat_to_pixel(lon, lat, transform):
    """Inverse-project a geographic point to (col, row) pixel coords."""
    col, row = ~transform * (lon, lat)
    return int(round(col)), int(round(row))


def mask_to_bgr(mask):
    """Binary mask → colour image  (BGR for OpenCV)."""
    bgr = np.zeros((mask.shape[0], mask.shape[1], 3), dtype=np.uint8)
    bgr[mask == 0] = (0, 180, 0)   # green  – inside polygon (OK)
    bgr[mask == 1] = (0, 0, 200)   # red    – outside polygon (DANGER)
    return bgr


def overlay_polygon(img, polygon, transform):
    """Draw polygon exterior + centroid marker on img (in-place, BGR)."""
    pts = [lonlat_to_pixel(lon, lat, transform) for lon, lat in poly_exterior(polygon)]
    arr = np.array(pts, dtype=np.int32).reshape((-1, 1, 2))
    cv2.polylines(img, [arr], isClosed=True, color=(0, 230, 230), thickness=2)  # yellow
    cx, cy = lonlat_to_pixel(*poly_centroid(polygon), transform)
    cv2.drawMarker(img, (cx, cy), (255, 255, 0), cv2.MARKER_CROSS, 20, 2)       # cyan


def add_legend(img, title):
    font, scale, lh = cv2.FONT_HERSHEY_SIMPLEX, 0.42, 17
    cv2.putText(img, title, (8, 18), font, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
    legends = [
        ("GREEN  = inside polygon (OK)",         (0, 200, 0)),
        ("RED    = outside polygon (DANGER)",     (0, 0, 200)),
        ("YELLOW = polygon boundary",             (0, 230, 230)),
        ("WHITE * = expected-inside sample",      (220, 220, 220)),
        ("MAGENTA x = expected-outside sample",   (200, 0, 200)),
    ]
    for i, (text, color) in enumerate(legends):
        cv2.putText(img, text, (8, 36 + i * lh), font, scale, color, 1, cv2.LINE_AA)


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

def run_test(name, transform, polygon,
             expected_inside_px=(), expected_outside_px=()):
    mask = create_geofencing_mask_runtime(FRAME_W, FRAME_H, transform, polygon)
    img = mask_to_bgr(mask)
    overlay_polygon(img, polygon, transform)

    for col, row in expected_inside_px:
        cv2.drawMarker(img, (col, row), (220, 220, 220), cv2.MARKER_STAR, 18, 2)
        assert mask[row, col] == 0, (
            f"[{name}] pixel ({col},{row}) should be INSIDE polygon (mask=0), "
            f"got {mask[row, col]}"
        )

    for col, row in expected_outside_px:
        cv2.drawMarker(img, (col, row), (200, 0, 200), cv2.MARKER_TILTED_CROSS, 18, 2)
        assert mask[row, col] == 1, (
            f"[{name}] pixel ({col},{row}) should be OUTSIDE polygon (mask=1), "
            f"got {mask[row, col]}"
        )

    add_legend(img, name)
    path = os.path.join(OUTPUT_DIR, f"{name}.png")
    cv2.imwrite(path, img)
    print(f"  PASS  {name}")
    print(f"        → {path}")
    return mask


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

def test_centered_polygon():
    """
    Axis-aligned frame.  Polygon covers the central ~40 % of the frame.
    Expected: green rectangle centred on the image, red border around it.
    """
    ul = (10.000, 48.010)   # top-left  (min lon, max lat)
    ur = (10.020, 48.010)   # top-right (max lon, max lat)
    bl = (10.000, 47.990)   # bot-left  (min lon, min lat)
    t = get_frame_transform(FRAME_H, FRAME_W, ul, ur, bl)

    # Polygon spans roughly cols 192-448 (lon 10.006..10.014)
    #                         rows 144-336 (lat 48.004..47.996)
    poly = make_polygon([
        (10.006, 48.004),
        (10.014, 48.004),
        (10.014, 47.996),
        (10.006, 47.996),
    ])
    run_test(
        "1_centered_polygon",
        t, poly,
        expected_inside_px=[(320, 240)],            # exact frame centre
        expected_outside_px=[(5, 5), (635, 475)],   # corners
    )


def test_top_left_polygon():
    """
    Axis-aligned frame.  Polygon confined to the top-left quadrant.
    Expected: green patch in the top-left, rest red.
    """
    ul = (10.000, 48.010)
    ur = (10.020, 48.010)
    bl = (10.000, 47.990)
    t = get_frame_transform(FRAME_H, FRAME_W, ul, ur, bl)

    poly = make_polygon([
        (10.000, 48.010),   # UL corner of frame
        (10.008, 48.010),
        (10.008, 48.004),
        (10.000, 48.004),
    ])
    run_test(
        "2_top_left_polygon",
        t, poly,
        expected_inside_px=[(40, 40)],                # inside top-left patch
        expected_outside_px=[(500, 400), (320, 240)], # bottom-right & centre
    )


def test_bottom_right_polygon():
    """
    Axis-aligned frame.  Polygon confined to the bottom-right quadrant.
    Expected: green patch in the bottom-right, rest red.
    """
    ul = (10.000, 48.010)
    ur = (10.020, 48.010)
    bl = (10.000, 47.990)
    t = get_frame_transform(FRAME_H, FRAME_W, ul, ur, bl)

    poly = make_polygon([
        (10.012, 48.002),
        (10.020, 48.002),
        (10.020, 47.990),   # BR corner of frame
        (10.012, 47.990),
    ])
    run_test(
        "3_bottom_right_polygon",
        t, poly,
        expected_inside_px=[(600, 440)],              # inside bottom-right patch
        expected_outside_px=[(40, 40), (320, 240)],   # top-left & centre
    )


def test_rotated_frame_centered_polygon():
    """
    Frame rotated ~30 degrees (drone heading northeast).  Centered polygon.
    Expected: green shape in the middle of the image despite the rotation.

    Corner derivation
    -----------------
    Start with axis-aligned half-extents hw=0.01 lon-deg, hh=0.008 lat-deg
    around centre (cx=10.01, cy=48.00), then rotate by 30° around the centre.
    """
    cx, cy = 10.010, 48.000
    hw, hh = 0.010, 0.008
    theta = math.radians(30)
    cos_t, sin_t = math.cos(theta), math.sin(theta)

    def rot(dx, dy):
        return (cx + dx * cos_t - dy * sin_t,
                cy + dx * sin_t + dy * cos_t)

    ul = rot(-hw, +hh)   # (col=0,   row=0)
    ur = rot(+hw, +hh)   # (col=W-1, row=0)
    bl = rot(-hw, -hh)   # (col=0,   row=H-1)

    t = get_frame_transform(FRAME_H, FRAME_W, ul, ur, bl)

    d = 0.003
    poly = make_polygon([
        (cx - d, cy + d),   # 10.013, 48.003
        (cx + d, cy + d),   # 10.017, 48.003
        (cx + d, cy - d),   # 10.017, 47.997
        (cx - d, cy - d),   # 10.013, 47.997
    ])
    run_test(
        "4_rotated_frame_centered_polygon",
        t, poly,
        expected_inside_px=[(320, 240)],  # frame centre must be inside polygon
    )


def test_full_frame_polygon():
    """
    Polygon larger than the entire frame → mask must be all zeros (all safe).
    """
    ul = (10.000, 48.010)
    ur = (10.020, 48.010)
    bl = (10.000, 47.990)
    t = get_frame_transform(FRAME_H, FRAME_W, ul, ur, bl)

    poly = make_polygon([
        (9.990, 48.020),
        (10.030, 48.020),
        (10.030, 47.980),
        (9.990, 47.980),
    ])
    mask = run_test("5_full_frame_polygon", t, poly)
    assert mask.max() == 0, "Full-frame polygon: expected all pixels inside (mask=0)"
    print("        mask all 0 (all inside) ✓")


def test_polygon_outside_frame():
    """
    Polygon entirely outside the frame → mask must be all ones (all danger).
    """
    ul = (10.000, 48.010)
    ur = (10.020, 48.010)
    bl = (10.000, 47.990)
    t = get_frame_transform(FRAME_H, FRAME_W, ul, ur, bl)

    poly = make_polygon([
        (11.000, 49.000),
        (11.010, 49.000),
        (11.010, 48.990),
        (11.000, 48.990),
    ])
    mask = run_test("6_polygon_outside_frame", t, poly)
    assert mask.min() == 1, "Off-frame polygon: expected all pixels outside (mask=1)"
    print("        mask all 1 (all outside) ✓")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"\nWriting PNGs to: {OUTPUT_DIR}\n")
    test_centered_polygon()
    test_top_left_polygon()
    test_bottom_right_polygon()
    test_rotated_frame_centered_polygon()
    test_full_frame_polygon()
    test_polygon_outside_frame()
    print("\nAll visual tests passed.\n")

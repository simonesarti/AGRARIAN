import cv2
import numpy as np
import pytest

from app.danger_detection.output.frames import (
    RED,
    GREEN,
    BLUE,
    YELLOW,
    WHITE,
    BLACK,
    PURPLE,
    CLASS_COLOR,
    get_danger_intersect_colored_frames,
    draw_safety_areas,
    draw_dangerous_area,
    draw_detections,
    draw_count,
)


# ------------------------------
# Tests for get_danger_intersect_colored_frames
# ------------------------------
def test_get_danger_intersect_colored_frames():
    shape = (10, 10, 3)
    danger_frame, intersect_frame = get_danger_intersect_colored_frames(shape)
    # danger_frame should be filled with RED and intersect_frame with YELLOW.
    expected_danger = np.full(shape, RED, dtype=np.uint8)
    expected_intersect = np.full(shape, YELLOW, dtype=np.uint8)
    np.testing.assert_array_equal(danger_frame, expected_danger)
    np.testing.assert_array_equal(intersect_frame, expected_intersect)


# ------------------------------
# Tests for draw_safety_areas
# ------------------------------
def test_draw_safety_areas_nonempty(monkeypatch):
    """Test that draw_safety_areas calls cv2.circle for each center in boxes_centers."""
    # Create a dummy annotated frame.
    annotated_frame = np.zeros((50, 50, 3), dtype=np.uint8)
    # Define two centers as a (2,2) array.
    boxes_centers = np.array([
        [10, 10],
        [20, 20]]
    )
    safety_radius = 5

    calls = []

    def fake_circle(img, center, radius, color, thickness):
        # Record the call parameters.
        # Convert center to a tuple in case it is a NumPy array.
        calls.append((tuple(center), radius, color, thickness))

    monkeypatch.setattr(cv2, "circle", fake_circle)

    draw_safety_areas(annotated_frame, boxes_centers, safety_radius)

    expected_calls = [
        ((10, 10), safety_radius, GREEN, 2),
        ((20, 20), safety_radius, GREEN, 2)
    ]
    assert calls == expected_calls


def test_draw_safety_areas_empty(monkeypatch):
    """Test that when boxes_centers is empty, no circles are drawn."""
    annotated_frame = np.zeros((50, 50, 3), dtype=np.uint8)
    boxes_centers = np.empty((0, 2), dtype=int)
    safety_radius = 5

    calls = []

    def fake_circle(img, center, radius, color, thickness):
        calls.append((tuple(center), radius, color, thickness))

    monkeypatch.setattr(cv2, "circle", fake_circle)

    draw_safety_areas(annotated_frame, boxes_centers, safety_radius)
    assert len(calls) == 0


# ------------------------------
# Tests for draw_detections
# ------------------------------
def test_draw_detections_nonempty(monkeypatch):
    """
    Test that draw_detections calls cv2.rectangle for each detection with the correct parameters.
    The CLASS_COLOR list is used to determine the rectangle color based on the class.
    """
    annotated_frame = np.zeros((30, 30, 3), dtype=np.uint8)
    # Create consistent (N,2) arrays for the corners and a (N,) array for classes.
    classes = np.array([0, 1], dtype=int)
    boxes_corner1 = np.array([[5, 5], [15, 15]], dtype=int)
    boxes_corner2 = np.array([[10, 10], [20, 20]], dtype=int)

    calls = []

    def fake_rectangle(img, pt1, pt2, color, thickness):
        # Record the call parameters.
        calls.append((tuple(pt1), tuple(pt2), color, thickness))

    monkeypatch.setattr(cv2, "rectangle", fake_rectangle)

    draw_detections(annotated_frame, classes, boxes_corner1, boxes_corner2)

    expected_calls = [
        ((5, 5), (10, 10), CLASS_COLOR[0], 2),
        ((15, 15), (20, 20), CLASS_COLOR[1], 2)
    ]
    assert calls == expected_calls


def test_draw_detections_empty(monkeypatch):
    """
    Test that when the detection arrays are empty, draw_detections does not attempt to draw any rectangles.
    """
    annotated_frame = np.zeros((30, 30, 3), dtype=np.uint8)
    classes = np.array([], dtype=int)
    boxes_corner1 = np.empty((0, 2), dtype=int)
    boxes_corner2 = np.empty((0, 2), dtype=int)

    calls = []

    def fake_rectangle(img, pt1, pt2, color, thickness):
        calls.append((pt1, pt2, color, thickness))

    monkeypatch.setattr(cv2, "rectangle", fake_rectangle)

    draw_detections(annotated_frame, classes, boxes_corner1, boxes_corner2)
    assert len(calls) == 0


# ------------------------------
# Tests for draw_dangerous_area
# ------------------------------
def test_draw_dangerous_area():
    """
    Test draw_dangerous_area by computing the expected overlay.
    The function uses bitwise operations and addWeighted to blend overlays.
    """
    # Create a small annotated frame.
    frame_shape = (5, 5, 3)
    annotated_frame = np.full(frame_shape, 100, dtype=np.uint8)

    # Create binary masks (5x5) for danger and intersection.
    dangerous_mask_no_intersection = np.zeros((5, 5), dtype=np.uint8)
    intersection = np.zeros((5, 5), dtype=np.uint8)

    # Mark one pixel in each mask.
    dangerous_mask_no_intersection[2, 2] = 255
    intersection[1, 1] = 255

    color_danger_frame, color_intersect_frame = get_danger_intersect_colored_frames(frame_shape)

    # Make a copy of the annotated frame to compute the expected result.
    annotated_frame_copy = annotated_frame.copy()

    # The function computes overlays as follows:
    danger_overlay = cv2.bitwise_and(color_danger_frame, color_danger_frame, mask=dangerous_mask_no_intersection)
    intersect_overlay = cv2.bitwise_and(color_intersect_frame, color_intersect_frame, mask=intersection)
    overlay = cv2.add(danger_overlay, intersect_overlay)
    expected_frame = cv2.addWeighted(annotated_frame_copy, 0.75, overlay, 0.25, 0)

    # Call the function (it modifies annotated_frame in-place).
    draw_dangerous_area(
        annotated_frame,
        dangerous_mask_no_intersection,
        intersection,
        color_danger_frame,
        color_intersect_frame
    )

    np.testing.assert_array_equal(annotated_frame, expected_frame)


# --- Helper: Patch cv2.getTextSize to return a fixed size ---
# For consistency in tests, let each text line have width=100 and height=20.
def dummy_get_text_size(text, fontFace, fontScale, thickness):
    return ((100, 20), 0)


@pytest.fixture
def patch_get_text_size(monkeypatch):
    monkeypatch.setattr(cv2, "getTextSize", dummy_get_text_size)


# --- Helper: Capture calls to cv2.rectangle ---
@pytest.fixture
def capture_rectangle(monkeypatch):
    calls = []

    def fake_rectangle(img, pt1, pt2, color, thickness):
        calls.append((pt1, pt2, color, thickness))

    monkeypatch.setattr(cv2, "rectangle", fake_rectangle)
    return calls


# --- Helper: Capture calls to cv2.putText ---
@pytest.fixture
def capture_put_text(monkeypatch):
    calls = []

    def fake_put_text(img, text, org, fontFace, fontScale, color, thickness, lineType):
        calls.append((text, org, fontFace, fontScale, color, thickness, lineType))

    monkeypatch.setattr(cv2, "putText", fake_put_text)
    return calls


# --- Tests ---

def test_draw_count_nonempty(patch_get_text_size, capture_rectangle, capture_put_text):
    """
    Test that draw_count writes num_classes lines at the bottom left,
    that the class names and counts are correct when classes is non-empty.
    """
    frame_height = 200
    # Create a dummy frame (the drawing is in-place).
    annotated_frame = np.zeros((frame_height, 300, 3), dtype=np.uint8)
    # Non-empty classes: e.g. 2 detections of class 0 and 3 detections of class 1.
    classes = np.array([0, 1, 1, 0, 1], dtype=int)
    num_classes = 2
    classes_names = {
        0: "Sheep",
        1: "Goat",
    }

    # Call the function.
    draw_count(classes, num_classes, classes_names, annotated_frame)

    # The function sets the origin at (10, frame_height - 10) = (10, 190)
    org = (10, frame_height - 10)  # (10, 190)
    # With our dummy getTextSize, each line returns size (100,20)
    # Total height = (20 + 5)*num_lines = 25 * 2 = 50.
    total_height = 50
    # The rectangle is drawn from:
    # textbox_coord_ul = (org[0]-5, org[1]-total_height-5) = (10-5, 190-50-5) = (5, 135)
    # textbox_coord_br = (org[0]+100+5, org[1]+5) = (10+100+5, 190+5) = (115, 195)
    expected_rect_ul = (5, 135)
    expected_rect_br = (115, 195)
    # Verify that cv2.rectangle was called once with the correct coordinates.
    assert len(capture_rectangle) == 1
    rect_call = capture_rectangle[0]
    assert rect_call[0] == expected_rect_ul
    assert rect_call[1] == expected_rect_br

    # The y_offset for the first line is:
    # y_offset = org[1] - total_height + line_height = 190 - 50 + 20 = 160,
    # and for the second line: 160 + (20+5) = 185.
    expected_orgs = [(10, 160), (10, 185)]
    # Expected texts based on np.bincount: class 0 count = 2, class 1 count = 3.
    expected_texts = ["N. Sheep: 2", "N. Goat: 3"]

    # Verify that cv2.putText was called exactly 2 times with correct parameters.
    assert len(capture_put_text) == num_classes
    for i, call in enumerate(capture_put_text):
        text, org_text, fontFace, fontScale, color, thickness, lineType = call
        assert text == expected_texts[i]
        assert org_text == expected_orgs[i]
        # Also verify that the text uses the computed base_font_scale and thickness.
        # For frame_height=200, base_font_scale = 0.001 * 200 = 0.2 and base_thickness = max(1, int(0.002*200)) = 1.
        assert fontScale == 0.2
        assert thickness == 1
        # Check that text color is BLACK.
        assert color == (0, 0, 0)


def test_draw_count_empty(patch_get_text_size, capture_rectangle, capture_put_text):
    """
    Test that draw_count still draws the text lines (with counts of 0)
    when the classes array is empty.
    """
    frame_height = 200
    annotated_frame = np.zeros((frame_height, 300, 3), dtype=np.uint8)
    classes = np.array([], dtype=int)
    num_classes = 2
    classes_names = {
        0: "Sheep",
        1: "Goat",
    }

    draw_count(classes, num_classes, classes_names, annotated_frame)

    # With an empty classes array, np.bincount(classes) is empty,
    # so class_counts remains zeros. Expected lines:
    # "N. Sheep: 0" and "N. Goat: 0".
    expected_texts = ["N. Sheep: 0", "N. Goat: 0"]

    # The rectangle coordinates and text origins will be the same as in the non-empty case.
    org = (10, frame_height - 10)  # (10, 190)
    total_height = (20 + 5) * num_classes  # 50
    expected_rect_ul = (org[0] - 5, org[1] - total_height - 5)  # (5, 135)
    expected_rect_br = (org[0] + 100 + 5, org[1] + 5)  # (115, 195)
    expected_orgs = [(10, 160), (10, 185)]

    # Check rectangle call.
    assert len(capture_rectangle) == 1
    rect_call = capture_rectangle[0]
    assert rect_call[0] == expected_rect_ul
    assert rect_call[1] == expected_rect_br

    # Check putText calls.
    assert len(capture_put_text) == num_classes
    for i, call in enumerate(capture_put_text):
        text, org_text, fontFace, fontScale, color, thickness, lineType = call
        assert text == expected_texts[i]
        assert org_text == expected_orgs[i]
        assert fontScale == 0.2
        assert thickness == 1
        assert color == (0, 0, 0)

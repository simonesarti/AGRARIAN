import numpy as np
import torch

from app.danger_detection.detection.detection import postprocess_detection_results


# --- Dummy Classes to Mimic YOLO's Output Structure ---

class DummyTensor:
    def __init__(self, data):
        self.data = torch.tensor(data)

    def cpu(self):
        return self.data.cpu()

    def numpy(self):
        return self.data.numpy()


class DummyBoxes:
    def __init__(self, cls, xywh, xyxy):
        self.cls = DummyTensor(cls)
        self.xywh = DummyTensor(xywh)
        self.xyxy = DummyTensor(xyxy)


class DummyDetection:
    def __init__(self, boxes):
        # boxes can be a DummyBoxes instance or None
        self.boxes = boxes


# --- Tests ---

def test_postprocess_detection_results_nonempty():
    """
    Test postprocess_detection_results with valid (non-empty) detection results.
    """
    # Expected dummy data
    expected_classes = [1.0, 2.0]
    expected_xywh = [[50.0, 60.0, 100.0, 120.0], [150.0, 160.0, 200.0, 220.0]]
    expected_xyxy = [[10.0, 20.0, 70.0, 80.0], [110.0, 120.0, 170.0, 180.0]]

    # Create dummy boxes with expected values.
    boxes = DummyBoxes(expected_classes, expected_xywh, expected_xyxy)
    detection = DummyDetection(boxes)
    detection_results = [detection]

    # Expected postprocessed outputs
    expected_boxes_centers = np.array(expected_xywh, dtype=int)[:, :2]
    expected_boxes_corner1 = np.array(expected_xyxy, dtype=int)[:, :2]
    expected_boxes_corner2 = np.array(expected_xyxy, dtype=int)[:, 2:]

    # Call the function
    classes, boxes_centers, boxes_corner1, boxes_corner2 = postprocess_detection_results(detection_results)

    # Validate the outputs
    np.testing.assert_array_equal(classes, np.array(expected_classes, dtype=int))
    np.testing.assert_array_equal(boxes_centers, np.array(expected_boxes_centers, dtype=int))
    np.testing.assert_array_equal(boxes_corner1, np.array(expected_boxes_corner1, dtype=int))
    np.testing.assert_array_equal(boxes_corner2, np.array(expected_boxes_corner2, dtype=int))


def test_postprocess_detection_results_empty():
    """
    Test postprocess_detection_results when no boxes are detected (boxes is None).
    """
    detection = DummyDetection(boxes=None)
    detection_results = [detection]

    classes, boxes_centers, boxes_corner1, boxes_corner2 = postprocess_detection_results(detection_results)

    # Expect all returned arrays to be empty.
    np.testing.assert_array_equal(classes, np.array([], dtype=int))
    np.testing.assert_array_equal(boxes_centers, np.array([], dtype=int))
    np.testing.assert_array_equal(boxes_corner1, np.array([], dtype=int))
    np.testing.assert_array_equal(boxes_corner2, np.array([], dtype=int))

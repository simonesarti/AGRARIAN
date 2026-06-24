import cv2
import numpy as np
import pytest
import torch

from app.danger_detection.segmentation.segmentation import postprocess_segmentation_results


# --- Dummy Classes to Mimic YOLO Segmentation Output ---

class DummyTensor:
    def __init__(self, array):
        self.data = torch.tensor(array)

    def cpu(self):
        return self.data.cpu()

    def numpy(self):
        return self.data.numpy()

    def int(self):
        return self.data.type(torch.int)


class DummyMask:
    def __init__(self, data):
        # here data is expected to be a numpy array (e.g., shape: (num_masks, mask_h, mask_w))
        self.data = DummyTensor(data)


class DummySegmentationResult:
    def __init__(self, masks):
        # masks can be a DummyMask instance or None
        self.masks = masks


# --- Test Cases ---

def test_postprocess_segmentation_results_with_masks():
    """
    Test the case where segmentation results contain valid masks.
    The dummy mask has a smaller spatial extent than the frame.
    """
    # Create dummy mask data with 2 masks, each of size (10, 10)
    mask_data = np.zeros((3, 10, 10), dtype=int)
    # Set a block of ones in the first mask and another block in the second mask
    mask_data[0, 2:5, 2:5] = 1
    mask_data[1, 6:9, 6:9] = 1

    dummy_mask = DummyMask(mask_data)
    dummy_seg_result = DummySegmentationResult(dummy_mask)
    segment_results = [dummy_seg_result]

    # Create a dummy frame with larger size (height, width, channels)
    frame_height, frame_width = 20, 20

    # Manually compute the expected merged mask:
    # Step 1: Merge the masks using np.any along axis=0, then convert to uint8.
    merged_mask = np.any(mask_data, axis=0).astype(np.uint8)
    # Step 2: Resize the merged mask to the frame dimensions using nearest-neighbor interpolation.
    expected_mask = cv2.resize(merged_mask, dsize=(frame_width, frame_height), interpolation=cv2.INTER_NEAREST)

    # Call the function under test.
    result_mask = postprocess_segmentation_results(segment_results, frame_height, frame_width)

    # Check that the output mask has the correct shape and values.
    assert result_mask.shape == (frame_height, frame_width)
    np.testing.assert_array_equal(result_mask, expected_mask)


def test_postprocess_segmentation_results_without_masks():
    """
    Test the case where segmentation results do not contain any masks (masks is None).
    The function should return a zeros mask with the same height and width as the frame.
    """
    dummy_seg_result = DummySegmentationResult(masks=None)
    segment_results = [dummy_seg_result]

    # Define a frame of arbitrary size.
    frame_height, frame_width = 30, 40
    expected_mask = np.zeros((frame_height, frame_width), dtype=np.uint8)

    result_mask = postprocess_segmentation_results(segment_results, frame_height, frame_width)

    assert result_mask.shape == (frame_height, frame_width)
    np.testing.assert_array_equal(result_mask, expected_mask)





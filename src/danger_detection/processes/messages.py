import numpy as np
from dataclasses import dataclass


@dataclass
class DetectionSlotMetadata:
    """
    Lightweight message carrying a shared-memory frame slot reference and detection results.
    Passed between DetectionWorker and SegmentationWorker via the metadata queue.
    The frame lives in a FrameBuffer slot; detection box arrays are small enough to carry directly.
    """
    frame_id: int
    timestamp: float
    original_wh: tuple[int, int]
    slot_index: int
    telemetry: dict | None
    classes_names: dict           # {class_id: class_name} — fixed for the model lifetime
    num_classes: int
    classes: np.ndarray           # (N,) class IDs for each detection
    boxes_centers: np.ndarray     # (N, 2) center coordinates
    boxes_corner1: np.ndarray     # (N, 2) top-left corner coordinates
    boxes_corner2: np.ndarray     # (N, 2) bottom-right corner coordinates


@dataclass
class SegmentationSlotMetadata:
    """
    Lightweight message carrying a shared-memory frame slot reference, detection results,
    and segmentation masks. Passed between SegmentationWorker and the next pipeline stage.

    The slot points to a (H, W, 5) array in shared memory:
        channels 0-2 : BGR frame (unchanged from detection)
        channel  3   : roads_mask  (uint8, values 0/1)
        channel  4   : vehicles_mask (uint8, values 0/1)

    Detection box arrays are small and travel directly in this message.
    """
    frame_id: int
    timestamp: float
    original_wh: tuple[int, int]
    slot_index: int
    telemetry: dict | None
    classes_names: dict        # {class_id: class_name} — fixed per model
    num_classes: int
    classes: np.ndarray        # (N,) class IDs
    boxes_centers: np.ndarray  # (N, 2)
    boxes_corner1: np.ndarray  # (N, 2)
    boxes_corner2: np.ndarray  # (N, 2)


@dataclass
class GeoSlotMetadata:
    """
    Lightweight message carrying a shared-memory frame slot reference, detection results,
    segmentation masks, and geo-analysis masks. Passed between GeoWorker and the next
    pipeline stage (danger annotator).

    The slot points to a (H, W, 8) array in shared memory:
        channels 0-2 : BGR frame (unchanged from detection)
        channel  3   : roads_mask      (uint8, 0/1)
        channel  4   : vehicles_mask   (uint8, 0/1)
        channel  5   : nodata_dem_mask (uint8, 0/1)
        channel  6   : geofencing_mask (uint8, 0/1)
        channel  7   : slope_mask      (uint8, 0/1)

    Detection box arrays and safety radius are small and travel directly in this message.
    """
    frame_id: int
    timestamp: float
    original_wh: tuple[int, int]
    slot_index: int
    telemetry: dict | None
    classes_names: dict        # {class_id: class_name} — fixed per model
    num_classes: int
    classes: np.ndarray        # (N,) class IDs
    boxes_centers: np.ndarray  # (N, 2)
    boxes_corner1: np.ndarray  # (N, 2)
    boxes_corner2: np.ndarray  # (N, 2)
    safety_radius_pixels: int

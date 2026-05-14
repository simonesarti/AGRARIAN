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


@dataclass
class DetectionResult:
    """
    A dataclass to store the results of an object detection task.

    Attributes:
        frame_id (int): The unique identifier of the frame where detection occurred.
        frame (np.ndarray): The image data of the frame.
        classes_names (list[str]): A list of the names of the detected classes.
        num_classes (int): The number of unique classes detected.
        classes (np.ndarray): An array of class IDs for each detected object.
        boxes_centers (np.ndarray): An array of the center coordinates of each bounding box.
        boxes_corner1 (np.ndarray): An array of the top-left corner coordinates of each bounding box.
        boxes_corner2 (np.ndarray): An array of the bottom-right corner coordinates of each bounding box.
        timestamp (float): The timestamp of reception (of the frame).
        original_wh tuple(int, int): The original shape of the image
    """
    frame_id: int
    frame: np.ndarray
    classes_names: list[str]
    num_classes: int
    classes: np.ndarray
    boxes_centers: np.ndarray
    boxes_corner1: np.ndarray
    boxes_corner2: np.ndarray
    timestamp: float
    original_wh: tuple[int, int]


@dataclass
class SegmentationResult:
    """
    A dataclass to store the results of a segmentation task.

    Attributes:
        frame_id (int): The unique identifier of the frame.
        mask (np.ndarray): The segmentation mask as a NumPy array.
    """
    frame_id: int
    roads_mask: np.ndarray
    vehicles_mask: np.ndarray


@dataclass
class GeoResult:
    """
    A dataclass to store geographical and geometric analysis results.

    Attributes:
        frame_id (int): The unique identifier of the frame.
        safety_radius_pixels (int): The defined safety radius in pixels.
        nodata_dem_mask (np.ndarray): A mask for areas with no digital elevation model data.
        geofencing_mask (np.ndarray): A mask for geofenced areas.
        slope_mask (np.ndarray): A mask representing the slope analysis of the terrain.
    """
    frame_id: int
    safety_radius_pixels: int
    nodata_dem_mask: np.ndarray
    geofencing_mask: np.ndarray
    slope_mask: np.ndarray


@dataclass
class ModelsAlignmentResult:
    detection_result: DetectionResult
    segmentation_result: SegmentationResult
    geo_result: GeoResult


@dataclass
class DangerDetectionResults:
    """
    A dataclass combining detection and geographical data to highlight danger areas.

    Attributes:
        frame_id (int): The unique identifier of the frame.
        frame (np.ndarray): The image data of the frame.
        classes_names (list[str]): A list of the names of the detected classes.
        num_classes (int): The number of unique classes detected.
        classes (np.ndarray): An array of class IDs for each detected object.
        boxes_centers (np.ndarray): An array of the center coordinates of each bounding box.
        boxes_corner1 (np.ndarray): An array of the top-left corner coordinates of each bounding box.
        boxes_corner2 (np.ndarray): An array of the bottom-right corner coordinates of each bounding box.
        safety_radius_pixels (int): The defined safety radius in pixels.
        danger_mask (np.ndarray): A mask highlighting areas of danger.
        intersection_mask (np.ndarray): A mask showing the intersection of detection and geo-analysis.
        danger_types (str): A list of string describing the types of danger detected.
        timestamp (float): The timestamp of reception (of the frame).
        original_wh tuple(int, int): The original shape of the image
    """
    frame_id: int
    frame: np.ndarray
    classes_names: list[str]
    num_classes: int
    classes: np.ndarray
    boxes_centers: np.ndarray
    boxes_corner1: np.ndarray
    boxes_corner2: np.ndarray
    safety_radius_pixels: int
    danger_mask: np.ndarray
    intersection_mask: np.ndarray
    danger_types: str
    timestamp: float
    original_wh: tuple[int, int]


import numpy as np
from dataclasses import dataclass


@dataclass
class FrameSlotMetadata:
    """
    Lightweight message passed between pipeline processes via the metadata queue.
    The actual frame lives in a FrameBuffer slot; only the slot index is carried here.
    """
    frame_id: int
    timestamp: float
    original_wh: tuple[int, int]
    slot_index: int


@dataclass
class CombinedSlotMetadata:
    """
    Lightweight message combining a shared-memory frame reference with matched telemetry.
    Passed between pipeline processes via the metadata queue.
    The actual frame lives in a FrameBuffer slot; only the slot index is carried here.
    """
    frame_id: int
    timestamp: float
    original_wh: tuple[int, int]
    slot_index: int
    telemetry: dict | None


@dataclass
class FrameQueueObject:
    """
    A dataclass to represent a frame and its ID in a queue.

    Attributes:
        frame_id (int): The unique identifier for the frame.
        frame (np.ndarray): The actual image data as a NumPy array.
        timestamp (float): The timestamp of reception.
        original_wh tuple(int, int): The original shape of the image
    """
    frame_id: int
    frame: np.ndarray
    timestamp: float
    original_wh: tuple[int, int]


@dataclass
class TelemetryQueueObject:
    """
    A dataclass to represent a telemetry packet and its reception timestamp in a queue.

    Attributes:
        telemetry (dict): A dictionary object containing the drone telemetry.
        timestamp (float): The timestamp of reception.
    """
    telemetry: dict
    timestamp: float


@dataclass
class CombinedFrameTelemetryQueueObject:
    """
    A dataclass to represent the match between a frame and a telemetry packet based on timestamp.

    Attributes:
        frame_id (int): The unique identifier for the frame.
        frame (np.ndarray): The actual image data as a NumPy array.
        telemetry (dict|None): A dictionary object containing the drone telemetry.
        timestamp (float): The timestamp of reception (of the frame).
        original_wh tuple(int, int): The original shape of the image
    """
    frame_id: int
    frame: np.ndarray
    telemetry: dict|None
    timestamp: float
    original_wh: tuple[int, int]


@dataclass
class AnnotationResults:
    """
    A dataclass to store the final annotated frame and danger information.

    Attributes:
        frame_id (int): The unique identifier of the frame.
        annotated_frame (np.ndarray): The final image with annotations at the original resolution.
        alert_msg (str): A string describing the types of danger detected.
        timestamp (float): The timestamp of reception (of the frame).
    """
    frame_id: int
    annotated_frame: np.ndarray
    alert_msg: str
    timestamp: float


@dataclass
class AnnotationSlotMetadata:
    """
    Lightweight message carrying a shared-memory slot reference to the full-resolution
    annotated frame. Passed from DangerAnnotationWorker downstream to the alert writer
    and then to the video writer.

    The slot points to a (original_H, original_W, 3) BGR array at the original video
    resolution, with all danger overlays, bounding boxes, and safety circles already drawn.

    alert_msg is an empty string when no danger was detected, or a human-readable
    description of the active danger types (e.g. "Roads & Steep slope") when danger exists.
    The alert writer uses this field to decide whether and what to notify.
    """
    frame_id: int
    timestamp: float
    slot_index: int
    alert_msg: str

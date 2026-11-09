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

    latitude / longitude are the drone position (WGS84 degrees) from the telemetry matched
    to this frame, or None when no telemetry was available for it.
    """
    frame_id: int
    timestamp: float
    slot_index: int
    alert_msg: str
    latitude: float | None = None
    longitude: float | None = None


def drone_position(telemetry: dict | None) -> tuple[float | None, float | None]:
    """
    Extract the drone (latitude, longitude) from a telemetry dict.
    Returns (None, None) when telemetry is missing or lacks either coordinate.
    """
    if not telemetry:
        return None, None
    lat = telemetry.get("latitude")
    lon = telemetry.get("longitude")
    if lat is None or lon is None:
        return None, None
    return float(lat), float(lon)

import numpy as np
from dataclasses import dataclass


@dataclass
class TrackingResult:
    """
    A dataclass to store the results of an object tracking task.

    Attributes:
        frame_id (int): The unique identifier of the frame where detection occurred.
        frame (np.ndarray): The image data of the frame.
        classes_names (list[str]): A list of the names of the detected classes.
        num_classes (int): The number of unique classes detected.
        classes (np.ndarray): An array of class IDs for each detected object.
        boxes_corner1 (np.ndarray): An array of the top-left corner coordinates of each bounding box.
        boxes_corner2 (np.ndarray): An array of the bottom-right corner coordinates of each bounding box.
        scalenorm_boxes_centers (np.ndarray): An array of the normalized, shape adjusted, center coordinates of each bounding box.
        objects_ids (list[int]): list of tracked object ids
        timestamp (float): The timestamp of reception (of the frame).
        original_wh tuple(int, int): The original shape of the image
    """
    frame_id: int
    frame: np.ndarray
    classes_names: list[str]
    num_classes: int
    classes: np.ndarray
    boxes_corner1: np.ndarray
    boxes_corner2: np.ndarray
    scalenorm_boxes_centers: np.ndarray
    objects_ids: list[int]
    timestamp: float
    original_wh: tuple[int, int]


@dataclass
class AnomalyInferenceResults:
    """
    A dataclass to store the model predictions
    
    ids (list[str]): the list of entity ids present in the frame
    status (list[bool]): a boolean array specifying, for each entity, wheter it behaved anomalously in the window

    """
    ids: list[int]
    status: list[bool]
    


@dataclass
class CombinedAnomalyDetectionResults:
    """
    A dataclass combining raw and anomaly detecion data to highlight anomalous entities.

    Attributes:
        frame_id (int): The unique identifier of the frame.
        frame (np.ndarray): The image data of the frame.
        classes_names (list[str]): A list of the names of the detected classes.
        num_classes (int): The number of unique classes detected.
        classes (np.ndarray): An array of class IDs for each detected object.
        boxes_corner1 (np.ndarray): An array of the top-left corner coordinates of each bounding box.
        boxes_corner2 (np.ndarray): An array of the bottom-right corner coordinates of each bounding box.
        safety_radius_pixels (int): The defined safety radius in pixels.
        are_anomalous (list[bool]): a boolean array specifying, for each entity, wheter it behaved anomalously in the window
        ids (list[int]): the list of entity ids present in the frame
        alerts_msg (str): A string describing the type of anomaly
        timestamp (float): The timestamp of reception (of the frame).
        original_wh tuple(int, int): The original shape of the image
    """
    frame_id: int
    frame: np.ndarray
    classes_names: list[str]
    num_classes: int
    classes: np.ndarray
    boxes_corner1: np.ndarray
    boxes_corner2: np.ndarray
    are_anomalous: list[bool]
    ids: list[int]
    alert_msg: str
    timestamp: float
    original_wh: tuple[int, int]
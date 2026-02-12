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
import numpy as np


def perform_detection(detector, frame, detection_args):
    # Detect animals in frame
    detection_results = detector.predict(source=frame, **detection_args)
    return postprocess_detection_results(detection_results)


def postprocess_detection_results(detection_results):

    if detection_results[0].boxes is not None:
        # Parse detection results to get bounding boxes
        data = detection_results[0].boxes.data.cpu().numpy()   # (N,6) xyxy, conf, cls — one sync
        xyxy = data[:, :4].astype(int)
        classes = data[:, 5].astype(int)
        boxes_corner1, boxes_corner2 = xyxy[:, :2], xyxy[:, 2:]
        boxes_centers = (boxes_corner1 + boxes_corner2) // 2
    else:
        classes = np.array([], dtype=int)
        boxes_centers = np.array([], dtype=int)
        boxes_corner1 = np.array([], dtype=int)
        boxes_corner2 = np.array([], dtype=int)

    return classes, boxes_centers, boxes_corner1, boxes_corner2

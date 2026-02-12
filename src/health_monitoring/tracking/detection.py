import numpy as np


def perform_tracking(detector, frame, tracking_args, aspect_ratio):
    # track animals in frame
    tracking_results = detector.track(source=frame, stream=False, persist=True, **tracking_args)
    return postprocess_tracking_results(tracking_results, aspect_ratio)


def postprocess_tracking_results(tracking_results, aspect_ratio):
    # Parse detection results to get bounding boxes and
    # create additional variables to store useful info
    if tracking_results[0].boxes.id is not None:

        ids_list = tracking_results[0].boxes.id.int().cpu().tolist()

        classes = tracking_results[0].boxes.cls.cpu().numpy().astype(int)

        if len(ids_list) != len(classes):
            #print(ids_list)
            #print(classes)
            raise ValueError("Num of IDS does not match num of detections")

        xywh_boxes = tracking_results[0].boxes.xywh.cpu().numpy()
        xyxy_boxes = tracking_results[0].boxes.xyxy.cpu().numpy()
        xywhn_boxes = tracking_results[0].boxes.xywhn.cpu().numpy()

        boxes_corner1 = xyxy_boxes[:, :2].astype(int)
        boxes_corner2 = xyxy_boxes[:, 2:].astype(int)

        boxes_centers = xywh_boxes[:, :2].astype(int)
        normalized_boxes_centers = xywhn_boxes[:, :2]

        scaled_normalized_boxes_centers = normalized_boxes_centers.copy()
        scaled_normalized_boxes_centers[:, 1] = scaled_normalized_boxes_centers[:, 1] / aspect_ratio

        # both x and y normalized in [0,1] over their respective lenghts
        # but frame dimensions are different
        # MUST preserve distance relationships in the normalized space
        # divide Y component by aspect ratio, so that X in [0,1] and Y in [1, 1/aspect_ratio]
        # example: aspect ratio = 1920/1080 = 16/9 -> X in [0,1], Y in [0,9/16]
        # sheep moves in diagonal 1080 pixels, final position is [9/16, 9/16] (not max on X but max on Y)
    else:
        ids_list = []
        classes = np.array([], dtype=int)
        boxes_centers = np.array([], dtype=int)
        normalized_boxes_centers = np.array([], dtype=np.float32)
        scaled_normalized_boxes_centers = np.array([], dtype=np.float32)
        boxes_corner1 = np.array([], dtype=int)
        boxes_corner2 = np.array([], dtype=int)

    # Missed Tracking warning
    # if tracking_results[0].boxes.id is None and tracking_results[0].boxes is not None and len(tracking_results[0].boxes.cls) > 0:
    #     print(f"WARNING: {len(tracking_results[0].boxes.cls)} detections, but no IDS assigned.")

    return_args = (
        ids_list,
        classes,
        boxes_centers,
        normalized_boxes_centers,
        scaled_normalized_boxes_centers,
        boxes_corner1,
        boxes_corner2,
    )

    return return_args

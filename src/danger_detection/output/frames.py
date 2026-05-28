import numpy as np
import cv2
from pathlib import Path


RED = (0, 0, 255)
GREEN = (0, 255, 0)
BLUE = (255, 0, 0)
YELLOW = (0, 255, 255)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
PURPLE = (128, 0, 128)

CLASS_COLOR = [BLUE, PURPLE]


# generate the constant images based on the frame shape and color
def get_danger_intersect_colored_frames(shape) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    red_quarter    = tuple(int(c * 0.25) for c in RED)
    yellow_quarter = tuple(int(c * 0.25) for c in YELLOW)
    color_danger_frame    = np.full(shape, red_quarter,    dtype=np.uint8)
    color_intersect_frame = np.full(shape, yellow_quarter, dtype=np.uint8)
    danger_buf  = np.zeros(shape, dtype=np.uint8)
    overlay_buf = np.zeros(shape, dtype=np.uint8)
    return color_danger_frame, color_intersect_frame, danger_buf, overlay_buf

def draw_safety_areas(
        annotated_frame,
        boxes_centers,
        safety_radius,
):
    # drawing safety circles & detection boxes
    for box_center in boxes_centers:
        # Draw safety circle on annotated frame (green)
        cv2.circle(annotated_frame, box_center, safety_radius, GREEN, 2)

def draw_detections(
        annotated_frame,
        classes,
        boxes_corner1,
        boxes_corner2,
):
    # drawing safety circles & detection boxes
    for obj_class, box_corner1, box_corner2 in zip(classes, boxes_corner1, boxes_corner2):
        # Draw bounding box on annotated frame (blue sheep, purple goat), on top of safety circles
        cv2.rectangle(annotated_frame, box_corner1, box_corner2, CLASS_COLOR[obj_class], 2)

def draw_dangerous_area(
        annotated_frame,
        dangerous_mask_no_intersection,
        intersection,
        color_danger_frame,
        color_intersect_frame,
        danger_buf,
        overlay_buf,
):
    danger_buf.fill(0)
    overlay_buf.fill(0)
    cv2.bitwise_and(color_danger_frame,    color_danger_frame,    dst=danger_buf,  mask=dangerous_mask_no_intersection)
    cv2.bitwise_and(color_intersect_frame, color_intersect_frame, dst=overlay_buf, mask=intersection)
    cv2.add(danger_buf, overlay_buf, dst=overlay_buf)
    
    cv2.add(annotated_frame, overlay_buf, dst=annotated_frame)
    # alternative with dimming: avoids saturation on bright pixels, ~1.23x faster than addWeighted (needs extra quarter_buf pre-allocated at setup)
    # np.right_shift(annotated_frame, 2, out=quarter_buf); cv2.subtract(annotated_frame, quarter_buf, dst=annotated_frame); cv2.add(annotated_frame, overlay_buf, dst=annotated_frame)


def draw_count(
        classes,
        num_classes,
        classes_names,
        annotated_frame,
):
    frame_height = annotated_frame.shape[0]

    # Dynamically scale font size and thickness based on frame height
    font_face = cv2.FONT_HERSHEY_SIMPLEX
    base_font_scale = 0.001 * frame_height  # Scale with frame height
    base_thickness = max(1, int(0.002 * frame_height))  # Ensure thickness is at least 1
    text_color = BLACK
    fill_color = WHITE
    line_type = cv2.LINE_AA
    org = (10, frame_height - 10)  # Initial position of the bottom-left corner of the text

    # Count classes
    class_counts = np.zeros(num_classes, dtype=np.int32)
    class_counts[: len(np.bincount(classes))] = np.bincount(classes)

    # Generate text lines
    lines = [f"N. {classes_names[idx]}: {count}" for idx, count in enumerate(class_counts)]

    # Measure text dimensions for all lines
    max_line_width = 0
    total_height = 0
    line_height = 0
    for line in lines:
        (line_width, line_height), _ = cv2.getTextSize(
            text=line,
            fontFace=font_face,
            fontScale=base_font_scale,
            thickness=base_thickness,
        )
        max_line_width = max(max_line_width, line_width)
        total_height += line_height + 5  # Add a little spacing between lines

    # Adjust text box coordinates (expand upward for all lines)
    textbox_coord_ul = (org[0] - 5, org[1] - total_height - 5)  # Expand upward
    textbox_coord_br = (org[0] + max_line_width + 5, org[1] + 5)

    # Draw white rectangle as background
    cv2.rectangle(annotated_frame, textbox_coord_ul, textbox_coord_br, fill_color, cv2.FILLED)

    # Draw each line of text inside the box
    y_offset = org[1] - total_height + line_height  # Start at the top line
    for line in lines:
        cv2.putText(
            img=annotated_frame,
            text=line,
            org=(org[0], y_offset),
            fontFace=font_face,
            fontScale=base_font_scale,
            color=text_color,
            thickness=base_thickness,
            lineType=line_type,
        )
        y_offset += line_height + 5  # Move down to the next line

    return annotated_frame


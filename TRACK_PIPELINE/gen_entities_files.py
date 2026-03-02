import pandas as pd
import cv2
from pathlib import Path
import yaml
import numpy as np
from ultralytics import YOLO
from src.health_monitoring.tracking.detection import perform_tracking
import os
from argparse import ArgumentParser
from TRACK_PIPELINE.entities_history import EntitiesTrackHistory
from TRACK_PIPELINE.gen_camera_files import main as do_camera_tracking


RED = (0, 0, 255)
BLUE = (255, 0, 0)
PURPLE = (128, 0, 128)
WHITE = (255, 255, 255)
CLASS_COLOR = [BLUE, PURPLE]

FONT = cv2.FONT_HERSHEY_SIMPLEX
FONT_SCALE = 0.4
TEXT_THICKNESS = 1

FPS = 30
FRAME_HEIGHT = 720
FRAME_WIDTH = 1280
ASPECT_RATIO = FRAME_WIDTH/FRAME_HEIGHT


def draw_detections(
        annotated_frame,
        classes,
        ids,
        boxes_corner1,
        boxes_corner2,
):

    ids_annotated_frame = annotated_frame.copy()    # create frame copy to show ids, later overlay it onto detections

    # drawing detection boxes
    for obj_class, id, box_corner1, box_corner2 in zip(classes, ids, boxes_corner1, boxes_corner2):
        # Choose color depending on class (purple sheep, blue goat)
        color = CLASS_COLOR[obj_class]
        # Draw bounding box on frame
        cv2.rectangle(annotated_frame, box_corner1, box_corner2, color, 2)

        # Draw ID bounding box on frame
        cv2.rectangle(ids_annotated_frame, box_corner1, box_corner2, color, -1)        # Get text size and position
        # Setup ID text
        text = str(id)
        text_size = cv2.getTextSize(text, FONT, FONT_SCALE, TEXT_THICKNESS)[0]
        text_x = box_corner1[0] + (box_corner2[0] - box_corner1[0] - text_size[0]) // 2
        text_y = box_corner1[1] + (box_corner2[1] - box_corner1[1] + text_size[1]) // 2
        # Draw text
        cv2.putText(ids_annotated_frame, text, (text_x, text_y), FONT, FONT_SCALE, WHITE, TEXT_THICKNESS)

    # Blend overlay with original frame
    alpha = 0.4
    cv2.addWeighted(ids_annotated_frame, alpha, annotated_frame, 1 - alpha, 0, annotated_frame)


def do_tracking(source_video_path, dest_video_path, yolo_conf_path):

    history = EntitiesTrackHistory()

    dest_dir = Path(dest_video_path).parent

    entities_json = dest_dir / "entities_1.json"
    entities_metadata_json = dest_dir / "entities_1_npy_metadata.json"
    entities_npy = dest_dir / "entities_1.npy"

    # Define the output video properties
    annotated_writer = cv2.VideoWriter(
        filename=dest_video_path,
        fourcc=cv2.VideoWriter_fourcc(*"mp4v"),
        fps=FPS,
        frameSize=(FRAME_WIDTH, FRAME_HEIGHT),
    )

    # load deafult yolo config
    with open(yolo_conf_path, "r") as file:
        yolo_cfg = yaml.safe_load(file)
        yolo_cfg["verbose"] = False
    
    # load YOLO model
    model_checkpoint = yolo_cfg.pop("model_checkpoint")
    model = YOLO(model=model_checkpoint, task="detect")

    # Open actual video
    cap = cv2.VideoCapture(source_video_path)
    assert cap.isOpened(), f"Error reading video file {source_video_path}"

    while cap.isOpened():
        success, frame = cap.read()

        # stop when video ends
        if not success:
            print("Video processing has been successfully completed.")
            break

        # do tracking
        (
            ids,
            classes,
            _,
            _,
            scalenorm_centers,
            boxes_corner1,
            boxes_corner2,
        ) = perform_tracking(
                detector=model,
                frame=frame,
                tracking_args=yolo_cfg,
                aspect_ratio=ASPECT_RATIO,
        )

        # draw detection boxes
        draw_detections(frame, classes, ids, boxes_corner1, boxes_corner2)
        # save the annotated frame to the video
        annotated_writer.write(frame)

        # update history
        history.update(ids, scalenorm_centers)

    # close videos
    cap.release()
    annotated_writer.release()

    # history dump
    history.dump_json(entities_json)
    history.dump_metadata_json(entities_metadata_json)
    history.dump_npy(entities_npy)


def process_directory(root_folder, yolo_conf, out_dir):
    
    root_path = Path(root_folder).resolve()
    
    # Create parallel folder: "OriginalName_optimal_tracking"
    output_root = Path(out_dir)
    
    for current_dir, _, files in os.walk(root_path):
        for file in files:
            if file.lower().endswith(".mp4"):

                input_file_path = Path(current_dir) / file
                
                # Maintain subfolder hierarchy
                # relative_path = input_file_path.relative_to(root_path)
                # target_dir = output_root / relative_path.parent
                # target_dir.mkdir(parents=True, exist_ok=True)
                
                target_dir = output_root/Path(file).stem
                target_dir.mkdir(exist_ok=True, parents=True)
                
                
                # Append resolution to name
                new_name = f"{input_file_path.stem}_optimal_tracking{input_file_path.suffix}"
                output_file_path = target_dir / new_name
                
                print(f"Adding frame ids: {target_dir}...")
                do_tracking(input_file_path, output_file_path, yolo_conf)
                
                do_camera_tracking(
                    video_path=output_file_path,
                    flight_data_path=input_file_path.with_suffix(".SRT"),
                    camera_json_path=target_dir /"camera_1.json",
                    camera_metadata_path=target_dir /"camera_1_npy_metadata.json",
                    camera_npy_path=target_dir /"camera_1.npy",
                )


if __name__ == "__main__":        

    parser = ArgumentParser(description="Add frame IDS")
    parser.add_argument("--input_dir", help="Path to the folder containing videos.")
    parser.add_argument("--out_dir", help="Path where to save all annoatations")
    parser.add_argument("--yolo_conf", help="Path to yolo tracker yaml config", default="./configs/health_monitoring/tracker.yaml")
    # yolo conf contains path to tracker conf
    
    args = parser.parse_args()
   
    if not os.path.isdir(args.input_dir):
        print("Invalid directory path.")
        exit(1)

    if not os.path.isfile(args.yolo_conf):
        print("Invalid YOLO conf path.")
        exit(1)

    os.makedirs(args.out_dir, exist_ok=True)

    process_directory(args.input_dir, args.yolo_conf, args.out_dir)


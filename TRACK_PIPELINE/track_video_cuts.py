import pandas as pd
import cv2
from pathlib import Path
import yaml
import numpy as np
from ultralytics import YOLO
from src.health_monitoring.tracking.detection import perform_tracking


RED = (0, 0, 255)
BLUE = (255, 0, 0)
PURPLE = (128, 0, 128)
WHITE = (255, 255, 255)
CLASS_COLOR = [BLUE, PURPLE]

FONT = cv2.FONT_HERSHEY_SIMPLEX
FONT_SCALE = 0.6
TEXT_THICKNESS = 1

FPS = 30
FRAME_HEIGHT = 720
FRAME_WIDTH = 1280
ASPECT_RATIO = FRAME_WIDTH/FRAME_HEIGHT

def create_text_frames(
    path, 
    start_frame, 
    end_frame,
    width=1280, 
    height=720,
    font_scale=0.6, 
    thickness=2
):
    """
    Creates black frames with the given text written on them
    for frames in range [start_frame, end_frame).

    Returns a list of frames (numpy arrays).
    """

    text = f"{str(path)}\t{start_frame} - {end_frame}"

    frame = np.zeros((height, width, 3), dtype=np.uint8)

    # Get text size for centering
    text_size, _ = cv2.getTextSize(text, FONT, font_scale, thickness)
    text_width, text_height = text_size
    # Center position
    x = (width - text_width) // 2
    y = (height + text_height) // 2
    # Put text on frame (white color)
    cv2.putText(frame, text, (x, y), FONT, font_scale, WHITE, thickness, cv2.LINE_AA)

    return frame

    
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


def main(save_dir, videos_df, configs):

    base_yolo_cfg = "/home/simone/projects/AGRARIAN/configs/health_monitoring/tracker.yaml"
    base_tracker_cfg = "/home/simone/projects/AGRARIAN/configs/health_monitoring/botsort.yaml"

    for conf, iou, track_high_thresh, track_low_thresh, new_track_thresh, track_buffer, match_thresh, proximity_thresh, appearance_thresh  in configs:
        
        # create out dir
        save_path = Path(save_dir)/f"conf{conf}_iou{iou}_tht{track_high_thresh}_tlt{track_low_thresh}_ntt{new_track_thresh}_tb{track_buffer}_mt{match_thresh}_pt{proximity_thresh}_at{appearance_thresh}"
        save_path.mkdir(exist_ok=True, parents=True)
    
        # load default tracker config
        with open(base_tracker_cfg, "r") as file:
            new_tracker_config = yaml.safe_load(file)

        # change .... in tracker config
        new_tracker_config["track_high_thresh"] = track_high_thresh
        new_tracker_config["track_low_thresh"] = track_low_thresh
        new_tracker_config["new_track_thresh"] = new_track_thresh
        new_tracker_config["track_buffer"] = track_buffer
        new_tracker_config["match_thresh"] = match_thresh
        new_tracker_config["proximity_thresh"] = proximity_thresh
        new_tracker_config["appearance_thresh"] = appearance_thresh
        print(new_tracker_config)
        
        # save updated tracker config
        new_tracker_config_path = save_path / "botsort.yaml"
        with open(new_tracker_config_path, "w") as file:
            yaml.dump(new_tracker_config, file, indent=2)

        # load deafult yolo config
        with open(base_yolo_cfg, "r") as file:
            new_yolo_cfg = yaml.safe_load(file)
        
        # change conf/iou/new_tracker_path in yolo cfg
        new_yolo_cfg["conf"] = conf
        new_yolo_cfg["iou"] = iou
        new_yolo_cfg["tracker"] = str(new_tracker_config_path)
        new_yolo_cfg["verbose"] = False
        
        # save update yolo cfg
        new_yolo_config_path = save_path / "yolo.yaml"
        with open(new_yolo_config_path, "w") as file:
            yaml.dump(new_yolo_cfg, file, indent=2)
        
        # load YOLO model checkpoint
        model_checkpoint = new_yolo_cfg.pop("model_checkpoint")

        # Define the output video properties
        output_video_path = save_path / "tracking.mp4"
        annotated_writer = cv2.VideoWriter(
            filename=output_video_path,
            fourcc=cv2.VideoWriter_fourcc(*"mp4v"),
            fps=FPS,
            frameSize=(FRAME_WIDTH, FRAME_HEIGHT),
        )

        for df_row in videos_df.itertuples(index=False):
            
            video_path = df_row.path
            from_frame = df_row.from_frame
            to_frame = df_row.to_frame

            # instantiate YOLO model for specific track config and video cut
            model = YOLO(model=model_checkpoint, task="detect")

            # intro frames
            for _ in range(FPS):
                frame = create_text_frames(video_path, from_frame, to_frame, FRAME_WIDTH, FRAME_HEIGHT)
                annotated_writer.write(frame)

            # Open actual video
            cap = cv2.VideoCapture(video_path)
            assert cap.isOpened(), "Error reading video file"

            # Video processing loop
            print(f"Processing {(video_path)} ...")
            frame_id = 0
            while cap.isOpened():
                success, frame = cap.read()

                # move on to next video
                if not success:
                    print("Video processing has been successfully completed.")
                    break

                frame_id += 1

                # skip frames that are not in the relevat interval of frames
                if not (from_frame < frame_id < to_frame):
                    continue
                
                # do tracking
                (
                    ids,
                    classes,
                    _,
                    _,
                    _,
                    boxes_corner1,
                    boxes_corner2,
                ) = perform_tracking(
                        detector=model,
                        frame=frame,
                        tracking_args=new_yolo_cfg,
                        aspect_ratio=ASPECT_RATIO,
                )

                # draw detection boxes
                draw_detections(frame, classes, ids, boxes_corner1, boxes_corner2)

                # save the annotated frame to the video
                annotated_writer.write(frame)

            # close videos
            cap.release()
        
        annotated_writer.release()

if __name__ == "__main__":        

    # DJI_202410241029_019-1 		0:25-0:40
    # DJI_202410241029_019-1 		1:40-1:50
    # DJI_202410241029_019-4 		1:00-1:15
    # DJI_202410241029_019-8 		1:15-1:20
    # DJI_202502241159_020-1		0:20:0:50
    # DJI_202502241159_020-2		0:10-0.40
    # DJI_202502241159_020-3		2:15-2:35
    # DJI_202502241159_020-3		5:25-5:55
    # DJI_202502241159_020-4		1:20-1:30
    # DJI_202502241159_020-5		1:45-1:50
    # DJI_202502241223_021-1		2:15-2:25
    # DJI_202502241223_021-2		2:00-2:10
    # DJI_202502241223_021-3		3:30-3:35
    # DJI_202502241223_021-4		0:45-0:50
    # DJI_202502241340_023-1		0:00-0:30
    # DJI_202502241340_023-1		1:15-1:30
    # DJI_202502241340_023-1		3:05-3:25
    # DJI_202502241340_023-2		0:00-0:30
    # DJI_202502241357_024-1		0:00-0:25
    # DJI_202502241357_024-2		0:10-0:20
    # DJI_202502241357_024-3		0.20-0:40
    # DJI_202502241357_024-4		0:0-=:20
    # DJI_202502241357_024-4		1:30-1:45
    # DJI_202503061323_026-1		4:40-5:00
    # DJI_202503061323_026-2		0:15-0:25
    # DJI_202503061323_026-3		0:05-0:35								
    # DJI_202503061323_026-3		1:40-2:05							
    # DJI_202503061323_026-3		2:30-2:40								
    # DJI_202503061341_027-3		03:45-4:15
    # DJI_202503181150_028-2		0:15-0:45

    videos = [
        ("/home/simone/Desktop/MAICH_v2_1280x720/DJI_202410241029_019/DJI_20241024103403_0001_D_1280x720.MP4", 750, 1200),    
        ("/home/simone/Desktop/MAICH_v2_1280x720/DJI_202410241029_019/DJI_20241024103403_0001_D_1280x720.MP4", 3000, 3300),
        ("/home/simone/Desktop/MAICH_v2_1280x720/DJI_202410241029_019/DJI_20241024104019_0004_D_1280x720.MP4", 1800, 2250),
        ("/home/simone/Desktop/MAICH_v2_1280x720/DJI_202410241029_019/DJI_20241024104935_0008_D_1280x720.MP4", 2250, 2400),
        ("/home/simone/Desktop/MAICH_v2_1280x720/DJI_202502241159_020/DJI_20250224120046_0001_D_1280x720.MP4", 600, 1500),
        ("/home/simone/Desktop/MAICH_v2_1280x720/DJI_202502241159_020/DJI_20250224120324_0002_D_1280x720.MP4", 300, 1200),
        ("/home/simone/Desktop/MAICH_v2_1280x720/DJI_202502241159_020/DJI_20250224120558_0003_D_1280x720.MP4", 4050, 4650),
        ("/home/simone/Desktop/MAICH_v2_1280x720/DJI_202502241159_020/DJI_20250224120558_0003_D_1280x720.MP4", 9750, 10650),
        ("/home/simone/Desktop/MAICH_v2_1280x720/DJI_202502241159_020/DJI_20250224121245_0004_D_1280x720.MP4", 2400, 2700),
        ("/home/simone/Desktop/MAICH_v2_1280x720/DJI_202502241159_020/DJI_20250224121827_0005_D_1280x720.MP4", 3150, 3300),
        ("/home/simone/Desktop/MAICH_v2_1280x720/DJI_202502241223_021/DJI_20250224122443_0001_D_1280x720.MP4", 4050, 4350),
        ("/home/simone/Desktop/MAICH_v2_1280x720/DJI_202502241223_021/DJI_20250224123005_0002_D_1280x720.MP4", 3600, 3900),
        ("/home/simone/Desktop/MAICH_v2_1280x720/DJI_202502241223_021/DJI_20250224123534_0003_D_1280x720.MP4", 6300, 6450),
        ("/home/simone/Desktop/MAICH_v2_1280x720/DJI_202502241223_021/DJI_20250224124109_0004_D_1280x720.MP4", 1350, 1500),
        ("/home/simone/Desktop/MAICH_v2_1280x720/DJI_202502241340_023/DJI_20250224134208_0001_D_1280x720.MP4", 0, 900),
        ("/home/simone/Desktop/MAICH_v2_1280x720/DJI_202502241340_023/DJI_20250224134208_0001_D_1280x720.MP4", 2250, 2700),
        ("/home/simone/Desktop/MAICH_v2_1280x720/DJI_202502241340_023/DJI_20250224134208_0001_D_1280x720.MP4", 5550, 6150),
        ("/home/simone/Desktop/MAICH_v2_1280x720/DJI_202502241340_023/DJI_20250224134619_0002_D_1280x720.MP4", 0, 900),
        ("/home/simone/Desktop/MAICH_v2_1280x720/DJI_202502241357_024/DJI_20250224135834_0001_D_1280x720.MP4", 0, 750),
        ("/home/simone/Desktop/MAICH_v2_1280x720/DJI_202502241357_024/DJI_20250224140006_0002_D_1280x720.MP4", 300, 600),
        ("/home/simone/Desktop/MAICH_v2_1280x720/DJI_202502241357_024/DJI_20250224140114_0003_D_1280x720.MP4", 600, 1200),
        ("/home/simone/Desktop/MAICH_v2_1280x720/DJI_202502241357_024/DJI_20250224140224_0004_D_1280x720.MP4", 0, 600),
        ("/home/simone/Desktop/MAICH_v2_1280x720/DJI_202502241357_024/DJI_20250224140224_0004_D_1280x720.MP4", 2700, 3150),
        ("/home/simone/Desktop/MAICH_v2_1280x720/DJI_202503061323_026/DJI_20250306132455_0001_D_1280x720.MP4", 8400, 9000),
        ("/home/simone/Desktop/MAICH_v2_1280x720/DJI_202503061323_026/DJI_20250306133155_0002_D_1280x720.MP4", 450, 750),
        ("/home/simone/Desktop/MAICH_v2_1280x720/DJI_202503061323_026/DJI_20250306133227_0003_D_1280x720.MP4", 150, 1050),								
        ("/home/simone/Desktop/MAICH_v2_1280x720/DJI_202503061323_026/DJI_20250306133227_0003_D_1280x720.MP4", 3000, 3750),							
        ("/home/simone/Desktop/MAICH_v2_1280x720/DJI_202503061323_026/DJI_20250306133227_0003_D_1280x720.MP4", 4500, 4800),								
        ("/home/simone/Desktop/MAICH_v2_1280x720/DJI_202503061341_027/DJI_20250306134844_0003_D_1280x720.MP4", 6750, 7650),
        ("/home/simone/Desktop/MAICH_v2_1280x720/DJI_202503181150_028/DJI_20250318115602_0002_D_1280x720.MP4", 450, 1350),
    ]

    videos_df = pd.DataFrame.from_records(videos, columns=["path", "from_frame", "to_frame"])

    tracker_configs = [
        # "yolo_conf"
        # "yolo_iou"
        # "track_high_thresh"
        # "track_low_thresh"
        # "new_track_thresh"
        # "track_buffer"
        # "match_thresh"
        # "proximity_thresh"
        # "appearance_thresh"

        # exploratory
        # [0.35, 0.50, 0.40, 0.10, 0.50, 30, 0.80, 0.50, 0.30],            # 1. High Precision	        ==>>> NEW BEST
        # [0.15, 0.60, 0.20, 0.05, 0.20, 120, 0.70, 0.40, 0.45],           # 2. High Recall	
        # [0.25, 0.45, 0.25, 0.10, 0.30, 90, 0.85, 0.60, 0.25],            # 3. High Alt / Small Targets	
        # [0.25, 0.50, 0.30, 0.10, 0.40, 60, 0.80, 0.50, 0.20],            # 4. Balanced / General	

        # optimize conf
        #[0.25, 0.50, 0.25, 0.10, 0.35, 60, 0.80, 0.60, 0.30],       
        #[0.30, 0.50, 0.30, 0.10, 0.40, 60, 0.80, 0.60, 0.30],   =====> BEST
        # 1 vs 2, meglkio 2     

        # optimize new_track_thresh
        #[0.25, 0.50, 0.25, 0.10, 0.25, 60, 0.80, 0.60, 0.30],      # match all detections to track 
        #[0.25, 0.50, 0.25, 0.10, 0.30, 60, 0.80, 0.60, 0.30],      # increse new track certainty threshold
        #[0.25, 0.50, 0.25, 0.10, 0.35, 60, 0.80, 0.60, 0.30],      # increse new track certainty threshold agin
        
        # optimize reid: (proximity_thresh &  appearance_thresh, last 2 values)
        #[0.25, 0.50, 0.25, 0.10, 0.35, 60, 0.80, 0.50, 0.25], # 1. High-Recall (Very loose look, standard distance)
        #[0.25, 0.50, 0.25, 0.10, 0.35, 60, 0.80, 0.50, 0.40], # 2. Balanced-Recall (Loose look, standard distance)
        #[0.25, 0.50, 0.25, 0.10, 0.35, 60, 0.80, 0.60, 0.25], # 3. Optimized Drone (The "Brown Blob" Special)
        #[0.25, 0.50, 0.25, 0.10, 0.35, 60, 0.80, 0.60, 0.35], # 4. Optimized Drone (The "Brown Blob" Special)
        #[0.25, 0.50, 0.25, 0.10, 0.35, 60, 0.80, 0.70, 0.20], # 5. Spatial Anchor (Tight distance, very loose look)
        #[0.25, 0.50, 0.25, 0.10, 0.35, 60, 0.80, 0.70, 0.30], # 6. Strict Spatial (Tight distance, moderate look)
        # --> If you get ID Switches (Animal A becomes Animal B): Increase proximity
        # --> if you get ID Fragmentation (Animal A becomes a New ID): Decrease appearance

        # =============== ROUND 2 ==================

        #[0.30, 0.50, 0.30, 0.10, 0.40, 60, 0.80, 0.5, 0.25], # COMARARE 2 PRIMO GRUPPO E 1 TERZ GRUPPO
        #[0.30, 0.50, 0.30, 0.10, 0.40, 60, 0.80, 0.35, 0.25], # ANCORA - PROX (salta)
        # differenze trascurabili con #6

        #[0.30, 0.65, 0.30, 0.10, 0.40, 60, 0.80, 0.5, 0.25], # IOU IU ALTO GRANDI GRUPPI
        #[0.30, 0.75, 0.30, 0.10, 0.40, 60, 0.80, 0.5, 0.25], # IOU IU ALTO GRANDI GRUPPI
        # molto peggio

        #[0.40, 0.50, 0.50, 0.10, 0.55, 45, 0.80, 0.55, 0.30],
        #[0.50, 0.50, 0.60, 0.10, 0.65, 45, 0.80, 0.55, 0.30],

    ]

    # 1 e 6 meglio 1
    # 1 e 3 meglio 1
    # 1 e 2 meglio (identiche)

    main(
        save_dir="/home/simone/Desktop/tracking",
        videos_df=videos_df,
        configs=tracker_configs,
    )

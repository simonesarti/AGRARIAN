import os
import cv2
import argparse
from pathlib import Path
import shutil

def add_frame_ids(input_path, output_path):
    """
    Reads a video and overlays the frame ID in the upper-left corner
    on a black background.
    """
    cap = cv2.VideoCapture(str(input_path))
    
    # Get original video properties
    fps = cap.get(cv2.CAP_PROP_FPS)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')

    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.8
    thickness = 2
    
    # Maintain original dimensions
    out = cv2.VideoWriter(str(output_path), fourcc, fps, (width, height))

    frame_id = 0
    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break
        
        # 1. Define text properties
        text = str(frame_id)

        
        # 2. Calculate text size to make the black box fit perfectly
        (text_width, text_height), baseline = cv2.getTextSize(text, font, font_scale, thickness)
        
        # 3. Draw black rectangle (background)
        # Coordinates: (x1, y1), (x2, y2)
        cv2.rectangle(frame, (0, 0), (text_width + 10, text_height + 15), (0, 0, 0), -1)
        
        # 4. Put white text over the rectangle
        # Position is the bottom-left corner of the text string
        cv2.putText(frame, text, (5, text_height + 5), font, font_scale, (255, 255, 255), thickness)

        out.write(frame)
        frame_id += 1

    cap.release()
    out.release()

def process_directory(root_folder):
    root_path = Path(root_folder).resolve()
    # Create parallel folder: "OriginalName_frameids"
    output_root = root_path.parent / f"{root_path.name}_frameids"
    
    for current_dir, _, files in os.walk(root_path):
        for file in files:
            if file.lower().endswith(".mp4") or file.lower().endswith(".srt"):
                input_file_path = Path(current_dir) / file
                
                # Maintain subfolder hierarchy
                relative_path = input_file_path.relative_to(root_path)
                target_dir = output_root / relative_path.parent
                target_dir.mkdir(parents=True, exist_ok=True)
                
                # Append resolution to name
                new_name = f"{input_file_path.stem}_frameids{input_file_path.suffix}"
                output_file_path = target_dir / new_name
                
                # if video, add frame ids
                if file.lower().endswith(".mp4"): 
                    print(f"Adding frame ids: {relative_path}...")
                    add_frame_ids(input_file_path, output_file_path)
                # if telemetry, copy it
                else:
                    print("Copying telemetry ...")
                    shutil.copy2(input_file_path, output_file_path)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Add frame IDS")
    parser.add_argument("--input_dir", help="Path to the folder containing videos.")
    
    args = parser.parse_args()
    if os.path.isdir(args.input_dir):
        process_directory(args.input_dir)
    else:
        print("Invalid directory path.")
"""

if __name__ == "__main__":
    add_frame_ids("/home/simone/Desktop/MAICH_v2_1280x720/DJI_202410241029_019/DJI_20241024103403_0001_D_1280x720.MP4", "/home/simone/Desktop/test.mp4")
"""

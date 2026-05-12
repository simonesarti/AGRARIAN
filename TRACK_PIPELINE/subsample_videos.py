import os
import cv2
import argparse
from pathlib import Path
import shutil

def resize_video_cv2(input_path, output_path):
    """
    Resizes 1080p to 720p using cv2.INTER_AREA for high-quality downsampling.
    """
    cap = cv2.VideoCapture(str(input_path))
    
    # Get original video properties
    fps = cap.get(cv2.CAP_PROP_FPS)
    fourcc = cv2.VideoWriter_fourcc(*'mp4v') # Standard MP4 codec
    
    # Initialize writer with 1280x720 dimensions
    out = cv2.VideoWriter(str(output_path), fourcc, fps, (1280, 720))

    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break
        
        # INTER_AREA is the preferred method for downsampling (shrinking)
        resized_frame = cv2.resize(frame, (1280, 720), interpolation=cv2.INTER_AREA)
        out.write(resized_frame)

    cap.release()
    out.release()

def process_directory(root_folder):
    root_path = Path(root_folder).resolve()
    # Create parallel folder: "OriginalName_1280x720"
    output_root = root_path.parent / f"{root_path.name}_1280x720"
    
    for current_dir, _, files in os.walk(root_path):
        for file in files:
            if file.lower().endswith(".mp4") or file.lower().endswith(".srt"):
                input_file_path = Path(current_dir) / file
                # Maintain subfolder hierarchy
                relative_path = input_file_path.relative_to(root_path)
                target_dir = output_root / relative_path.parent
                target_dir.mkdir(parents=True, exist_ok=True)
                
                # Append resolution to name
                new_name = f"{input_file_path.stem}_1280x720{input_file_path.suffix}"
                output_file_path = target_dir / new_name
                
                # if video, downsample it
                if file.lower().endswith(".mp4"): 
                    print(f"Downsampling: {relative_path}...")
                    resize_video_cv2(input_file_path, output_file_path)
                # if telemetry, copy it
                else:
                    print("Copying telemetry ...")
                    shutil.copy2(input_file_path, output_file_path)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Subsample MP4s to 720p using OpenCV.")
    parser.add_argument("--input_dir", help="Path to the folder containing videos.")
    
    args = parser.parse_args()
    if os.path.isdir(args.input_dir):
        process_directory(args.input_dir)
    else:
        print("Invalid directory path.")
import os
import cv2
import argparse
from ultralytics import YOLO

def extract_and_infer_frames(model_path, video_path, output_dir="output_frames"):
    # 1. Load the YOLO model
    print(f"Loading model from {model_path}...")
    model = YOLO(model_path)
    
    # 2. Open the input video
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"Error: Could not open video file {video_path}")
        return

    # Create output directory if it doesn't exist
    os.makedirs(output_dir, exist_ok=True)

    # Define target size
    target_width = 1280
    target_height = 720
    
    frame_count = 0
    saved_count = 0

    print(f"Processing video. Resizing and running inference on every 30th frame...")

    while cap.isOpened():
        success, frame = cap.read()
        if not success:
            break  # End of video
            
        # Only process every 30th frame
        if frame_count % 30 == 0:
            # 3. Resize the frame from 1920x1080 to 1280x720
            resized_frame = cv2.resize(frame, (target_width, target_height), interpolation=cv2.INTER_LINEAR)

            # 4. Run YOLO inference
            results = model.predict(resized_frame, imgsz=(736,1280), iou=0.5, conf=0.35, show_conf=False, show_labels=False, verbose=False)

            # 5. Plot the bounding boxes onto the frame
            annotated_frame = results[0].plot(labels=False, boxes=True, conf=False, masks=False, line_width=1, font_size=1)

            # 6. Save the frame as an image
            output_filename = os.path.join(output_dir, f"frame_{frame_count:06d}.jpg")
            cv2.imwrite(output_filename, annotated_frame)
            saved_count += 1

        frame_count += 1

    # Clean up resources
    cap.release()
    cv2.destroyAllWindows()
    print(f"Finished! Processed {frame_count} total frames and saved {saved_count} annotated images to '{output_dir}/'")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="YOLO Frame Extraction and Inference")
    parser.add_argument("--model", type=str, required=True, help="Path to the YOLO model checkpoint (.pt)")
    parser.add_argument("--video", type=str, required=True, help="Path to the input video file")
    parser.add_argument("--output_dir", type=str, default="output_frames", help="Directory to save the extracted frames")
    
    args = parser.parse_args()
    
    extract_and_infer_frames(args.model, args.video, args.output_dir)
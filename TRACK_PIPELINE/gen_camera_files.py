from pathlib import Path
from src.shared.drone_utils.flight_logs import parse_drone_flight_data
import cv2
import math
import numpy as np
import json
from geopy.distance import geodesic

# expected features:
# "normalized_h"
# "rot_sin"
# "rot_cos"
# "normalized_vx"
# "normalized_vy"
# "normalized_vh"
# "normalized_vr"


# camera.rotation is gb_yaw
# camera.altitude is rel_alt


#normalized_h = (camera.altitude - CAMERA_ALTITUDE_MIN) / (CAMERA_ALTITUDE_MAX - CAMERA_ALTITUDE_MIN)    # [0, 1]
#rot_sin = math.sin(math.radians(camera.rotation))               # [-1, 1]
#rot_cos = math.cos(math.radians(camera.rotation))               # [-1, 1]
#
#normalized_vx = camera.vx / CAMERA_HORIZONTAL_SPEED_MAX     # [-1, 1]
#normalized_vy = camera.vy / CAMERA_HORIZONTAL_SPEED_MAX     # [-1, 1]
#normalized_vh = camera.vh / CAMERA_VERTICAL_SPEED_MAX       # [-1, 1]
#normalized_vr = camera.vr / CAMERA_ROTATIONAL_SPEED_MAX     # [-1, 1]

FPS = 30
CAMERA_ALTITUDE_MIN = 10            # [m]
CAMERA_ALTITUDE_MAX = 100           # [m]
CAMERA_HORIZONTAL_SPEED_MAX = 15.0  # [m/s]
CAMERA_VERTICAL_SPEED_MAX = 6.0     # [m/s]
CAMERA_ROTATIONAL_SPEED_MAX = 45.0  # [deg/s]
        

def main(
        video_path, 
        flight_data_path, 
        camera_json_path, 
        camera_metadata_path, 
        camera_npy_path
    ):

    history = {
        "normalized_h": [],
        "rot_sin": [],
        "rot_cos": [],
        "normalized_vx": [],
        "normalized_vy": [],
        "normalized_vh": [],
        "normalized_vr": [],
    }

    cap = cv2.VideoCapture(video_path)
    assert cap.isOpened(), "Error reading video file"

    # Open drone flight data
    flight_data_file_path = Path(flight_data_path)
    flight_data_file = open(flight_data_file_path, "r")

    # Frame counter
    frame_id = 0

    previous_frame_flight_data = None

    # Video processing loop
    while cap.isOpened():
        success, frame = cap.read()
        
        if not success:
            print("Video processing has been successfully completed.")
            break

        frame_id += 1  # Update frame ID (file starts at 1)

        # parse frame flight data file
        frame_flight_data = parse_drone_flight_data(flight_data_file, frame_id)
        
        # initialize previous frame data if this is the first frame
        if previous_frame_flight_data is None:
            previous_frame_flight_data = frame_flight_data.copy()

        # frame depent metrics
        normalized_h = (frame_flight_data["rel_alt"] - CAMERA_ALTITUDE_MIN) / (CAMERA_ALTITUDE_MAX - CAMERA_ALTITUDE_MIN) # [0,1]
        rot_sin = math.sin(math.radians(frame_flight_data["gb_yaw"]))               # [-1, 1]
        rot_cos = math.cos(math.radians(frame_flight_data["gb_yaw"]))               # [-1, 1]
        
        # delta metrics

        dh = (frame_flight_data["rel_alt"] - previous_frame_flight_data["rel_alt"])     # [m]
        normalized_vh = (dh * FPS) / CAMERA_VERTICAL_SPEED_MAX   # [pure number]
        
        dr = (frame_flight_data["gb_yaw"] - previous_frame_flight_data["gb_yaw"])   # [deg]
        dr = (dr + 180) % 360 - 180  # Forces delta into [-180, 180] range
        normalized_vr = (dr * FPS) / CAMERA_ROTATIONAL_SPEED_MAX # [pure number]

        dx = geodesic(
            (frame_flight_data["latitude"], frame_flight_data["longitude"]),
            (frame_flight_data["latitude"], previous_frame_flight_data["longitude"]),
            ).meters
        normalized_vx = (dx * FPS) / CAMERA_HORIZONTAL_SPEED_MAX
        if frame_flight_data["longitude"] < previous_frame_flight_data["longitude"]:
            normalized_vx *= -1
        # this ignores circularity of longitude

        dy = geodesic(
            (frame_flight_data["latitude"], frame_flight_data["longitude"]),
            (previous_frame_flight_data["latitude"], frame_flight_data["longitude"]),
            ).meters
        normalized_vy = (dy * FPS) / CAMERA_HORIZONTAL_SPEED_MAX
        if frame_flight_data["latitude"] < previous_frame_flight_data["latitude"]:
            normalized_vy *= -1

        # ensure metrics for this frame are stored as "previous" for the next frame
        previous_frame_flight_data = frame_flight_data.copy()
        
        # add values to history
        history["normalized_h"].append(normalized_h)
        history["rot_sin"].append(rot_sin)
        history["rot_cos"].append(rot_cos)
        history["normalized_vx"].append(normalized_vx)
        history["normalized_vy"].append(normalized_vy)
        history["normalized_vh"].append(normalized_vh)
        history["normalized_vr"].append(normalized_vr)

    flight_data_file.close()

    # dump history as json
    with open(camera_json_path, "w") as file:
        json.dump(history, file, indent=4)

    # dump metedata (features names) as json
    metadata = {"features": [
        "normalized_h",
        "rot_sin",
        "rot_cos",
        "normalized_vx",
        "normalized_vy",
        "normalized_vh",
        "normalized_vr",
    ]}
    with open(camera_metadata_path, "w") as file:
        json.dump(metadata, file, indent=4)

    # dumpy history as npy (ensure order of features)
    t = len(history["normalized_h"])
    arr = np.zeros((t,7), dtype=np.float32)
    arr[:,0] = history["normalized_h"]
    arr[:,1] = history["rot_sin"]
    arr[:,2] = history["rot_cos"]
    arr[:,3] = history["normalized_vx"]
    arr[:,4] = history["normalized_vy"]
    arr[:,5] = history["normalized_vh"]
    arr[:,6] = history["normalized_vr"]
    np.save(camera_npy_path, arr)

if __name__ == "__main__":
    
    main(
        video_path="/home/simone/Desktop/MAICH_v2/DJI_202410241029_019/DJI_20241024103846_0003_D.MP4",
        flight_data_path="/home/simone/Desktop/MAICH_v2/DJI_202410241029_019/DJI_20241024103846_0003_D.SRT",
        camera_json_path="/home/simone/Desktop/MAICH_v2/DJI_202410241029_019/camera.json",
        camera_metadata_path="/home/simone/Desktop/MAICH_v2/DJI_202410241029_019/camera_metadata.json",
        camera_npy_path="/home/simone/Desktop/MAICH_v2/DJI_202410241029_019/camera.npy",
    )


    
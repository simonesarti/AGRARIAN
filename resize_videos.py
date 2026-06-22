#!/usr/bin/env python3
"""Resize videos to 1280x720 (16:9). Output saved next to the input with a name suffix."""

import subprocess
import sys
from pathlib import Path

PATHS = [
    # Add your video paths here, e.g.:
    # "/path/to/video.mp4",
]

SUFFIX = "_1280x720"


def resize_video(input_path: str) -> None:
    p = Path(input_path)
    output_path = p.with_name(p.stem + SUFFIX + p.suffix)
    cmd = [
        "ffmpeg", "-i", str(p),
        "-vf", "scale=1280:720",
        "-c:v", "libx264", "-crf", "18",
        "-c:a", "copy",
        "-y", str(output_path),
    ]
    print(f"{p.name} -> {output_path.name}")
    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    paths = sys.argv[1:] or PATHS
    if not paths:
        print("Usage: python resize_videos.py video1.mp4 video2.mp4 ...")
        sys.exit(1)
    for path in paths:
        resize_video(path)

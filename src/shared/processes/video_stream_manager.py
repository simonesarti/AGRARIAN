import subprocess
import threading
import logging
from time import time, sleep
from queue import Queue, Empty, Full
import numpy as np

from src.shared.processes.constants import (
    FPS,
    MAX_SIZE_VIDEO_STREAM,
    PIPELINE_QUEUE_TIMEOUT,
    VIDEO_OUT_STREAM_FFMPEG_STARTUP_TIMEOUT,    # 0.5
    VIDEO_OUT_STREAM_FFMPEG_SHUTDOWN_TIMEOUT,    # 8.0
    VIDEO_OUT_STREAM_STARTUP_TIMEOUT,           # 2.0
    VIDEO_OUT_STREAM_SHUTDOWN_TIMEOUT,           # 10.0
)

# ================================================================
logger = logging.getLogger("main.video_out.stream")

if not logger.handlers:  # Avoid duplicate handlers
    video_handler = logging.FileHandler('./logs/video_out_stream.log', mode='w')
    video_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(video_handler)
    logger.setLevel(logging.DEBUG)
# ================================================================


class VideoStreamManager:
    def __init__(
            self,
            mediaserver_url: str,
            fps: int = FPS,
            queue_max_size: int = MAX_SIZE_VIDEO_STREAM,
            queue_get_timeout: float = PIPELINE_QUEUE_TIMEOUT,
            ffmpeg_startup_timeout: float = VIDEO_OUT_STREAM_FFMPEG_STARTUP_TIMEOUT,
            ffmpeg_shutdown_timeout: float = VIDEO_OUT_STREAM_FFMPEG_SHUTDOWN_TIMEOUT,
            startup_timeout: float = VIDEO_OUT_STREAM_STARTUP_TIMEOUT,
            shutdown_timeout: float = VIDEO_OUT_STREAM_SHUTDOWN_TIMEOUT,
    ):

        self.mediaserver_url = mediaserver_url
        self.fps = fps

        # lazy-init frame dimensions based on first frame received
        self.width = None
        self.height = None

        self.frame_queue = Queue(maxsize=queue_max_size)

        self.running = False

        self.stream_thread = None
        self._ffmpeg_process = None
        self.stderr_consumer = None

        self.queue_get_timeout = queue_get_timeout
        self.ffmpeg_startup_timeout = ffmpeg_startup_timeout
        self.ffmpeg_shutdown_timeout = ffmpeg_shutdown_timeout
        self.startup_timeout = startup_timeout
        self.shutdown_timeout = shutdown_timeout

        # Synchronization for startup health check
        self._start_confirmed = threading.Event()
        self._startup_error = None

    def set_frame_dims(self, width: int, height: int):
        self.width = width
        self.height = height

    def push_to_queue(self, frame: np.ndarray) -> bool:
        """
        Receives frames from the Worker process.
        Returns whether the frame was enqueued or not.
        """
        if not self.running:
            logger.warning("Stream Manager not running. Dropping frame.")
            return False

        try:
            self.frame_queue.put_nowait(frame)
            return True
        except Full:
            logger.warning("Stream queue full, dropping frame to maintain real-time sync.")
            return False
    
    @staticmethod
    def log_stderr(pipe):
        for line in iter(pipe.readline, b''):
            logger.debug(f"FFmpeg: {line.decode().strip()}")

    def get_ffmpeg_command(self):

        # Optimized command for Full HD 30fps ingest
        command = [
            'ffmpeg',
            '-y', 
            '-f', 'rawvideo',
            '-vcodec', 'rawvideo',
            '-pix_fmt', 'bgr24',
            '-s', f"{self.width}x{self.height}",
            '-r', str(self.fps),
            '-i', '-', # Input from stdin pipe
            '-c:v', 'libx264',
            '-preset', 'veryfast',
            '-tune', 'zerolatency',
            '-profile:v', 'baseline',
            '-pix_fmt', 'yuv420p',
            # Rate Control
            '-b:v', '4000k',
            '-maxrate', '4000k',
            '-bufsize', '8000k',
            # GOP / Keyframe Settings
            '-g', str(2*self.fps),                    # Force keyframe every 2 seconds (at 30fps)
            '-x264-params', f'keyint={str(2*self.fps)}:min-keyint={str(2*self.fps)}:scenecut=0',
            # Output format
            '-an',                         # Remove audio if not needed
            '-f', 'flv',
            self.mediaserver_url
        ]

        return command

    def _stream_loop(self):
        """
        Background thread that manages the FFmpeg pipe and ensures
        graceful termination.
        """

        command = self.get_ffmpeg_command()

        # should only stop on parent process .close() command
        # if connection is lost, should try to reestablish it
        while self.running:

            try:
                # Launch FFmpeg process
                logger.info(
                    f"Attempting to connect to {self.mediaserver_url}"
                )
                self._ffmpeg_process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=10 ** 7
                )

                # Brief sleep to allow FFmpeg to initialize/fail connection
                sleep(self.ffmpeg_startup_timeout)

                # startup verification
                if self._ffmpeg_process.poll() is not None:
                    # Process exited immediately
                    _, stderr_data = self._ffmpeg_process.communicate()
                    self._startup_error = stderr_data.decode().split('\n')[-2] if stderr_data else "Unknown error"
                    self._start_confirmed.set()
                    return # exit the thread on failed startup
                
                # If we reach here, process is alive
                self._start_confirmed.set()
                
                #stderr consumer
                self.stderr_consumer = threading.Thread(target=self.log_stderr, args=(self._ffmpeg_process.stderr,), daemon=True)
                self.stderr_consumer.start()

                # Loop continues as long as we are "running"
                # OR there are frames left to drain.
                while self.running or not self.frame_queue.empty():
                    try:
                        # Use a timeout to block for a short time only
                        frame = self.frame_queue.get(timeout=self.queue_get_timeout)
                        self._ffmpeg_process.stdin.write(frame.tobytes())
                        self._ffmpeg_process.stdin.flush() # Ensure data transfer
                    except Empty:
                        logger.debug("Queue empty. Continuing ...")
                        continue
                    except BrokenPipeError as e:
                        logger.error(f"FFmpeg pipe broken: {e}")
                        break
                    except ConnectionResetError as e:
                        logger.error(f"Connection Reset: {e}")
                        break


            except Exception as e:
                logger.error(f"Streaming error: {e}")
                self._startup_error = str(e)
                self._start_confirmed.set()
            finally:
                self._finalize_ffmpeg()
                # on exit, checkj again whether the exit was due to thrad being stopped or due to error
                # if the process should still be running (not stopped from outside), try to reconnect
        
        # thread complete, set running status to False
        self.running = False

    def _finalize_ffmpeg(self):
        """Internal routine to close the pipe and wait for process exit."""
        if self._ffmpeg_process:
            logger.info("Closing FFmpeg pipe ...")
            try:
                if self._ffmpeg_process.stdin:
                    self._ffmpeg_process.stdin.close()
            except Exception as e:
                logger.error(f"Error closing FFmpeg stdin: {e}")
            # Always wait, even if stdin.close() raised, to avoid zombie processes
            try:
                self._ffmpeg_process.wait(timeout=self.ffmpeg_shutdown_timeout)
                logger.info("FFmpeg process exited cleanly.")
            except subprocess.TimeoutExpired:
                logger.warning("FFmpeg did not exit in time. Forcing termination.")
                self._ffmpeg_process.kill()
            except Exception as e:
                logger.error(f"Error waiting for FFmpeg to exit: {e}")

    def start(self) -> bool:
        """
        Launches the streaming thread and prepares the FFmpeg pipe.
        Returns True if the stream started correctly, False otherwise.
        """
        if self.running:
            logger.warning("Stream Manager is already running.")
            return True

        if self.width is None or self.height is None:
            logger.error("Cannot start StreamManager: Frame dimensions not set.")
            return False

        self._start_confirmed.clear()
        self._startup_error = None
        self.running = True

        self.stream_thread = threading.Thread(
            target=self._stream_loop,
            name="StreamThread",
            daemon=True
        )
        self.stream_thread.start()

        # Wait for the thread to confirm success (blocking start)
        # Timeout slightly longer than the sleep in the thread
        started = self._start_confirmed.wait(timeout=self.startup_timeout)

        if not started or self._startup_error:
            error_msg = self._startup_error if self._startup_error else "Timeout during startup"
            logger.error(f"Streaming failed to start: {error_msg}")
            self.stop()
            return False

        logger.info(f"Streaming thread successfully initialized for target: {self.mediaserver_url}")
        return True

    def stop(self):
        """Triggers graceful shutdown of the streaming thread."""

        # set stopping flag
        self.running = False

        # Give it a moment (timeout) to flush the queue before the parent process exits
        if self.stream_thread:
            self.stream_thread.join(timeout=self.shutdown_timeout)
            if self.stream_thread.is_alive():
                logger.warning("Video Streaming thread did not terminate cleanly within timeout")
            else:
                logger.info("Video Streaming thread terminated successfully")



if __name__ == "__main__":
        
    import cv2
    import datetime
    from time import time, perf_counter, sleep

    mediaserver_url = "rtmp://0.0.0.0:1935/annot"
    fps=30
    duration_seconds = 60

    def make_frame():
        # black frame
        frame = np.zeros((1080, 1920, 3), dtype=np.uint8)

        # 3. Define text properties
        font = cv2.FONT_HERSHEY_SIMPLEX
        org = (50, 100)  # Coordinates (X, Y) where text starts
        fontScale = 2
        color = (255, 255, 255)  # White in BGR
        thickness = 3

        # put current datetime string oin frame
        current_time = datetime.datetime.now().isoformat()
        cv2.putText(frame, current_time, org, font, fontScale, color, thickness, cv2.LINE_AA)

        return frame
    
    stream_manager = VideoStreamManager(
        mediaserver_url=mediaserver_url,
        fps=fps,
    )
    stream_manager.set_frame_dims(width=1920, height=1080)
    stream_manager.start()

    next = perf_counter() + 1/fps
    stop_time = next + duration_seconds

    while True:
        if perf_counter() > stop_time:
            break

        frame = make_frame()
        stream_manager.push_to_queue(frame)

        perf = perf_counter()
        if perf < next:
            sleep(next-perf)
        next += 1/fps

    stream_manager.stop()
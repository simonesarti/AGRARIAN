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
# Two dedicated loggers, one per sink, each writing to its own file so the
# RTMP stream and the local recording can be analysed independently.
stream_logger = logging.getLogger("main.video_out.stream")
if not stream_logger.handlers:
    _h = logging.FileHandler('./logs/video_out_stream.log', mode='w')
    _h.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    stream_logger.addHandler(_h)
    stream_logger.setLevel(logging.WARNING)

file_logger = logging.getLogger("main.video_out.file")
if not file_logger.handlers:
    _h = logging.FileHandler('./logs/video_out_file.log', mode='w')
    _h.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    file_logger.addHandler(_h)
    file_logger.setLevel(logging.WARNING)
# ================================================================


class FFmpegSink:
    """
    Base class for an output sink backed by a single FFmpeg subprocess fed raw
    BGR frames over stdin.

    Frames are pushed onto a bounded in-process queue (non-blocking; dropped when
    full to preserve real-time cadence). A background thread drains the queue into
    FFmpeg's stdin, performing the slow encode/write off the producer's path.

    Subclasses supply:
      - get_ffmpeg_command(): the FFmpeg argv (input is always rawvideo/bgr24 on stdin).
      - reconnect (class attr): if True, the background thread re-spawns FFmpeg after a
        failure (used for network sinks); if False, it stops after the first run
        (used for local-file sinks, which never need to reconnect).

    Lifecycle: set_frame_dims() -> start() (blocking health check) -> push_to_queue()
    -> stop() (drains the queue, then closes FFmpeg so the container trailer is written).
    """

    reconnect: bool = False
    name: str = "ffmpeg"

    def __init__(
            self,
            logger: logging.Logger,
            fps: int = FPS,
            queue_max_size: int = MAX_SIZE_VIDEO_STREAM,
            queue_get_timeout: float = PIPELINE_QUEUE_TIMEOUT,
            ffmpeg_startup_timeout: float = VIDEO_OUT_STREAM_FFMPEG_STARTUP_TIMEOUT,
            ffmpeg_shutdown_timeout: float = VIDEO_OUT_STREAM_FFMPEG_SHUTDOWN_TIMEOUT,
            startup_timeout: float = VIDEO_OUT_STREAM_STARTUP_TIMEOUT,
            shutdown_timeout: float = VIDEO_OUT_STREAM_SHUTDOWN_TIMEOUT,
    ):
        self.logger = logger
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
        Enqueue a frame for the background writer.
        Returns whether the frame was enqueued or not (dropped when full).
        """
        if not self.running:
            self.logger.warning(f"{self.name} not running. Dropping frame.")
            return False

        try:
            self.frame_queue.put_nowait(frame)
            return True
        except Full:
            self.logger.warning(f"{self.name} queue full, dropping frame to maintain real-time sync.")
            return False

    def log_stderr(self, pipe):
        for line in iter(pipe.readline, b''):
            self.logger.debug(f"FFmpeg: {line.decode().strip()}")

    def get_ffmpeg_command(self) -> list:
        raise NotImplementedError

    def _stream_loop(self):
        """
        Background thread that owns the FFmpeg subprocess. Drains the frame queue
        into FFmpeg's stdin and ensures graceful termination. If self.reconnect is
        True, it re-spawns FFmpeg after a failure; otherwise it runs exactly once.
        """

        command = self.get_ffmpeg_command()

        while self.running:

            try:
                self.logger.info(f"Starting FFmpeg for {self.name}: target={self._target_repr()}")
                self._ffmpeg_process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=10 ** 7
                )

                # Brief sleep to allow FFmpeg to initialize/fail
                sleep(self.ffmpeg_startup_timeout)

                # startup verification
                if self._ffmpeg_process.poll() is not None:
                    # Process exited immediately
                    _, stderr_data = self._ffmpeg_process.communicate()
                    self._startup_error = stderr_data.decode().split('\n')[-2] if stderr_data else "Unknown error"
                    self._start_confirmed.set()
                    return  # exit the thread on failed startup

                # If we reach here, process is alive
                self._start_confirmed.set()

                # stderr consumer
                self.stderr_consumer = threading.Thread(
                    target=self.log_stderr,
                    args=(self._ffmpeg_process.stderr,),
                    daemon=True,
                )
                self.stderr_consumer.start()

                # Loop continues as long as we are "running"
                # OR there are frames left to drain.
                while self.running or not self.frame_queue.empty():
                    try:
                        frame = self.frame_queue.get(timeout=self.queue_get_timeout)
                        t0 = time()
                        raw = frame.tobytes()
                        self._ffmpeg_process.stdin.write(raw)
                        self._ffmpeg_process.stdin.flush()
                        self.logger.debug(
                            f"[TIMING {self.name}] write={(time() - t0) * 1000:.1f}ms | "
                            f"qdepth={self.frame_queue.qsize()}"
                        )
                    except Empty:
                        continue
                    except BrokenPipeError as e:
                        self.logger.error(f"FFmpeg pipe broken: {e}")
                        break
                    except ConnectionResetError as e:
                        self.logger.error(f"Connection reset: {e}")
                        break

            except Exception as e:
                self.logger.error(f"FFmpeg sink error: {e}")
                self._startup_error = str(e)
                self._start_confirmed.set()
            finally:
                self._finalize_ffmpeg()

            # Local-file sinks never reconnect; network sinks retry while running.
            if not self.reconnect:
                break

        # thread complete, set running status to False
        self.running = False

    def _target_repr(self) -> str:
        return self.name

    def _finalize_ffmpeg(self):
        """Internal routine to close the pipe and wait for process exit."""
        if self._ffmpeg_process:
            self.logger.info("Closing FFmpeg pipe ...")
            try:
                if self._ffmpeg_process.stdin:
                    self._ffmpeg_process.stdin.close()
            except Exception as e:
                self.logger.error(f"Error closing FFmpeg stdin: {e}")
            # Always wait, even if stdin.close() raised, to avoid zombie processes
            try:
                self._ffmpeg_process.wait(timeout=self.ffmpeg_shutdown_timeout)
                self.logger.info("FFmpeg process exited cleanly.")
            except subprocess.TimeoutExpired:
                self.logger.warning("FFmpeg did not exit in time. Forcing termination.")
                self._ffmpeg_process.kill()
            except Exception as e:
                self.logger.error(f"Error waiting for FFmpeg to exit: {e}")

    def start(self) -> bool:
        """
        Launches the background thread and the FFmpeg pipe.
        Returns True if FFmpeg started correctly, False otherwise.
        """
        if self.running:
            self.logger.warning(f"{self.name} is already running.")
            return True

        if self.width is None or self.height is None:
            self.logger.error(f"Cannot start {self.name}: frame dimensions not set.")
            return False

        self._start_confirmed.clear()
        self._startup_error = None
        self.running = True

        self.stream_thread = threading.Thread(
            target=self._stream_loop,
            name=f"{self.name}Thread",
            daemon=True
        )
        self.stream_thread.start()

        # Wait for the thread to confirm success (blocking start)
        started = self._start_confirmed.wait(timeout=self.startup_timeout)

        if not started or self._startup_error:
            error_msg = self._startup_error if self._startup_error else "Timeout during startup"
            self.logger.error(f"{self.name} failed to start: {error_msg}")
            self.stop()
            return False

        self.logger.info(f"{self.name} successfully initialized for target: {self._target_repr()}")
        return True

    def stop(self):
        """Triggers graceful shutdown: drains the queue, then closes FFmpeg."""
        self.running = False

        if self.stream_thread:
            self.stream_thread.join(timeout=self.shutdown_timeout)
            if self.stream_thread.is_alive():
                self.logger.warning(f"{self.name} thread did not terminate cleanly within timeout")
            else:
                self.logger.info(f"{self.name} thread terminated successfully")


class VideoStreamManager(FFmpegSink):
    """
    RTMP output sink: encodes with libx264 and streams to a media server via FFmpeg.
    Reconnects automatically if the connection drops while the app keeps running.
    """

    reconnect = True
    name = "VideoStreamManager"

    def __init__(self, mediaserver_url: str, **kwargs):
        super().__init__(logger=stream_logger, **kwargs)
        self.mediaserver_url = mediaserver_url

    def _target_repr(self) -> str:
        return self.mediaserver_url

    def get_ffmpeg_command(self):
        # libx264 ingest for Full HD 30fps RTMP. Kept on CPU (subprocess keeps up
        # under load) so it does not compete for NVENC sessions with the recorder.
        return [
            'ffmpeg',
            '-y',
            '-f', 'rawvideo',
            '-pix_fmt', 'bgr24',
            '-s', f"{self.width}x{self.height}",
            '-r', str(self.fps),
            '-i', '-',                      # Input from stdin pipe
            '-vf', f'realtime,fps=fps={self.fps}',
            '-c:v', 'libx264',
            '-preset', 'veryfast',
            '-tune', 'zerolatency',
            '-profile:v', 'baseline',
            '-pix_fmt', 'yuv420p',
            # Rate Control
            '-b:v', '6M',
            '-maxrate', '6M',
            '-bufsize', '12M',
            # GOP / Keyframe Settings
            '-g', str(2 * self.fps),
            '-x264-params', f'keyint={2 * self.fps}:min-keyint={2 * self.fps}:scenecut=0',
            '-an',                          # no audio
            '-f', 'flv',
            self.mediaserver_url
        ]


class VideoFileWriter(FFmpegSink):
    """
    Local-file output sink: encodes with the GPU (h264_nvenc) and writes to a local
    mp4. Connection-independent (no network), so it never reconnects.

    NVENC moves the encode off the (saturated) CPU onto the GPU's dedicated encoder
    ASIC, replacing the in-process cv2.VideoWriter software encode that bottlenecked
    the recording under full-pipeline load.

    Uses fragmented mp4 so the file stays playable even if the process is killed
    before a clean shutdown.
    """

    reconnect = False
    name = "VideoFileWriter"

    def __init__(self, file_path: str, **kwargs):
        super().__init__(logger=file_logger, **kwargs)
        self.file_path = file_path

    def _target_repr(self) -> str:
        return self.file_path

    def get_ffmpeg_command(self):
        return [
            'ffmpeg',
            '-y',
            '-f', 'rawvideo',
            '-pix_fmt', 'bgr24',
            '-s', f"{self.width}x{self.height}",
            '-r', str(self.fps),
            '-i', '-',                      # Input from stdin pipe
            '-an',                          # no audio
            '-c:v', 'h264_nvenc',
            '-preset', 'p4',                # balanced speed/quality
            '-pix_fmt', 'yuv420p',
            '-b:v', '8M',
            '-maxrate', '8M',
            '-bufsize', '16M',
            '-g', str(2 * self.fps),
            # Fragmented mp4: file remains playable even on an abrupt kill.
            '-movflags', '+frag_keyframe+empty_moov+default_base_moof',
            '-f', 'mp4',
            self.file_path,
        ]


if __name__ == "__main__":

    import cv2
    import datetime
    from time import perf_counter, sleep

    mediaserver_url = "rtmp://0.0.0.0:1935/annot"
    fps = 30
    duration_seconds = 60

    def make_frame():
        frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
        font = cv2.FONT_HERSHEY_SIMPLEX
        org = (50, 100)
        fontScale = 2
        color = (255, 255, 255)
        thickness = 3
        current_time = datetime.datetime.now().isoformat()
        cv2.putText(frame, current_time, org, font, fontScale, color, thickness, cv2.LINE_AA)
        return frame

    stream_manager = VideoStreamManager(
        mediaserver_url=mediaserver_url,
        fps=fps,
    )
    stream_manager.set_frame_dims(width=1920, height=1080)
    stream_manager.start()

    next = perf_counter() + 1 / fps
    stop_time = next + duration_seconds

    while True:
        if perf_counter() > stop_time:
            break

        frame = make_frame()
        stream_manager.push_to_queue(frame)

        perf = perf_counter()
        if perf < next:
            sleep(next - perf)
        next += 1 / fps

    stream_manager.stop()

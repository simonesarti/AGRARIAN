import multiprocessing as mp
import signal
from datetime import datetime
from pathlib import Path
from time import sleep
import logging

from app.shared.processes.app_settings import AppSettings
from app.shared.processes.stream_video_reader import StreamVideoReader, StreamVideoReaderConfig
from app.shared.processes.frame_telemetry_combiner import FrameTelemetryCombiner, FrameTelemetryCombinerConfig
from app.danger_detection.processes.detection import DetectionWorker, DetectionWorkerConfig
from app.danger_detection.processes.segmentation import SegmentationWorker, SegmentationWorkerConfig
from app.danger_detection.processes.geo import GeoWorker, GeoWorkerConfig
from app.danger_detection.processes.danger_worker import DangerWorker, DangerWorkerConfig
from app.danger_detection.processes.annotation_worker import AnnotationWorker, AnnotationWorkerConfig
from app.shared.processes.output_alert_streamer import NotificationsStreamWriter, NotificationsStreamWriterConfig
from app.shared.processes.output_video_streamer import VideoProducerProcess, VideoProducerProcessConfig
from app.shared.processes.frame_buffer import FrameBuffer, MultiFrameBuffer
from app.shared.processes.constants import (
    LOCAL_OUTPUT_DIR,
    VIDEO_STREAM_READER_PROCESSING_SHAPE,
    VIDEO_STREAM_READER_ORIGINAL_SHAPE,
    MAX_SIZE_FRAME_READER_OUT,
    MAX_SIZE_DETECTION_IN,
    MAX_SIZE_SEGMENTATION_IN,
    MAX_SIZE_GEO_IN,
    MAX_SIZE_DANGER_DETECTION_RESULT,
    MAX_SIZE_NOTIFICATIONS_STREAM,
    MAX_SIZE_VIDEO_STREAM,
    FPS,
    PIPELINE_QUEUE_TIMEOUT,
    PROCESS_JOIN_TIMEOUT,
    VIDEO_STREAM_READER_CONNECTION_OPEN_TIMEOUT_S,
    VIDEO_STREAM_READER_RECONNECT_DELAY,
    VIDEO_STREAM_READER_FRAME_READ_TIMEOUT_S,
    VIDEO_STREAM_READER_FRAME_RETRY_DELAY,
    VIDEO_STREAM_READER_FRAME_MAX_CONSECUTIVE_FAILURES,
    VIDEO_STREAM_READER_EXPECTED_ASPECT_RATIO,
    TELEMETRY_LISTENER_RECONNECT_DELAY,
    TELEMETRY_LISTENER_MSG_WAIT_TIMEOUT,
    TELEMETRY_LISTENER_MAX_INCOMING_MESSAGES,
    FRAMETELCOMB_MAX_TELEM_BUFFER_SIZE,
    FRAMETELCOMB_MAX_TIME_DIFF,
    ALERTS_QUEUE_GET_TIMEOUT,
    ALERTS_MAX_CONSECUTIVE_FAILURES,
    VIDEO_OUT_STREAM_FFMPEG_STARTUP_TIMEOUT,
    VIDEO_OUT_STREAM_FFMPEG_SHUTDOWN_TIMEOUT,
    VIDEO_OUT_STREAM_STARTUP_TIMEOUT,
    VIDEO_OUT_STREAM_SHUTDOWN_TIMEOUT,
)
from app.utils import read_yaml_config


# ================================================================

logger = logging.getLogger("main")

# ================================================================


def main():

    try:
        s = AppSettings()
    except Exception as e:
        logger.critical(f"Configuration error: {e}", exc_info=True)
        exit(1)

    try:
        detection_args    = read_yaml_config("configs/danger_detection/detector.yaml")
        segmentation_args = read_yaml_config("configs/danger_detection/segmenter.yaml")
    except Exception as e:
        logger.critical(f"Failed to load models configs: {e}", exc_info=True)
        exit(1)

    dem_path              = Path("dem/dem.tif")
    dem_mask_path         = Path("dem/dem_mask.tif")
    output_dir            = Path(LOCAL_OUTPUT_DIR)
    output_dir.mkdir(exist_ok=True, parents=True)
    mqtt_certificates_dir = Path("certificates") / "mqtt"

    session_ts = datetime.now().strftime('%Y%m%d_%H%M%S')

    # ============== EVENTS ==============

    error_event = mp.Event()

    # ============== FRAME BUFFER SHAPES ==============
    # Shapes are (H, W, C) — NumPy convention

    _proc_h, _proc_w = VIDEO_STREAM_READER_PROCESSING_SHAPE[1], VIDEO_STREAM_READER_PROCESSING_SHAPE[0]
    _orig_h, _orig_w = VIDEO_STREAM_READER_ORIGINAL_SHAPE[1],   VIDEO_STREAM_READER_ORIGINAL_SHAPE[0]

    _3ch = (_proc_h, _proc_w, 3)
    _ann = (_orig_h, _orig_w, 3)

    # ============== FRAME BUFFERS ==============
    # n_slots matches the corresponding metadata queue maxsize: each queue entry
    # holds exactly one slot index, so queue capacity == max in-flight frames.

    reader_to_combiner_buf        = FrameBuffer(_3ch, n_slots=MAX_SIZE_FRAME_READER_OUT)
    combiner_to_detection_buf     = FrameBuffer(_3ch, n_slots=MAX_SIZE_DETECTION_IN)
    detection_to_segmentation_buf = FrameBuffer(_3ch, n_slots=MAX_SIZE_SEGMENTATION_IN)
    segmentation_to_geo_buf       = MultiFrameBuffer(
        primary_shape=_3ch,
        secondary_shape=(2, _proc_h, _proc_w),
        n_slots=MAX_SIZE_GEO_IN,
    )
    geo_to_danger_buf             = MultiFrameBuffer(
        primary_shape=_3ch,
        secondary_shape=(5, _proc_h, _proc_w),
        n_slots=MAX_SIZE_DANGER_DETECTION_RESULT,
    )
    danger_to_annotation_buf      = MultiFrameBuffer(
        primary_shape=_3ch,
        secondary_shape=(2, _proc_h, _proc_w),
        n_slots=MAX_SIZE_DANGER_DETECTION_RESULT,
    )
    annotation_to_alert_buf       = FrameBuffer(_ann, n_slots=MAX_SIZE_NOTIFICATIONS_STREAM)
    annotation_to_video_buf       = FrameBuffer(_ann, n_slots=MAX_SIZE_VIDEO_STREAM)

    frame_buffers = [
        reader_to_combiner_buf,
        combiner_to_detection_buf,
        detection_to_segmentation_buf,
        segmentation_to_geo_buf,
        geo_to_danger_buf,
        danger_to_annotation_buf,
        annotation_to_alert_buf,
        annotation_to_video_buf,
    ]

    # ============== METADATA QUEUES ==============

    reader_to_combiner_q        = mp.Queue(maxsize=MAX_SIZE_FRAME_READER_OUT)
    combiner_to_detection_q     = mp.Queue(maxsize=MAX_SIZE_DETECTION_IN)
    detection_to_segmentation_q = mp.Queue(maxsize=MAX_SIZE_SEGMENTATION_IN)
    segmentation_to_geo_q       = mp.Queue(maxsize=MAX_SIZE_GEO_IN)
    geo_to_danger_q             = mp.Queue(maxsize=MAX_SIZE_DANGER_DETECTION_RESULT)
    danger_to_annotation_q      = mp.Queue(maxsize=MAX_SIZE_DANGER_DETECTION_RESULT)
    annotation_to_alert_q       = mp.Queue(maxsize=MAX_SIZE_NOTIFICATIONS_STREAM)
    annotation_to_video_q       = mp.Queue(maxsize=MAX_SIZE_VIDEO_STREAM)

    # ============== CPU AFFINITY ASSIGNMENTS ==============
    # i7-12850HX: 8 P-cores (logical 0-15, HT pairs) + 8 E-cores (logical 16-23, no HT).
    # ============== BUILD PROCESS CONFIGS ==============

    video_reader_config = StreamVideoReaderConfig(
        video_stream_url=s.video_stream_reader_url,
        connect_open_timeout_s=VIDEO_STREAM_READER_CONNECTION_OPEN_TIMEOUT_S,
        connect_retry_delay_s=VIDEO_STREAM_READER_RECONNECT_DELAY,
        frame_read_timeout_s=VIDEO_STREAM_READER_FRAME_READ_TIMEOUT_S,
        frame_read_retry_delay_s=VIDEO_STREAM_READER_FRAME_RETRY_DELAY,
        frame_read_max_consecutive_failures=VIDEO_STREAM_READER_FRAME_MAX_CONSECUTIVE_FAILURES,
        expected_aspect_ratio=VIDEO_STREAM_READER_EXPECTED_ASPECT_RATIO,
        processing_shape=VIDEO_STREAM_READER_PROCESSING_SHAPE,
        queue_timeout=PIPELINE_QUEUE_TIMEOUT,
    )

    combiner_config = FrameTelemetryCombinerConfig(
        mqtt_protocol=s.telemetry_listener_protocol,
        mqtt_broker_host=s.telemetry_listener_host,
        mqtt_broker_port=s.telemetry_listener_port,
        # The one credential this container holds, reused rather than a second
        # secret: Mosquitto's ACL plugin authorises it against this exact stream.
        mqtt_username=s.publisher_token.get_secret_value(),
        mqtt_password=s.publisher_token.get_secret_value(),
        mqtt_topic_prefix=s.telemetry_listener_stream_key,
        mqtt_qos_level=s.telemetry_listener_qos_level,
        mqtt_max_msg_wait_s=TELEMETRY_LISTENER_MSG_WAIT_TIMEOUT,
        mqtt_reconnect_delay_s=TELEMETRY_LISTENER_RECONNECT_DELAY,
        mqtt_ca_certs_path=(
            str(mqtt_certificates_dir)
            if s.telemetry_listener_protocol == "mqtts"
            else None
        ),
        mqtt_max_incoming_messages=TELEMETRY_LISTENER_MAX_INCOMING_MESSAGES,
        telemetry_buffer_max_size=FRAMETELCOMB_MAX_TELEM_BUFFER_SIZE,
        max_time_diff_s=FRAMETELCOMB_MAX_TIME_DIFF,
        queue_timeout=PIPELINE_QUEUE_TIMEOUT,
    )

    # Resolve detector checkpoint: use TensorRT engine if present, otherwise .pt from yaml.
    _yaml_det_checkpoint = detection_args.pop("model_checkpoint")
    _det_engine_path = Path("engine") / (Path(_yaml_det_checkpoint).stem + ".engine")
    if _det_engine_path.is_file():
        _det_checkpoint = str(_det_engine_path)
        logger.info(f"TensorRT engine found at {_det_engine_path}. Using engine for detector.")
    else:
        _det_checkpoint = _yaml_det_checkpoint
        logger.info(f"No TensorRT engine at {_det_engine_path}. Using .pt checkpoint for detector.")

    detection_config    = DetectionWorkerConfig(
        model_checkpoint=_det_checkpoint,
        predict_args=detection_args,
        queue_timeout=PIPELINE_QUEUE_TIMEOUT,
    )
    # Resolve segmentation checkpoint: use TensorRT engine if present, otherwise .onnx from yaml.
    _yaml_seg_checkpoint = segmentation_args.pop("model_checkpoint")
    _seg_engine_path = Path("engine") / (Path(_yaml_seg_checkpoint).stem + ".engine")
    if _seg_engine_path.is_file():
        _seg_checkpoint = str(_seg_engine_path)
        logger.info(f"TensorRT engine found at {_seg_engine_path}. Using engine for segmentation.")
    else:
        _seg_checkpoint = _yaml_seg_checkpoint
        logger.info(f"No TensorRT engine at {_seg_engine_path}. Using .onnx checkpoint for segmentation.")

    segmentation_config = SegmentationWorkerConfig(
        model_checkpoint=_seg_checkpoint,
        predict_args=segmentation_args,
        queue_timeout=PIPELINE_QUEUE_TIMEOUT,
    )

    geo_config = GeoWorkerConfig(
        input_args={
            "dem":                   str(dem_path),
            "dem_mask":              str(dem_mask_path),
            "safety_radius_m":       s.safety_radius_m,
            "slope_angle_threshold": s.slope_angle_threshold,
            "geofencing_vertexes":   s.geofencing_vertexes,
        },
        drone_args={
            "true_focal_len_mm":    s.drone_true_focal_len_mm,
            "sensor_width_mm":      s.drone_sensor_width_mm,
            "sensor_height_mm":     s.drone_sensor_height_mm,
            "sensor_width_pixels":  s.drone_sensor_width_pixels,
            "sensor_height_pixels": s.drone_sensor_height_pixels,
        },
        queue_timeout=PIPELINE_QUEUE_TIMEOUT,
    )

    danger_worker_config = DangerWorkerConfig(
        queue_timeout=PIPELINE_QUEUE_TIMEOUT,
    )

    annotation_worker_config = AnnotationWorkerConfig(
        alerts_cooldown_s=s.alerts_cooldown_seconds,
        queue_timeout=PIPELINE_QUEUE_TIMEOUT,
    )

    alert_writer_config = NotificationsStreamWriterConfig(
        alerts_jpeg_quality=s.alerts_jpeg_compression_quality,
        alerts_max_consecutive_failures=ALERTS_MAX_CONSECUTIVE_FAILURES,
        queue_get_timeout=ALERTS_QUEUE_GET_TIMEOUT,
        log_file_path=str(output_dir / f"{session_ts}.log"),
        flight_id=s.flight_id,
        publisher_token=s.publisher_token.get_secret_value(),
        ws_server_url=s.ws_server_url,
        db_writer_url=s.db_writer_url,
    )

    video_producer_config = VideoProducerProcessConfig(
        fps=FPS,
        queue_timeout=PIPELINE_QUEUE_TIMEOUT,
        media_server_url=s.video_out_stream_url,
        stream_manager_queue_max_size=MAX_SIZE_VIDEO_STREAM,
        stream_manager_ffmpeg_startup_timeout=VIDEO_OUT_STREAM_FFMPEG_STARTUP_TIMEOUT,
        stream_manager_ffmpeg_shutdown_timeout=VIDEO_OUT_STREAM_FFMPEG_SHUTDOWN_TIMEOUT,
        stream_manager_startup_timeout=VIDEO_OUT_STREAM_STARTUP_TIMEOUT,
        stream_manager_shutdown_timeout=VIDEO_OUT_STREAM_SHUTDOWN_TIMEOUT,
    )

    # ============== INSTANTIATE PROCESSES ==============

    try:
        video_reader_process = StreamVideoReader(
            output_meta_queue=reader_to_combiner_q,
            output_frame_buffer=reader_to_combiner_buf,
            error_event=error_event,
            config=video_reader_config,
        )

        combiner_process = FrameTelemetryCombiner(
            input_meta_queue=reader_to_combiner_q,
            input_frame_buffer=reader_to_combiner_buf,
            output_meta_queue=combiner_to_detection_q,
            output_frame_buffer=combiner_to_detection_buf,
            error_event=error_event,
            config=combiner_config,
        )

        detection_process = DetectionWorker(
            input_meta_queue=combiner_to_detection_q,
            input_frame_buffer=combiner_to_detection_buf,
            output_meta_queue=detection_to_segmentation_q,
            output_frame_buffer=detection_to_segmentation_buf,
            error_event=error_event,
            config=detection_config,
        )

        segmentation_process = SegmentationWorker(
            input_meta_queue=detection_to_segmentation_q,
            input_frame_buffer=detection_to_segmentation_buf,
            output_meta_queue=segmentation_to_geo_q,
            output_frame_buffer=segmentation_to_geo_buf,
            error_event=error_event,
            config=segmentation_config,
        )

        geo_process = GeoWorker(
            input_meta_queue=segmentation_to_geo_q,
            input_frame_buffer=segmentation_to_geo_buf,
            output_meta_queue=geo_to_danger_q,
            output_frame_buffer=geo_to_danger_buf,
            error_event=error_event,
            config=geo_config,
        )

        danger_process = DangerWorker(
            input_meta_queue=geo_to_danger_q,
            input_frame_buffer=geo_to_danger_buf,
            output_meta_queue=danger_to_annotation_q,
            output_frame_buffer=danger_to_annotation_buf,
            error_event=error_event,
            config=danger_worker_config,
        )

        annotation_process = AnnotationWorker(
            input_meta_queue=danger_to_annotation_q,
            input_frame_buffer=danger_to_annotation_buf,
            alert_output_meta_queue=annotation_to_alert_q,
            alert_output_frame_buffer=annotation_to_alert_buf,
            video_output_meta_queue=annotation_to_video_q,
            video_output_frame_buffer=annotation_to_video_buf,
            error_event=error_event,
            config=annotation_worker_config,
        )

        alert_writer_process = NotificationsStreamWriter(
            input_meta_queue=annotation_to_alert_q,
            input_frame_buffer=annotation_to_alert_buf,
            error_event=error_event,
            config=alert_writer_config,
        )

        video_producer_process = VideoProducerProcess(
            input_meta_queue=annotation_to_video_q,
            input_frame_buffer=annotation_to_video_buf,
            error_event=error_event,
            config=video_producer_config,
        )

    except Exception as e:
        logger.critical(f"Failed to instantiate one of the processes: {e}", exc_info=True)
        for buf in frame_buffers:
            buf.close()
            buf.unlink()
        return

    # ============== START PROCESSES (REVERSE ORDER) ==============
    # Start downstream consumers first so they are ready before producers push data.

    processes = [
        video_reader_process,
        combiner_process,
        detection_process,
        segmentation_process,
        geo_process,
        danger_process,
        annotation_process,
        alert_writer_process,
        video_producer_process,
    ]

    # ============== SIGNAL HANDLING ==============
    # This is a long-running service: an operator or the orchestrator (container
    # stop, pod eviction) tears it down with SIGTERM, which Python does NOT turn
    # into KeyboardInterrupt. Without a handler the default disposition kills this
    # process outright, so nothing runs its cleanup and the shared memory segments
    # are never unlinked.
    #
    # Armed BEFORE the children are started, so a signal arriving during the
    # multi-second model-loading startup is still handled. Each child drops the
    # handlers it inherits at fork (reset_child_signal_handlers, first statement of
    # every run()), which is what keeps p.terminate() working as the force-kill
    # escalation below.
    #
    # The handler only records the signal: calling error_event.set() here could
    # deadlock, because mp.Event.set() takes a lock that the interrupted main
    # thread may already hold. The monitor loop below performs the actual set.
    shutdown_signals: list[int] = []
    signal.signal(signal.SIGTERM, lambda signum, _frame: shutdown_signals.append(signum))
    signal.signal(signal.SIGINT, lambda signum, _frame: shutdown_signals.append(signum))

    try:
        for p in reversed(processes):
            # Bail out rather than spend seconds loading more models onto the GPU
            # only to tear them straight back down; under Kubernetes that can eat
            # the whole termination grace period and get us SIGKILLed mid-cleanup.
            if shutdown_signals:
                logger.warning(
                    f"{signal.Signals(shutdown_signals[0]).name} received during startup. "
                    "Aborting the start sequence."
                )
                break
            p.start()
            sleep(1.0)
    except Exception as e:
        logger.critical(f"Failed to start one of the processes: {e}", exc_info=True)
        error_event.set()

    # ============== MONITOR ==============

    try:
        while True:
            if shutdown_signals:
                logger.info(
                    f"{signal.Signals(shutdown_signals[0]).name} received. "
                    "Shutting down pipeline."
                )
                error_event.set()
                break
            all_finished = all(p.work_finished.is_set() for p in processes)
            if all_finished or error_event.is_set():
                if error_event.is_set():
                    logger.error("Error event set. Terminating chain.")
                else:
                    logger.info("All processes finished. Cleaning up.")
                break
            sleep(0.5)
    except KeyboardInterrupt:
        # Safety net for a SIGINT arriving before the handler above was installed.
        logger.info("KeyboardInterrupt received. Shutting down pipeline.")
        error_event.set()
    except Exception as e:
        error_event.set()
        logger.critical(f"Unexpected error in main process: {e}", exc_info=True)

    logger.info("Granting 5s for all processes to conclude cleanly.")
    sleep(5.0)

    for p in processes:
        # Skip anything never started: Process.join() asserts on an unstarted process.
        if p.pid is None:
            logger.info(f"{p.name} was never started. Nothing to join.")
            continue
        if p.is_alive():
            logger.warning(
                f"{p.name} still running after grace period "
                f"(work_finished={p.work_finished.is_set()}). Terminating."
            )
            p.terminate()
        # Bounded join, then escalate to SIGKILL. An unbounded join would hang the
        # whole teardown on a single process wedged in an uninterruptible call
        # (e.g. a stuck FFmpeg pipe), which under Kubernetes means waiting out
        # terminationGracePeriodSeconds before the pod is killed anyway.
        p.join(timeout=PROCESS_JOIN_TIMEOUT)
        if p.is_alive():
            logger.error(f"{p.name} ignored SIGTERM. Sending SIGKILL.")
            p.kill()
            p.join(timeout=PROCESS_JOIN_TIMEOUT)
        logger.info(f"{p.name} joined.")

    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.synchronize()
            torch.cuda.empty_cache()
            logger.info("CUDA context flushed.")
    except Exception as e:
        logger.warning(f"CUDA cleanup failed (non-fatal): {e}")

    for buf in frame_buffers:
        buf.close()
        buf.unlink()
    logger.info("Shared memory freed. Pipeline shut down.")


if __name__ == "__main__":
    main()

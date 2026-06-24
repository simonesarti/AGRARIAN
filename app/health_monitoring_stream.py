import multiprocessing as mp
from datetime import datetime
from pathlib import Path
from time import sleep
import logging

from app.shared.processes.app_settings import AppSettings
from app.shared.processes.stream_video_reader import StreamVideoReader, StreamVideoReaderConfig
from app.health_monitoring.processes.hm_tracking_worker import HMTrackingWorker, HMTrackingWorkerConfig
from app.health_monitoring.processes.hm_anomaly_worker import HMAnomalyDetectionWorker, HMAnomalyDetectionWorkerConfig
from app.health_monitoring.processes.hm_interpolator import HMVideoInterpolatorProcess, HMVideoInterpolatorConfig
from app.health_monitoring.processes.hm_annotation_worker import HMAnnotationWorker, HMAnnotationWorkerConfig
from app.shared.processes.output_alert_streamer import NotificationsStreamWriter, NotificationsStreamWriterConfig
from app.shared.processes.output_video_streamer import VideoProducerProcess, VideoProducerProcessConfig
from app.shared.processes.frame_buffer import FrameBuffer
from app.shared.processes.constants import (
    LOCAL_OUTPUT_DIR,
    VIDEO_STREAM_READER_PROCESSING_SHAPE,
    VIDEO_STREAM_READER_ORIGINAL_SHAPE,
    MAX_SIZE_NOTIFICATIONS_STREAM,
    MAX_SIZE_VIDEO_STREAM,
    FPS,
    POISON_PILL_TIMEOUT,
    PIPELINE_QUEUE_TIMEOUT,
    VIDEO_STREAM_READER_CONNECTION_OPEN_TIMEOUT_S,
    VIDEO_STREAM_READER_RECONNECT_DELAY,
    VIDEO_STREAM_READER_MAX_CONSECUTIVE_CONNECTION_FAILURES,
    VIDEO_STREAM_READER_FRAME_READ_TIMEOUT_S,
    VIDEO_STREAM_READER_FRAME_RETRY_DELAY,
    VIDEO_STREAM_READER_EXPECTED_ASPECT_RATIO,
    VIDEO_STREAM_READER_FRAME_MAX_CONSECUTIVE_FAILURES,
    ALERTS_QUEUE_GET_TIMEOUT,
    ALERTS_MAX_CONSECUTIVE_FAILURES,
    VIDEO_OUT_STREAM_FFMPEG_STARTUP_TIMEOUT,
    VIDEO_OUT_STREAM_FFMPEG_SHUTDOWN_TIMEOUT,
    VIDEO_OUT_STREAM_STARTUP_TIMEOUT,
    VIDEO_OUT_STREAM_SHUTDOWN_TIMEOUT,
)
from app.health_monitoring.inference.config import FeaturesConfig, AnomalyConfig, ModelConfig
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

    _engine_path = Path("engine/detection_1280_720_yolo11m.engine")
    _use_engine  = _engine_path.is_file()
    _anomaly_cfg_file = "configs/health_monitoring/anomaly_detector_5fps.yaml"

    try:
        tracking_args          = read_yaml_config("configs/health_monitoring/tracker.yaml")
        anomaly_detection_args = read_yaml_config(_anomaly_cfg_file)
    except Exception as e:
        logger.critical(f"Failed to load models configs: {e}", exc_info=True)
        exit(1)
    
    output_dir = Path(LOCAL_OUTPUT_DIR)
    output_dir.mkdir(exist_ok=True, parents=True)

    session_ts = datetime.now().strftime('%Y%m%d_%H%M%S')

    # ============== EVENTS ==============

    error_event = mp.Event()

    # ============== FRAME BUFFER SHAPES ==============
    # Shapes are (H, W, C) — NumPy convention.
    # All stages pass 3-channel BGR frames; only the resolution changes at annotation.

    _proc_h, _proc_w = VIDEO_STREAM_READER_PROCESSING_SHAPE[1], VIDEO_STREAM_READER_PROCESSING_SHAPE[0]
    _orig_h, _orig_w = VIDEO_STREAM_READER_ORIGINAL_SHAPE[1],   VIDEO_STREAM_READER_ORIGINAL_SHAPE[0]

    _3ch = (_proc_h, _proc_w, 3)
    _ann = (_orig_h, _orig_w, 3)

    # Resolve tracker checkpoint; frame_skip comes from the anomaly detector config.
    # frame_skip must be extracted before buffers/queues are sized from it.
    _yaml_checkpoint = tracking_args.pop("model_checkpoint")
    _frame_skip = anomaly_detection_args.pop("frame_skip")

    # ============== FRAME BUFFERS ==============
    # n_slots matches the corresponding metadata queue maxsize: each queue entry
    # holds exactly one slot index, so queue capacity == max in-flight frames.
    # 2*_frame_skip + 3: holds two full groups (K + frame_skip passthrough frames each)
    # plus one slot of headroom to avoid stalling while the interpolator flushes a burst.
    _slot_count = 2 * _frame_skip + 3

    reader_to_tracking_buf         = FrameBuffer(_3ch, n_slots=_slot_count)
    tracking_to_anomaly_buf        = FrameBuffer(_3ch, n_slots=_slot_count)
    anomaly_to_interpolator_buf    = FrameBuffer(_3ch, n_slots=_slot_count)
    interpolator_to_annotation_buf = FrameBuffer(_3ch, n_slots=_slot_count)
    annotation_to_alert_buf        = FrameBuffer(_ann, n_slots=MAX_SIZE_NOTIFICATIONS_STREAM)
    annotation_to_video_buf        = FrameBuffer(_ann, n_slots=_slot_count)

    frame_buffers = [
        reader_to_tracking_buf,
        tracking_to_anomaly_buf,
        anomaly_to_interpolator_buf,
        interpolator_to_annotation_buf,
        annotation_to_alert_buf,
        annotation_to_video_buf,
    ]

    # ============== METADATA QUEUES ==============

    reader_to_tracking_q         = mp.Queue(maxsize=_slot_count)
    tracking_to_anomaly_q        = mp.Queue(maxsize=_slot_count)
    anomaly_to_interpolator_q    = mp.Queue(maxsize=_slot_count)
    interpolator_to_annotation_q = mp.Queue(maxsize=_slot_count)
    annotation_to_alert_q        = mp.Queue(maxsize=MAX_SIZE_NOTIFICATIONS_STREAM)
    annotation_to_video_q        = mp.Queue(maxsize=_slot_count)

    # ============== BUILD PROCESS CONFIGS ==============

    if _use_engine:
        _tracking_checkpoint = str(_engine_path)
        logger.info(f"TensorRT engine found at {_engine_path}. Using engine (frame_skip={_frame_skip}).")
    else:
        _tracking_checkpoint = _yaml_checkpoint
        logger.info(f"No TensorRT engine at {_engine_path}. Using .pt checkpoint (frame_skip={_frame_skip}).")

    video_reader_config = StreamVideoReaderConfig(
        video_stream_url=s.video_stream_reader_url,
        connect_open_timeout_s=VIDEO_STREAM_READER_CONNECTION_OPEN_TIMEOUT_S,
        connect_retry_delay_s=VIDEO_STREAM_READER_RECONNECT_DELAY,
        connect_max_consecutive_failures=VIDEO_STREAM_READER_MAX_CONSECUTIVE_CONNECTION_FAILURES,
        frame_read_timeout_s=VIDEO_STREAM_READER_FRAME_READ_TIMEOUT_S,
        frame_read_retry_delay_s=VIDEO_STREAM_READER_FRAME_RETRY_DELAY,
        frame_read_max_consecutive_failures=VIDEO_STREAM_READER_FRAME_MAX_CONSECUTIVE_FAILURES,
        expected_aspect_ratio=VIDEO_STREAM_READER_EXPECTED_ASPECT_RATIO,
        processing_shape=VIDEO_STREAM_READER_PROCESSING_SHAPE,
        queue_timeout=PIPELINE_QUEUE_TIMEOUT,
        poison_pill_timeout=POISON_PILL_TIMEOUT,
    )

    assert isinstance(_tracking_checkpoint, str), "tracking checkpoint must be a non-empty string"
    tracking_config = HMTrackingWorkerConfig(
        model_checkpoint=_tracking_checkpoint,
        track_kwargs=tracking_args,
        frame_skip=_frame_skip,
        queue_timeout=PIPELINE_QUEUE_TIMEOUT,
        poison_pill_timeout=POISON_PILL_TIMEOUT,
    )

    anomaly_config = HMAnomalyDetectionWorkerConfig(
        features_cfg=FeaturesConfig(**anomaly_detection_args.get("features", {})),
        anomaly_cfg=AnomalyConfig(
            use_ae=s.hm_anomaly_use_ae,
            use_social=s.hm_anomaly_use_social,
            ae_threshold=s.hm_anomaly_ae_threshold,
            social_threshold=s.hm_anomaly_social_threshold,
            smoothing_window=s.hm_anomaly_smoothing_window,
            min_anomaly_duration=s.hm_anomaly_min_anomaly_duration,
            social_ema_alpha=s.hm_anomaly_social_ema_alpha,
            social_min_updates=s.hm_anomaly_social_min_updates,
            social_min_herd=s.hm_anomaly_social_min_herd,
            require_both=s.hm_anomaly_require_both,
        ),
        model_cfg=ModelConfig(**anomaly_detection_args.get("model", {})),
        weights_path=anomaly_detection_args.get("weights_path"),
        device=anomaly_detection_args.get("device"),
        queue_timeout=PIPELINE_QUEUE_TIMEOUT,
        poison_pill_timeout=POISON_PILL_TIMEOUT,
    )

    interpolator_config = HMVideoInterpolatorConfig(
        queue_timeout=PIPELINE_QUEUE_TIMEOUT,
        poison_pill_timeout=POISON_PILL_TIMEOUT,
    )

    annotation_config = HMAnnotationWorkerConfig(
        queue_timeout=PIPELINE_QUEUE_TIMEOUT,
        poison_pill_timeout=POISON_PILL_TIMEOUT,
    )

    alert_writer_config = NotificationsStreamWriterConfig(
        alerts_cooldown_s=s.alerts_cooldown_seconds,
        alerts_jpeg_quality=s.alerts_jpeg_compression_quality,
        alerts_max_consecutive_failures=ALERTS_MAX_CONSECUTIVE_FAILURES,
        queue_get_timeout=ALERTS_QUEUE_GET_TIMEOUT,
        log_file_path=str(output_dir / f"{session_ts}.log"),
        ws_server_url=s.ws_server_url,
        db_writer_url=s.db_writer_url,
        database_username=s.db_username,
        database_password=s.db_password.get_secret_value(),
        video_stream_url=s.video_out_stream_url,
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
            output_meta_queue=reader_to_tracking_q,
            output_frame_buffer=reader_to_tracking_buf,
            error_event=error_event,
            config=video_reader_config,
        )

        tracking_process = HMTrackingWorker(
            input_meta_queue=reader_to_tracking_q,
            input_frame_buffer=reader_to_tracking_buf,
            output_meta_queue=tracking_to_anomaly_q,
            output_frame_buffer=tracking_to_anomaly_buf,
            error_event=error_event,
            config=tracking_config,
        )

        anomaly_process = HMAnomalyDetectionWorker(
            input_meta_queue=tracking_to_anomaly_q,
            input_frame_buffer=tracking_to_anomaly_buf,
            output_meta_queue=anomaly_to_interpolator_q,
            output_frame_buffer=anomaly_to_interpolator_buf,
            error_event=error_event,
            config=anomaly_config,
        )

        interpolator_process = HMVideoInterpolatorProcess(
            input_meta_queue=anomaly_to_interpolator_q,
            input_frame_buffer=anomaly_to_interpolator_buf,
            output_meta_queue=interpolator_to_annotation_q,
            output_frame_buffer=interpolator_to_annotation_buf,
            error_event=error_event,
            config=interpolator_config,
        )

        annotation_process = HMAnnotationWorker(
            input_meta_queue=interpolator_to_annotation_q,
            input_frame_buffer=interpolator_to_annotation_buf,
            alert_output_meta_queue=annotation_to_alert_q,
            alert_output_frame_buffer=annotation_to_alert_buf,
            video_output_meta_queue=annotation_to_video_q,
            video_output_frame_buffer=annotation_to_video_buf,
            error_event=error_event,
            config=annotation_config,
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
        tracking_process,
        anomaly_process,
        interpolator_process,
        annotation_process,
        alert_writer_process,
        video_producer_process,
    ]

    try:
        for p in reversed(processes):
            p.start()
            sleep(1.0)
    except Exception as e:
        logger.critical(f"Failed to start one of the processes: {e}", exc_info=True)
        error_event.set()

    # ============== MONITOR ==============

    try:
        while True:
            all_finished = all(p.work_finished.is_set() for p in processes)
            if all_finished or error_event.is_set():
                if error_event.is_set():
                    logger.error("Error event set. Terminating chain.")
                else:
                    logger.info("All processes finished. Cleaning up.")
                break
            sleep(0.5)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received. Shutting down pipeline.")
        error_event.set()
    except Exception as e:
        error_event.set()
        logger.critical(f"Unexpected error in main process: {e}", exc_info=True)

    logger.info("Granting 5s for all processes to conclude cleanly.")
    sleep(5.0)

    for p in processes:
        if p.is_alive():
            logger.warning(
                f"{p.name} still running after grace period "
                f"(work_finished={p.work_finished.is_set()}). Terminating."
            )
            p.terminate()
        p.join()
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

import multiprocessing as mp
from datetime import datetime
from pathlib import Path
from time import sleep
import logging

from src.shared.processes.app_settings import AppSettings
from src.shared.processes.stream_video_reader import StreamVideoReader, StreamVideoReaderConfig
from src.health_monitoring.processes.hm_tracking_worker import HMTrackingWorker, HMTrackingWorkerConfig
from src.health_monitoring.processes.hm_anomaly_worker import HMAnomalyDetectionWorker, HMAnomalyDetectionWorkerConfig
from src.health_monitoring.processes.hm_annotation_worker import HMAnnotationWorker, HMAnnotationWorkerConfig
from src.shared.processes.output_alert_streamer import NotificationsStreamWriter, NotificationsStreamWriterConfig
from src.shared.processes.output_video_streamer import VideoProducerProcess, VideoProducerProcessConfig
from src.shared.processes.video_storage_manager import (
    AzureBlobStorageConfig,
    AzureBlobStoragePersistenceProcess,
    S3StorageConfig,
    S3StoragePersistenceProcess,
    LocalStorageConfig,
    LocalStoragePersistenceProcess,
)
from src.shared.processes.frame_buffer import FrameBuffer
from src.shared.processes.constants import (
    LOCAL_OUTPUT_DIR,
    VIDEO_STREAM_READER_PROCESSING_SHAPE,
    VIDEO_STREAM_READER_ORIGINAL_SHAPE,
    MAX_SIZE_FRAME_READER_OUT,
    MAX_SIZE_DETECTION_IN,
    MAX_SIZE_DANGER_DETECTION_RESULT,
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
    WS_MANAGER_PING_INTERVAL,
    WS_MANAGER_PING_TIMEOUT,
    WS_MANAGER_BROADCAST_TIMEOUT,
    WS_MANAGER_THREAD_CLOSE_TIMEOUT,
    DB_MANAGER_POOL_SIZE,
    DB_MANAGER_MAX_OVERFLOW,
    DB_MANAGER_QUEUE_WAIT_TIMEOUT,
    DB_MANAGER_THREAD_CLOSE_TIMEOUT,
    DB_MANAGER_QUEUE_SIZE,
    VIDEO_OUT_STREAM_FFMPEG_STARTUP_TIMEOUT,
    VIDEO_OUT_STREAM_FFMPEG_SHUTDOWN_TIMEOUT,
    VIDEO_OUT_STREAM_STARTUP_TIMEOUT,
    VIDEO_OUT_STREAM_SHUTDOWN_TIMEOUT,
    VIDEO_WRITER_HANDOFF_TIMEOUT,
    VIDEO_OUT_STORE_QUEUE_GET_TIMEOUT,
    VIDEO_OUT_STORE_MAX_UPLOAD_RETRIES,
    VIDEO_OUT_STORE_RETRY_BACKOFF_TIME,
)
from src.health_monitoring.inference.config import FeaturesConfig, AnomalyConfig, ModelConfig
from src.utils import read_yaml_config


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
        tracking_args          = read_yaml_config("configs/health_monitoring/tracker.yaml")
        anomaly_detection_args = read_yaml_config("configs/health_monitoring/anomaly_detector.yaml")
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

    # ============== FRAME BUFFERS ==============
    # n_slots matches the corresponding metadata queue maxsize: each queue entry
    # holds exactly one slot index, so queue capacity == max in-flight frames.

    reader_to_tracking_buf    = FrameBuffer(_3ch, n_slots=MAX_SIZE_FRAME_READER_OUT)
    tracking_to_anomaly_buf   = FrameBuffer(_3ch, n_slots=MAX_SIZE_DETECTION_IN)
    anomaly_to_annotation_buf = FrameBuffer(_3ch, n_slots=MAX_SIZE_DANGER_DETECTION_RESULT)
    annotation_to_alert_buf   = FrameBuffer(_ann, n_slots=MAX_SIZE_NOTIFICATIONS_STREAM)
    annotation_to_video_buf   = FrameBuffer(_ann, n_slots=MAX_SIZE_VIDEO_STREAM)

    frame_buffers = [
        reader_to_tracking_buf,
        tracking_to_anomaly_buf,
        anomaly_to_annotation_buf,
        annotation_to_alert_buf,
        annotation_to_video_buf,
    ]

    # ============== METADATA QUEUES ==============

    reader_to_tracking_q    = mp.Queue(maxsize=MAX_SIZE_FRAME_READER_OUT)
    tracking_to_anomaly_q   = mp.Queue(maxsize=MAX_SIZE_DETECTION_IN)
    anomaly_to_annotation_q = mp.Queue(maxsize=MAX_SIZE_DANGER_DETECTION_RESULT)
    annotation_to_alert_q   = mp.Queue(maxsize=MAX_SIZE_NOTIFICATIONS_STREAM)
    annotation_to_video_q   = mp.Queue(maxsize=MAX_SIZE_VIDEO_STREAM)
    video_to_persistence_q  = mp.Queue(maxsize=1)

    # ============== BUILD PROCESS CONFIGS ==============

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

    _tracking_checkpoint = tracking_args.pop("model_checkpoint")
    tracking_config = HMTrackingWorkerConfig(
        model_checkpoint=_tracking_checkpoint,
        track_kwargs=tracking_args,
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
        websocket_host=s.websocket_host,
        websocket_port=s.websocket_port,
        ws_ping_interval=WS_MANAGER_PING_INTERVAL,
        ws_ping_timeout=WS_MANAGER_PING_TIMEOUT,
        ws_broadcast_timeout=WS_MANAGER_BROADCAST_TIMEOUT,
        ws_thread_close_timeout=WS_MANAGER_THREAD_CLOSE_TIMEOUT,
        database_service=s.db_service,
        database_host=s.db_host,
        database_port=s.db_port,
        database_worker_name=s.db_worker_name,
        database_worker_password=s.db_worker_password.get_secret_value() if s.db_worker_password else None,
        database_username=s.db_username,
        database_password=s.db_password.get_secret_value(),
        db_pool_size=DB_MANAGER_POOL_SIZE,
        db_max_overflow=DB_MANAGER_MAX_OVERFLOW,
        db_queue_get_timeout=DB_MANAGER_QUEUE_WAIT_TIMEOUT,
        db_thread_close_timeout=DB_MANAGER_THREAD_CLOSE_TIMEOUT,
        db_alerts_queue_size=DB_MANAGER_QUEUE_SIZE,
        video_stream_url=s.video_out_stream_url,
    )

    video_producer_config = VideoProducerProcessConfig(
        fps=FPS,
        queue_timeout=PIPELINE_QUEUE_TIMEOUT,
        video_file_path=str(output_dir / f"{session_ts}.mp4"),
        media_server_url=s.video_out_stream_url,
        stream_manager_queue_max_size=MAX_SIZE_VIDEO_STREAM,
        stream_manager_ffmpeg_startup_timeout=VIDEO_OUT_STREAM_FFMPEG_STARTUP_TIMEOUT,
        stream_manager_ffmpeg_shutdown_timeout=VIDEO_OUT_STREAM_FFMPEG_SHUTDOWN_TIMEOUT,
        stream_manager_startup_timeout=VIDEO_OUT_STREAM_STARTUP_TIMEOUT,
        stream_manager_shutdown_timeout=VIDEO_OUT_STREAM_SHUTDOWN_TIMEOUT,
        storage_manager_handoff_timeout=VIDEO_WRITER_HANDOFF_TIMEOUT,
    )

    _persistence_base = dict(
        delete_local_on_success=s.video_out_store_delete_local_on_success,
        queue_get_timeout=VIDEO_OUT_STORE_QUEUE_GET_TIMEOUT,
        max_retries=VIDEO_OUT_STORE_MAX_UPLOAD_RETRIES,
        retry_backoff_s=VIDEO_OUT_STORE_RETRY_BACKOFF_TIME,
    )
    if s.video_out_store_service == "azure":
        assert s.video_out_store_azure_connection_string is not None
        video_persistence_config = AzureBlobStorageConfig(
            connection_string=s.video_out_store_azure_connection_string.get_secret_value(),
            container_name=s.video_out_store_azure_container_name,
            blob_prefix=s.video_out_store_azure_blob_prefix,
            **_persistence_base,
        )
        VideoPersistenceProcessClass = AzureBlobStoragePersistenceProcess
    elif s.video_out_store_service == "aws":
        video_persistence_config = S3StorageConfig(
            bucket_name=s.video_out_store_aws_bucket_name,
            key_prefix=s.video_out_store_aws_key_prefix,
            aws_access_key_id=s.video_out_store_aws_access_key_id,
            aws_secret_access_key=s.video_out_store_aws_secret_access_key.get_secret_value() if s.video_out_store_aws_secret_access_key else None,
            region_name=s.video_out_store_aws_region_name,
            **_persistence_base,
        )
        VideoPersistenceProcessClass = S3StoragePersistenceProcess
    else:
        video_persistence_config = LocalStorageConfig(
            target_directory=s.video_out_store_local_target_dir,
            **_persistence_base,
        )
        VideoPersistenceProcessClass = LocalStoragePersistenceProcess

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
            output_meta_queue=anomaly_to_annotation_q,
            output_frame_buffer=anomaly_to_annotation_buf,
            error_event=error_event,
            config=anomaly_config,
        )

        annotation_process = HMAnnotationWorker(
            input_meta_queue=anomaly_to_annotation_q,
            input_frame_buffer=anomaly_to_annotation_buf,
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
            output_queue=video_to_persistence_q,
            error_event=error_event,
            config=video_producer_config,
        )

        video_persistence_process = VideoPersistenceProcessClass(
            input_queue=video_to_persistence_q,
            error_event=error_event,
            config=video_persistence_config,
        )

    except Exception as e:
        logger.critical(f"Failed to instantiate one of the processes: {e}", exc_info=True)
        for buf in frame_buffers:
            buf.unlink()
        return

    # ============== START PROCESSES (REVERSE ORDER) ==============
    # Start downstream consumers first so they are ready before producers push data.

    processes = [
        video_reader_process,
        tracking_process,
        anomaly_process,
        annotation_process,
        alert_writer_process,
        video_producer_process,
        video_persistence_process,
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

    for buf in frame_buffers:
        buf.unlink()
    logger.info("Shared memory freed. Pipeline shut down.")


if __name__ == "__main__":
    main()

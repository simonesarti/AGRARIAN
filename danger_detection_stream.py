import multiprocessing as mp
from pathlib import Path
from time import sleep
import logging

from src.shared.processes.app_settings import AppSettings
from src.shared.processes.stream_video_reader import StreamVideoReader, StreamVideoReaderConfig
from src.shared.processes.frame_telemetry_combiner import FrameTelemetryCombiner, FrameTelemetryCombinerConfig
from src.danger_detection.processes.detection import DetectionWorker, DetectionWorkerConfig
from src.danger_detection.processes.segmentation import SegmentationWorker, SegmentationWorkerConfig
from src.danger_detection.processes.geo import GeoWorker, GeoWorkerConfig
from src.danger_detection.processes.danger_annotation import DangerAnnotationWorker, DangerAnnotationWorkerConfig
from src.shared.processes.output_alert_streamer import NotificationsStreamWriter, NotificationsStreamWriterConfig
from src.shared.processes.output_video_streamer import VideoProducerProcess, VideoProducerProcessConfig
from src.shared.processes.video_storage_manager import (
    AzureBlobStorageConfig, AzureBlobStoragePersistenceProcess,
    S3StorageConfig, S3StoragePersistenceProcess,
    LocalStorageConfig, LocalStoragePersistenceProcess,
)
from src.shared.processes.frame_buffer import FrameBuffer
from src.shared.processes.constants import ALERTS_FILE_NAME, LOCAL_OUTPUT_DIR, MQTTS, AZURE, AWS, LOCAL
from src.configs.utils import read_yaml_config


# ================================================================

logger = logging.getLogger("main")

if not logger.handlers:
    _handler = logging.FileHandler('./logs/main_dd.log', mode='w')
    _handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

# ================================================================


def _build_persistence_process(
        input_queue: mp.Queue,
        error_event,
        s: AppSettings,
):
    """Instantiate the right VideoPersistenceProcess subclass from settings."""
    base = dict(
        delete_local_on_success=s.video_out_store_delete_local_on_success,
        queue_get_timeout=s.video_out_store_queue_get_timeout,
        max_retries=s.video_out_store_max_upload_retries,
        retry_backoff_s=s.video_out_store_retry_backoff_time,
    )
    service = s.video_out_store_service

    if service == AZURE:
        config = AzureBlobStorageConfig(
            connection_string=s.video_out_store_azure_connection_string,
            container_name=s.video_out_store_azure_container_name,
            blob_prefix=s.video_out_store_azure_blob_prefix,
            **base,
        )
        return AzureBlobStoragePersistenceProcess(input_queue=input_queue, error_event=error_event, config=config)

    if service == AWS:
        config = S3StorageConfig(
            bucket_name=s.video_out_store_aws_bucket_name,
            key_prefix=s.video_out_store_aws_key_prefix,
            aws_access_key_id=s.video_out_store_aws_access_key_id,
            aws_secret_access_key=s.video_out_store_aws_secret_access_key,
            region_name=s.video_out_store_aws_region_name,
            **base,
        )
        return S3StoragePersistenceProcess(input_queue=input_queue, error_event=error_event, config=config)

    # LOCAL (default)
    config = LocalStorageConfig(
        target_directory=s.video_out_store_local_target_dir,
        **base,
    )
    return LocalStoragePersistenceProcess(input_queue=input_queue, error_event=error_event, config=config)


def main():

    try:
        s = AppSettings()
    except Exception as e:
        logger.critical(f"Configuration error: {e}", exc_info=True)
        exit(1)

    detection_args    = read_yaml_config("configs/danger_detection/detector.yaml")
    segmentation_args = read_yaml_config("configs/danger_detection/segmenter.yaml")

    dem_path           = Path("dem/dem.tif")
    dem_mask_path      = Path("dem/dem_mask.tif")
    output_dir         = Path(LOCAL_OUTPUT_DIR)
    output_dir.mkdir(exist_ok=True, parents=True)
    mqtt_certificates_dir = Path("certificates") / "mqtt"

    # ============== EVENTS ==============

    error_event = mp.Event()

    # ============== FRAME BUFFER SHAPES ==============
    # Shapes are (H, W, C) — NumPy convention

    _3ch = (s.video_stream_reader_processing_height, s.video_stream_reader_processing_width, 3)
    _5ch = (s.video_stream_reader_processing_height, s.video_stream_reader_processing_width, 5)
    _8ch = (s.video_stream_reader_processing_height, s.video_stream_reader_processing_width, 8)
    _ann = (s.video_original_height, s.video_original_width, 3)

    # ============== FRAME BUFFERS ==============
    # n_slots matches the corresponding metadata queue maxsize: each queue entry
    # holds exactly one slot index, so queue capacity == max in-flight frames.

    reader_to_combiner_buf        = FrameBuffer(_3ch, n_slots=s.max_size_frame_reader_out)
    combiner_to_detection_buf     = FrameBuffer(_3ch, n_slots=s.max_size_detection_in)
    detection_to_segmentation_buf = FrameBuffer(_3ch, n_slots=s.max_size_segmentation_in)
    segmentation_to_geo_buf       = FrameBuffer(_5ch, n_slots=s.max_size_geo_in)
    geo_to_annotation_buf         = FrameBuffer(_8ch, n_slots=s.max_size_danger_detection_result)
    annotation_to_alert_buf       = FrameBuffer(_ann, n_slots=s.max_size_notifications_stream)
    annotation_to_video_buf       = FrameBuffer(_ann, n_slots=s.max_size_video_stream)

    frame_buffers = [
        reader_to_combiner_buf,
        combiner_to_detection_buf,
        detection_to_segmentation_buf,
        segmentation_to_geo_buf,
        geo_to_annotation_buf,
        annotation_to_alert_buf,
        annotation_to_video_buf,
    ]

    # ============== METADATA QUEUES ==============

    reader_to_combiner_q        = mp.Queue(maxsize=s.max_size_frame_reader_out)
    combiner_to_detection_q     = mp.Queue(maxsize=s.max_size_detection_in)
    detection_to_segmentation_q = mp.Queue(maxsize=s.max_size_segmentation_in)
    segmentation_to_geo_q       = mp.Queue(maxsize=s.max_size_geo_in)
    geo_to_annotation_q         = mp.Queue(maxsize=s.max_size_danger_detection_result)
    annotation_to_alert_q       = mp.Queue(maxsize=s.max_size_notifications_stream)
    annotation_to_video_q       = mp.Queue(maxsize=s.max_size_video_stream)
    video_to_persistence_q      = mp.Queue(maxsize=s.max_size_video_storage)

    # ============== BUILD PROCESS CONFIGS ==============

    video_reader_config = StreamVideoReaderConfig(
        video_stream_url=s.video_stream_reader_url,
        connect_open_timeout_s=s.video_stream_reader_connection_open_timeout_s,
        connect_retry_delay_s=s.video_stream_reader_reconnect_delay,
        connect_max_consecutive_failures=s.video_stream_reader_max_consecutive_connection_failures,
        frame_read_timeout_s=s.video_stream_reader_frame_read_timeout_s,
        frame_read_retry_delay_s=s.video_stream_reader_frame_retry_delay,
        frame_read_max_consecutive_failures=s.video_stream_reader_frame_max_consecutive_failures,
        meta_queue_put_timeout=s.video_stream_reader_queue_put_timeout,
        poison_pill_timeout=s.poison_pill_timeout,
    )

    combiner_config = FrameTelemetryCombinerConfig(
        mqtt_protocol=s.telemetry_listener_protocol,
        mqtt_broker_host=s.telemetry_listener_host,
        mqtt_broker_port=s.telemetry_listener_port,
        mqtt_username=s.telemetry_listener_username,
        mqtt_password=s.telemetry_listener_password,
        mqtt_qos_level=s.telemetry_listener_qos_level,
        mqtt_max_msg_wait_s=s.telemetry_listener_msg_wait_timeout,
        mqtt_reconnect_delay_s=s.telemetry_listener_reconnect_delay,
        mqtt_ca_certs_path=(
            str(mqtt_certificates_dir)
            if s.telemetry_listener_protocol == MQTTS
            else None
        ),
        mqtt_max_incoming_messages=s.telemetry_listener_max_incoming_messages,
        telemetry_buffer_max_size=s.frametelcomb_max_telem_buffer_size,
        max_time_diff_s=s.frametelcomb_max_time_diff,
        queue_get_timeout=s.frametelcomb_queue_get_timeout,
        queue_put_timeout=s.frametelcomb_queue_put_timeout,
        poison_pill_timeout=s.poison_pill_timeout,
    )

    # Model configs are loaded from YAML; Pydantic validates checkpoint path etc.
    detection_config    = DetectionWorkerConfig(
        **detection_args,
        queue_get_timeout=s.models_queue_get_timeout,
        queue_put_timeout=s.models_queue_put_timeout,
        poison_pill_timeout=s.poison_pill_timeout,
    )
    segmentation_config = SegmentationWorkerConfig(
        **segmentation_args,
        queue_get_timeout=s.models_queue_get_timeout,
        queue_put_timeout=s.models_queue_put_timeout,
        poison_pill_timeout=s.poison_pill_timeout,
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
        queue_get_timeout=s.models_queue_get_timeout,
        queue_put_timeout=s.models_queue_put_timeout,
        poison_pill_timeout=s.poison_pill_timeout,
    )

    danger_annotation_config = DangerAnnotationWorkerConfig(
        queue_get_timeout=s.annotation_queue_get_timeout,
        queue_put_timeout=s.annotation_queue_put_timeout,
        poison_pill_timeout=s.poison_pill_timeout,
    )

    alert_writer_config = NotificationsStreamWriterConfig(
        alerts_cooldown_s=s.alerts_cooldown_seconds,
        alerts_jpeg_quality=s.alerts_jpeg_compression_quality,
        alerts_max_consecutive_failures=s.alerts_max_consecutive_failures,
        queue_get_timeout=s.alerts_queue_get_timeout,
        log_file_path=str(output_dir / ALERTS_FILE_NAME),
        websocket_host=s.websocket_host,
        websocket_port=s.websocket_port,
        ws_ping_interval=s.ws_manager_ping_interval,
        ws_ping_timeout=s.ws_manager_ping_timeout,
        ws_broadcast_timeout=s.ws_manager_broadcast_timeout,
        ws_thread_close_timeout=s.ws_manager_thread_close_timeout,
        database_service=s.db_service,
        database_host=s.db_host,
        database_port=s.db_port,
        database_worker_name=s.db_worker_name,
        database_worker_password=s.db_worker_password,
        database_username=s.db_username,
        database_password=s.db_password,
        db_pool_size=s.db_manager_pool_size,
        db_max_overflow=s.db_manager_max_overflow,
        db_queue_get_timeout=s.db_manager_queue_wait_timeout,
        db_thread_close_timeout=s.db_manager_thread_close_timeout,
        db_alerts_queue_size=s.db_manager_queue_size,
        video_stream_url=s.video_out_stream_url,
    )

    video_producer_config = VideoProducerProcessConfig(
        fps=s.fps,
        queue_get_timeout=s.video_writer_get_frame_timeout,
        video_output_dir=str(output_dir),
        media_server_url=s.video_out_stream_url,
        stream_manager_queue_max_size=s.max_size_video_stream,
        stream_manager_queue_get_timeout=s.video_out_stream_queue_get_timeout,
        stream_manager_ffmpeg_startup_timeout=s.video_out_stream_ffmpeg_startup_timeout,
        stream_manager_ffmpeg_shutdown_timeout=s.video_out_stream_ffmpeg_shutdown_timeout,
        stream_manager_startup_timeout=s.video_out_stream_startup_timeout,
        stream_manager_shutdown_timeout=s.video_out_stream_shutdown_timeout,
        storage_manager_handoff_timeout=s.video_writer_handoff_timeout,
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
            output_meta_queue=geo_to_annotation_q,
            output_frame_buffer=geo_to_annotation_buf,
            error_event=error_event,
            config=geo_config,
        )

        danger_annotation_process = DangerAnnotationWorker(
            input_meta_queue=geo_to_annotation_q,
            input_frame_buffer=geo_to_annotation_buf,
            alert_output_meta_queue=annotation_to_alert_q,
            alert_output_frame_buffer=annotation_to_alert_buf,
            video_output_meta_queue=annotation_to_video_q,
            video_output_frame_buffer=annotation_to_video_buf,
            error_event=error_event,
            config=danger_annotation_config,
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

        video_persistence_process = _build_persistence_process(
            input_queue=video_to_persistence_q,
            error_event=error_event,
            s=s,
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
        combiner_process,
        detection_process,
        segmentation_process,
        geo_process,
        danger_annotation_process,
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

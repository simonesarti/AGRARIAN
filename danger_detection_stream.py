from typing import Any
import os

import multiprocessing as mp

from src.danger_detection.processes.detection import DetectionWorker
from src.danger_detection.processes.segmentation import SegmentationWorker
from src.danger_detection.processes.geo import GeoWorker
from src.danger_detection.processes.result_aligmnent import ModelsAlignmentWorker
from src.danger_detection.processes.danger import DangerDetectionWorker
from src.danger_detection.processes.annotation import AnnotationWorker

from src.shared.processes.stream_video_reader import StreamVideoReader
from src.shared.processes.mqtt_telemetry_listener import MqttCollectorProcess
from src.shared.processes.frame_telemetry_combiner import FrameTelemetryCombiner
from src.shared.processes.output_video_streamer import VideoProducerProcess
from src.shared.processes.output_alert_streamer import NotificationsStreamWriter
from src.shared.processes.video_storage_manager import VideoPersistenceProcess

from src.shared.processes.constants import *
from src.shared.processes.process_env_vars import preprocess_env_vars
from src.configs.utils import read_yaml_config
from pathlib import Path

from time import sleep

import logging

# ================================================================

logger = logging.getLogger("main")

if not logger.handlers:  # Avoid duplicate handlers
    video_handler = logging.FileHandler('./logs/main_dd.log', mode='w')
    video_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(video_handler)
    logger.setLevel(logging.INFO)


# ================================================================

def main():

    # base_env_vars = {k:v for k,v in os.environ.items()}
    # logger.info(base_env_vars)
    
    try:
        env_vars = preprocess_env_vars()
    except Exception as e:
        logger.critical(f"Failed to process ENV variables {e}", exc_info=True)
        exit(1)

    # logger.info(env_vars)

    detection_args = read_yaml_config("configs/danger_detection/detector.yaml")
    segmentation_args = read_yaml_config("configs/danger_detection/segmenter.yaml")

    # DEM data is passed to the container as volume mapped into /app/dem/dem.tif,dem_mask.tif
    dem_path = Path("dem/dem.tif")
    dem_mask_path = Path("dem/dem_mask.tif")

    output_dir = Path(LOCAL_OUTPUT_DIR)
    output_dir.mkdir(exist_ok=True, parents=True)

    certificates_dir = Path("certificates")
    mqtt_certificates_dir = certificates_dir / "mqtt"

    geo_in_args = {
        "dem": dem_path,
        "dem_mask": dem_mask_path,
        "safety_radius_m": env_vars["SAFETY_RADIUS_M"],
        "slope_angle_threshold": env_vars["SLOPE_ANGLE_THRESHOLD"],
        "geofencing_vertexes": env_vars["GEOFENCING_VERTEXES"],

    }

    drone_args = {
        "true_focal_len_mm": env_vars["DRONE_TRUE_FOCAL_LEN_MM"],
        "sensor_width_mm": env_vars["DRONE_SENSOR_WIDTH_MM"],
        "sensor_height_mm": env_vars["DRONE_SENSOR_HEIGHT_MM"],
        "sensor_width_pixels": env_vars["DRONE_SENSOR_WIDTH_PIXELS"],
        "sensor_height_pixels": env_vars["DRONE_SENSOR_HEIGHT_PIXELS"],
    }

    # ============== SETUP EVENTS ===================================

    stop_event = mp.Event()
    error_event = mp.Event()

    # ============== SETUP QUEUES ===================================

    # LAYER 1 -> 2
    frame_reader_out_queue = mp.Queue(maxsize=env_vars["MAX_SIZE_FRAME_READER_OUT"])
    telemetry_reader_out_queue = mp.Queue(maxsize=env_vars["MAX_SIZE_TELEMETRY_READER_OUT"])
    # LAYER 2 -> 3
    detection_in_queue = mp.Queue(maxsize=env_vars["MAX_SIZE_DETECTION_IN"])
    segmentation_in_queue = mp.Queue(maxsize=env_vars["MAX_SIZE_SEGMENTATION_IN"])
    geo_in_queue = mp.Queue(maxsize=env_vars["MAX_SIZE_GEO_IN"])
    models_in_queues = [detection_in_queue, segmentation_in_queue, geo_in_queue]
    # LAYER 3 -> 4
    detection_results_queue = mp.Queue(maxsize=env_vars["MAX_SIZE_DETECTION_RESULTS"])
    segmentation_results_queue = mp.Queue(maxsize=env_vars["MAX_SIZE_SEGMENTATION_RESULTS"])
    geo_results_queue = mp.Queue(maxsize=env_vars["MAX_SIZE_GEO_RESULTS"])
    models_results_queues = [detection_results_queue, segmentation_results_queue, geo_results_queue]
    # LAYER 4 -> 5
    aligned_models_queue = mp.Queue(maxsize=env_vars["MAX_SIZE_MODELS_ALIGNMENT_RESULTS"])
    # LAYER 5 -> 6
    danger_detection_result_queue = mp.Queue(maxsize=env_vars["MAX_SIZE_DANGER_DETECTION_RESULT"])
    # LAYER 6 -> 7
    video_stream_queue = mp.Queue(maxsize=env_vars["MAX_SIZE_VIDEO_STREAM"])
    notifications_stream_queue = mp.Queue(maxsize=env_vars["MAX_SIZE_NOTIFICATIONS_STREAM"])
    # LAYER 7 -> 8
    video_storage_queue = mp.Queue(maxsize=env_vars["MAX_SIZE_VIDEO_STORAGE"])

    # ============== INSTANTIATE PROCESSES ===================================

    try:

        # Create StreamVideoReader process
        video_reader_process = StreamVideoReader(
            frame_queue=frame_reader_out_queue,
            stop_event=stop_event,
            error_event=error_event,
            video_stream_url=env_vars["VIDEO_STREAM_READER_URL"],
            connect_open_timeout_s=env_vars["VIDEO_STREAM_READER_CONNECTION_OPEN_TIMEOUT_S"],
            connect_retry_delay_s=env_vars["VIDEO_STREAM_READER_RECONNECT_DELAY"],
            connect_max_consecutive_failures=env_vars["VIDEO_STREAM_READER_MAX_CONSECUTIVE_CONNECTION_FAILURES"],
            frame_read_timeout_s=env_vars["VIDEO_STREAM_READER_FRAME_READ_TIMEOUT_S"],
            frame_read_retry_delay_s=env_vars["VIDEO_STREAM_READER_FRAME_RETRY_DELAY"],
            frame_read_max_consecutive_failures=env_vars["VIDEO_STREAM_READER_FRAME_MAX_CONSECUTIVE_FAILURES"],
            buffer_size=env_vars["VIDEO_STREAM_READER_BUFFER_SIZE"],
            expected_aspect_ratio=VIDEO_STREAM_READER_EXPECTED_ASPECT_RATIO,  # fixed
            processing_shape=VIDEO_STREAM_READER_PROCESSING_SHAPE,  # fixed
            queue_out_put_timeout=env_vars["VIDEO_STREAM_READER_QUEUE_PUT_TIMEOUT"],
            poison_pill_timeout=env_vars["POISON_PILL_TIMEOUT"],
        )

        # Create StreamTelemetryReader process
        telemetry_reader_process = MqttCollectorProcess(
            telemetry_queue=telemetry_reader_out_queue,
            stop_event=stop_event,
            error_event=error_event,
            protocol=env_vars["TELEMETRY_LISTENER_PROTOCOL"],
            broker_host=env_vars["TELEMETRY_LISTENER_HOST"],
            broker_port=env_vars["TELEMETRY_LISTENER_PORT"],
            username=env_vars["TELEMETRY_LISTENER_USERNAME"],
            password=env_vars["TELEMETRY_LISTENER_PASSWORD"],
            qos_level=env_vars["TELEMETRY_LISTENER_QOS_LEVEL"],
            ca_certs_file_path=str(mqtt_certificates_dir),  # fixed
            cert_validation=TELEMETRY_LISTENER_CERT_VALIDATION,  # fixed
            max_msg_wait=env_vars["TELEMETRY_LISTENER_MSG_WAIT_TIMEOUT"],
            reconnection_delay=env_vars["TELEMETRY_LISTENER_RECONNECT_DELAY"],
            max_incoming_messages=env_vars["TELEMETRY_LISTENER_MAX_INCOMING_MESSAGES"],
        )

        # Create Frame-Telemetry combiner process
        combiner_process = FrameTelemetryCombiner(
            frame_queue=frame_reader_out_queue,
            telemetry_queue=telemetry_reader_out_queue,
            output_queues=models_in_queues,
            error_event=error_event,
            telemetry_buffer_max_size=env_vars["FRAMETELCOMB_MAX_TELEM_BUFFER_SIZE"],
            max_time_diff_s=env_vars["FRAMETELCOMB_MAX_TIME_DIFF"],
            queue_get_timeout=env_vars["FRAMETELCOMB_QUEUE_GET_TIMEOUT"],
            queue_put_max_retries=env_vars["FRAMETELCOMB_QUEUE_PUT_MAX_RETRIES"],
            queue_put_backoff=env_vars["FRAMETELCOMB_QUEUE_PUT_BACKOFF"],
            poison_pill_backoff=env_vars["POISON_PILL_TIMEOUT"],
        )

        # Create DetectionWorker process
        detection_process = DetectionWorker(
            input_queue=detection_in_queue,
            result_queue=detection_results_queue,
            error_event=error_event,
            detection_args=detection_args,
            queue_get_timeout=env_vars["MODELS_QUEUE_GET_TIMEOUT"],
            queue_put_timeout=env_vars["MODELS_QUEUE_PUT_TIMEOUT"],
            poison_pill_timeout=env_vars["POISON_PILL_TIMEOUT"],

        )

        # Create DetectionWorker process
        segmentation_process = SegmentationWorker(
            input_queue=segmentation_in_queue,
            result_queue=segmentation_results_queue,
            error_event=error_event,
            segmentation_args=segmentation_args,
            queue_get_timeout=env_vars["MODELS_QUEUE_GET_TIMEOUT"],
            queue_put_timeout=env_vars["MODELS_QUEUE_PUT_TIMEOUT"],
            poison_pill_timeout=env_vars["POISON_PILL_TIMEOUT"],
        )

        # Create GeoWorker process
        geo_process = GeoWorker(
            input_queue=geo_in_queue,
            result_queue=geo_results_queue,
            error_event=error_event,
            input_args=geo_in_args,
            drone_args=drone_args,
            queue_get_timeout=env_vars["MODELS_QUEUE_GET_TIMEOUT"],
            queue_put_timeout=env_vars["MODELS_QUEUE_PUT_TIMEOUT"],
            poison_pill_timeout=env_vars["POISON_PILL_TIMEOUT"],
        )

        models_alignment_process = ModelsAlignmentWorker(
            input_queues=models_results_queues,
            result_queue=aligned_models_queue,
            error_event=error_event,
            queue_get_timeout=env_vars["MODELS_QUEUE_GET_TIMEOUT"],
            queue_put_timeout=env_vars["MODELS_QUEUE_PUT_TIMEOUT"],
            poison_pill_timeout=env_vars["POISON_PILL_TIMEOUT"],
        )

        # Create DangerIdentification process
        danger_identification_process = DangerDetectionWorker(
            input_queue=aligned_models_queue,
            result_queue=danger_detection_result_queue,
            error_event=error_event,
            queue_get_timeout=env_vars["MODELS_QUEUE_GET_TIMEOUT"],
            queue_put_timeout=env_vars["MODELS_QUEUE_PUT_TIMEOUT"],
            poison_pill_timeout=env_vars["POISON_PILL_TIMEOUT"],
        )

        # Create VideoAnnotatorWorker
        annotation_process = AnnotationWorker(
            input_queue=danger_detection_result_queue,
            video_stream_queue=video_stream_queue,
            alerts_stream_queue=notifications_stream_queue,
            error_event=error_event,
            alerts_cooldown_seconds=env_vars["ALERTS_COOLDOWN_SECONDS"],
            queue_get_timeout=env_vars["ANNOTATION_QUEUE_GET_TIMEOUT"],
            queue_put_timeout=env_vars["ANNOTATION_QUEUE_PUT_TIMEOUT"],
            max_put_alert_consecutive_failures=env_vars["ANNOTATION_MAX_PUT_ALERT_CONSECUTIVE_FAILURES"],
            max_put_video_consecutive_failures=env_vars["ANNOTATION_MAX_PUT_VIDEO_CONSECUTIVE_FAILURES"],
            poison_pill_timeout=env_vars["POISON_PILL_TIMEOUT"],
        )

        # ---------------------------------

        # Create VideoStreamWriter process
        video_writer_process = VideoProducerProcess(
            input_queue=video_stream_queue,
            output_queue=video_storage_queue,
            error_event=error_event,
            fps=env_vars["FPS"],
            poison_pill_timeout=env_vars["POISON_PILL_TIMEOUT"],
            local_video_name=str(output_dir / ANNOTATED_VIDEO_NAME),  # fixed
            video_codec=CODEC,  # fixed
            media_server_url=env_vars["VIDEO_OUT_STREAM_URL"],
            stream_manager_queue_max_size=env_vars["MAX_SIZE_VIDEO_STREAM"],
            stream_manager_queue_get_timeout=env_vars["VIDEO_OUT_STREAM_QUEUE_GET_TIMEOUT"],
            stream_manager_ffmpeg_startup_timeout=env_vars["VIDEO_OUT_STREAM_FFMPEG_STARTUP_TIMEOUT"],
            stream_manager_ffmpeg_shutdown_timeout=env_vars["VIDEO_OUT_STREAM_FFMPEG_SHUTDOWN_TIMEOUT"],
            stream_manager_startup_timeout=env_vars["VIDEO_OUT_STREAM_STARTUP_TIMEOUT"],
            stream_manager_shutdown_timeout=env_vars["VIDEO_OUT_STREAM_SHUTDOWN_TIMEOUT"],
            # -------- STORAGE_MANAGER ------------
            storage_manager_handoff_timeout=env_vars["VIDEO_WRITER_HANDOFF_TIMEOUT"],
        )

        # ---------------------------------

        # Create VideoStreamWriter process
        notification_writer_process = NotificationsStreamWriter(
            input_queue=notifications_stream_queue,
            error_event=error_event,
            alerts_get_timeout=env_vars["ALERTS_QUEUE_GET_TIMEOUT"],
            alerts_max_consecutive_failures=env_vars["ALERTS_MAX_CONSECUTIVE_FAILURES"],
            alerts_jpeg_quality=env_vars["ALERTS_JPEG_COMPRESSION_QUALITY"],
            log_file_path=str(output_dir / ALERTS_FILE_NAME),
            websocket_host=WEBSOCKET_HOST,  # fixed, runs locally
            websocket_port=WEBSOCKET_PORT,  # fixed, uses tls
            ws_manager_ping_interval=env_vars["WS_MANAGER_PING_INTERVAL"],
            ws_manager_ping_timeout=env_vars["WS_MANAGER_PING_TIMEOUT"],
            ws_manager_broadcast_timeout=env_vars["WS_MANAGER_BROADCAST_TIMEOUT"],
            ws_manager_thread_close_timeout=env_vars["WS_MANAGER_THREAD_CLOSE_TIMEOUT"],
            database_service=env_vars["DB_SERVICE"],
            database_worker_name=env_vars["DB_WORKER_NAME"],
            database_worker_password=env_vars["DB_WORKER_PASSWORD"],
            database_host=env_vars["DB_HOST"],
            database_port=env_vars["DB_PORT"],
            database_username=env_vars["DB_USERNAME"],
            database_password=env_vars["DB_PASSWORD"],
            db_manager_pool_size=env_vars["DB_MANAGER_POOL_SIZE"],
            db_manager_max_overflow=env_vars["DB_MANAGER_MAX_OVERFLOW"],
            db_manager_queue_get_timeout=env_vars["DB_MANAGER_QUEUE_WAIT_TIMEOUT"],
            db_manager_thread_close_timeout=env_vars["DB_MANAGER_THREAD_CLOSE_TIMEOUT"],
            db_manager_alerts_queue_size=env_vars["DB_MANAGER_QUEUE_SIZE"],
        )

        # ---------------------------------

        video_storage_process = VideoPersistenceProcess(
            input_queue=video_storage_queue,
            storage_url=env_vars["VIDEO_OUT_STORE_URL"],
            delete_local_on_success=env_vars["VIDEO_OUT_STORE_DELETE_LOCAL_ON_SUCCESS"],
            queue_get_timeout=env_vars["VIDEO_OUT_STORE_QUEUE_GET_TIMEOUT"],
            max_retries=env_vars["VIDEO_OUT_STORE_MAX_UPLOAD_RETRIES"],
            retry_backoff=env_vars["VIDEO_OUT_STORE_RETRY_BACKOFF_TIME"],
        )

    except Exception as e:
        logger.critical(f"Failed to instantiate one of the processes: {e}", exc_info=True)
        return

    # ============== START PROCESSES (REVERSE ORDER) ===================================

    try:

        # LAYER 8
        video_storage_process.start()
        sleep(1.0)

        # LAYER 7
        notification_writer_process.start()
        sleep(1.0)

        video_writer_process.start()
        sleep(1.0)

        # LAYER 6
        annotation_process.start()
        sleep(1.0)

        # LAYER 5
        danger_identification_process.start()
        sleep(1.0)

        # LAYER 4
        models_alignment_process.start()
        sleep(1.0)

        # LAYER 3
        geo_process.start()
        sleep(1.0)
        segmentation_process.start()
        sleep(1.0)
        detection_process.start()
        sleep(1.0)

        # LAYER 2
        combiner_process.start()
        sleep(1.0)

        # LAYER 1
        telemetry_reader_process.start()
        sleep(1.0)
        video_reader_process.start()
        sleep(1.0)

    except Exception as e:
        logger.critical(f"Failed to start one of the processes: {e}.", exc_info=True)
        return

        # ============== JOIN PROCESSES (SEQUENTIAL ORDER) ===================================

    processes = [
        video_reader_process,
        telemetry_reader_process,
        combiner_process,
        detection_process,
        segmentation_process,
        geo_process,
        models_alignment_process,
        danger_identification_process,
        annotation_process,
        video_writer_process,
        notification_writer_process,
        video_storage_process,
    ]

    try:
        while True:

            # Check if everyone has finished their logic
            all_finished = all(p.work_finished.is_set() for p in processes)

            # Check if an error occurred anywhere
            error_occurred = error_event.is_set()

            if all_finished or error_occurred:
                if error_occurred:
                    logger.error("Error detected. Terminating chain.")
                else:
                    logger.info("All processes finished logic. Cleaning up.")
                break

            sleep(0.5)
    except Exception as e:
        error_event.set()
        logger.critical(f"Unexpecter error in main process, set error event: {e}", exc_info=True)

    logger.info(f"Granting 5s for all processed to cleanly conclude their processing.")
    sleep(5.0)

    # The Sweep: Force everyone to join or die
    for p in processes:
        # If the logic is finished but the process is still 'alive',
        # it is 100% stuck in the queue feeder thread.
        if p.is_alive():
            logger.warning(f"{p.name} is hanging in cleanup. Work Completed: {p.work_finished.is_set()}. Terminating.")
            p.terminate()

        p.join()
        logger.info(f"{p.name} joined.")


if __name__ == "__main__":
    main()
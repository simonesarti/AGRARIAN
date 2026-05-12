from typing import Any
import os

import multiprocessing as mp

from src.health_monitoring.processes.tracking import TrackerWorker
from src.health_monitoring.processes.anomaly import AnomalyWorker
from src.health_monitoring.processes.annotation import AnnotationWorker
from src.health_monitoring.processes.frame_telemetry_combiner import FrameTelemetryCombiner


from src.shared.processes.stream_video_reader import StreamVideoReader
from src.shared.processes.mqtt_telemetry_listener import MqttCollectorProcess
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
    video_handler = logging.FileHandler('./logs/main_hm.log', mode='w')
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

    tracking_args = read_yaml_config("configs/health_monitoring/tracker.yaml")
    anomaly_args = read_yaml_config("configs/health_monitoring/anomaly_detector.yaml")

    output_dir = Path(LOCAL_OUTPUT_DIR)
    output_dir.mkdir(exist_ok=True, parents=True)

    certificates_dir = Path("certificates")
    mqtt_certificates_dir = certificates_dir / "mqtt"

    # ============== SETUP EVENTS ===================================

    stop_event = mp.Event()
    error_event = mp.Event()

    # ============== SETUP QUEUES ===================================

    # LAYER 1 -> 2
    frame_reader_out_queue = mp.Queue(maxsize=env_vars["MAX_SIZE_FRAME_READER_OUT"])
    telemetry_reader_out_queue = mp.Queue(maxsize=env_vars["MAX_SIZE_TELEMETRY_READER_OUT"])
    # LAYER 2 -> 3
    tracking_in_queue = mp.Queue(maxsize=env_vars["MAX_SIZE_TRACKING_IN"])
    # LAYER 3-> 4
    anomaly_in_queue = mp.Queue(maxsize=env_vars["MAX_SIZE_ANOMALY_IN"])
    # LAYER 4 -> 5
    tracking_results_queue = mp.Queue(maxsize=env_vars["AX_SIZE_TRACKING_RESULTS"])
    anomaly_results_queue = mp.Queue(maxsize=env_vars["MAX_SIZE_ANOMALY_RESULTS"])
    models_results_queues = [tracking_results_queue, anomaly_results_queue]
    # LAYER 5 -> 6
    video_stream_queue = mp.Queue(maxsize=env_vars["MAX_SIZE_VIDEO_STREAM"])
    notifications_stream_queue = mp.Queue(maxsize=env_vars["MAX_SIZE_NOTIFICATIONS_STREAM"])
    # LAYER 6 -> 7
    video_storage_queue = mp.Queue(maxsize=env_vars["MAX_SIZE_VIDEO_STORAGE"])

    # ============== INSTANTIATE PROCESSES ===================================

    try:

        # Create StreamVideoReader process
        video_reader_process = StreamVideoReader(
            frame_queue=frame_reader_out_queue,
            stop_event=stop_event,
            error_event=error_event,
            video_stream_url=env_vars["VIDEO_STREAM_READER_URL"],
            connect_open_timeout_s=VIDEO_STREAM_READER_CONNECTION_OPEN_TIMEOUT_S,
            connect_retry_delay_s=VIDEO_STREAM_READER_RECONNECT_DELAY,
            connect_max_consecutive_failures=VIDEO_STREAM_READER_MAX_CONSECUTIVE_CONNECTION_FAILURES,
            frame_read_timeout_s=VIDEO_STREAM_READER_FRAME_READ_TIMEOUT_S,
            frame_read_retry_delay_s=VIDEO_STREAM_READER_FRAME_RETRY_DELAY,
            frame_read_max_consecutive_failures=VIDEO_STREAM_READER_FRAME_MAX_CONSECUTIVE_FAILURES,
            buffer_size=VIDEO_STREAM_READER_BUFFER_SIZE,
            expected_aspect_ratio=VIDEO_STREAM_READER_EXPECTED_ASPECT_RATIO,
            processing_shape=VIDEO_STREAM_READER_PROCESSING_SHAPE,
            queue_out_put_timeout=VIDEO_STREAM_READER_QUEUE_PUT_TIMEOUT,
            poison_pill_timeout=POISON_PILL_TIMEOUT,
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
            ca_certs_file_path=str(mqtt_certificates_dir),
            cert_validation=TELEMETRY_LISTENER_CERT_VALIDATION,
            max_msg_wait=TELEMETRY_LISTENER_MSG_WAIT_TIMEOUT,
            reconnection_delay=TELEMETRY_LISTENER_RECONNECT_DELAY,
            max_incoming_messages=TELEMETRY_LISTENER_MAX_INCOMING_MESSAGES,
        )

        # Create Frame-Telemetry combiner process
        combiner_process = FrameTelemetryCombiner(
            frame_queue=frame_reader_out_queue,
            telemetry_queue=telemetry_reader_out_queue,
            output_queue=tracking_in_queue,
            error_event=error_event,
            telemetry_buffer_max_size=FRAMETELCOMB_MAX_TELEM_BUFFER_SIZE,
            max_time_diff_s=FRAMETELCOMB_MAX_TIME_DIFF,
            queue_get_timeout=FRAMETELCOMB_QUEUE_GET_TIMEOUT,
            queue_put_timeout=FRAMETELCOMB_QUEUE_PUT_TIMEOUT,
            poison_pill_timeout=POISON_PILL_TIMEOUT,
        )

        # Create TrackingWorker process
        tracking_process = TrackerWorker(
            input_queue=tracking_in_queue,
            result_queue=tracking_results_queue,
            error_event=error_event,
            tracking_args=tracking_args,
            queue_get_timeout=MODELS_QUEUE_GET_TIMEOUT,
            queue_put_timeout=MODELS_QUEUE_PUT_TIMEOUT,
            poison_pill_timeout=POISON_PILL_TIMEOUT,
        )

        # TODO: Create TrackingWorker process
        anomaly_detection_process = AnomalyWorker(
            input_queue=anomaly_in_queue,
            result_queue=anomaly_results_queue,
            error_event=error_event,
            anomaly_args=anomaly_args,
            queue_get_timeout=MODELS_QUEUE_GET_TIMEOUT,
            queue_put_timeout=MODELS_QUEUE_PUT_TIMEOUT,
            poison_pill_timeout=POISON_PILL_TIMEOUT,
        )

        # TODO: Create VideoAnnotatorWorker
        annotation_process = AnnotationWorker(
            input_queues=models_results_queues,
            video_stream_queue=video_stream_queue,
            alerts_stream_queue=notifications_stream_queue,
            error_event=error_event,
            alerts_cooldown_seconds=ALERTS_COOLDOWN_SECONDS,
            queue_get_timeout=ANNOTATION_QUEUE_GET_TIMEOUT,
            queue_put_timeout=ANNOTATION_QUEUE_PUT_TIMEOUT,
            max_put_alert_consecutive_failures=ANNOTATION_MAX_PUT_ALERT_CONSECUTIVE_FAILURES,
            max_put_video_consecutive_failures=ANNOTATION_MAX_PUT_VIDEO_CONSECUTIVE_FAILURES,
            poison_pill_timeout=POISON_PILL_TIMEOUT,
        )

        # ---------------------------------

        # Create VideoStreamWriter process
        video_writer_process = VideoProducerProcess(
            input_queue=video_stream_queue,
            output_queue=video_storage_queue,
            error_event=error_event,
            fps=FPS,
            poison_pill_timeout=POISON_PILL_TIMEOUT,
            local_video_name=str(output_dir / ANNOTATED_VIDEO_NAME),
            video_codec=CODEC,
            media_server_url=env_vars["VIDEO_OUT_STREAM_URL"],
            stream_manager_queue_max_size=MAX_SIZE_VIDEO_STREAM,
            stream_manager_queue_get_timeout=VIDEO_OUT_STREAM_QUEUE_GET_TIMEOUT,
            stream_manager_ffmpeg_startup_timeout=VIDEO_OUT_STREAM_FFMPEG_STARTUP_TIMEOUT,
            stream_manager_ffmpeg_shutdown_timeout=VIDEO_OUT_STREAM_FFMPEG_SHUTDOWN_TIMEOUT,
            stream_manager_startup_timeout=VIDEO_OUT_STREAM_STARTUP_TIMEOUT,
            stream_manager_shutdown_timeout=VIDEO_OUT_STREAM_SHUTDOWN_TIMEOUT,
            # -------- STORAGE_MANAGER ------------
            storage_manager_handoff_timeout=VIDEO_WRITER_HANDOFF_TIMEOUT,
        )

        # ---------------------------------

        # Create VideoStreamWriter process
        notification_writer_process = NotificationsStreamWriter(
            input_queue=notifications_stream_queue,
            error_event=error_event,
            alerts_get_timeout=ALERTS_QUEUE_GET_TIMEOUT,
            alerts_max_consecutive_failures=ALERTS_MAX_CONSECUTIVE_FAILURES,
            alerts_jpeg_quality=ALERTS_JPEG_COMPRESSION_QUALITY,
            log_file_path=str(output_dir / ALERTS_FILE_NAME),
            websocket_host=WEBSOCKET_HOST,  # fixed, runs locally
            websocket_port=WEBSOCKET_PORT,  # fixed, uses tls
            ws_manager_ping_interval=WS_MANAGER_PING_INTERVAL,
            ws_manager_ping_timeout=WS_MANAGER_PING_TIMEOUT,
            ws_manager_broadcast_timeout=WS_MANAGER_BROADCAST_TIMEOUT,
            ws_manager_thread_close_timeout=WS_MANAGER_THREAD_CLOSE_TIMEOUT,
            database_service=env_vars["DB_SERVICE"],
            database_worker_name=env_vars["DB_WORKER_NAME"],
            database_worker_password=env_vars["DB_WORKER_PASSWORD"],
            database_host=env_vars["DB_HOST"],
            database_port=env_vars["DB_PORT"],
            database_username=env_vars["DB_USERNAME"],
            database_password=env_vars["DB_PASSWORD"],
            db_manager_pool_size=DB_MANAGER_POOL_SIZE,
            db_manager_max_overflow=DB_MANAGER_MAX_OVERFLOW,
            db_manager_queue_get_timeout=DB_MANAGER_QUEUE_WAIT_TIMEOUT,
            db_manager_thread_close_timeout=DB_MANAGER_THREAD_CLOSE_TIMEOUT,
            db_manager_alerts_queue_size=DB_MANAGER_QUEUE_SIZE,
        )

        # ---------------------------------

        video_storage_process = VideoPersistenceProcess(
            input_queue=video_storage_queue,
            storage_url=env_vars["VIDEO_OUT_STORE_URL"],
            delete_local_on_success=VIDEO_OUT_STORE_DELETE_LOCAL_ON_SUCCESS,
            queue_get_timeout=VIDEO_OUT_STORE_QUEUE_GET_TIMEOUT,
            max_retries=VIDEO_OUT_STORE_MAX_UPLOAD_RETRIES,
            retry_backoff=VIDEO_OUT_STORE_RETRY_BACKOFF_TIME,
        )

    except Exception as e:
        logger.critical(f"Failed to instantiate one of the processes: {e}", exc_info=True)
        return

    # ============== START PROCESSES (REVERSE ORDER) ===================================

    try:

        # LAYER 7
        video_storage_process.start()
        sleep(1.0)

        # LAYER 6
        notification_writer_process.start()
        sleep(1.0)

        video_writer_process.start()
        sleep(1.0)

        # LAYER 5
        annotation_process.start()
        sleep(1.0)

        # LAYER 4
        anomaly_detection_process.start()
        sleep(1.0)

        # LAYER 3
        tracking_process.start()
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
        tracking_process,
        anomaly_detection_process,
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
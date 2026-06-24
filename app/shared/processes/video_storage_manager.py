import os
import time
import logging
import shutil
import multiprocessing as mp
import multiprocessing.synchronize
from queue import Empty as QueueEmptyException
from typing import Optional
from pydantic import BaseModel, NonNegativeInt, PositiveFloat, PositiveInt

from app.shared.processes.constants import (
    VIDEO_OUT_STORE_DELETE_LOCAL_ON_SUCCESS,
    VIDEO_OUT_STORE_QUEUE_GET_TIMEOUT,
    VIDEO_OUT_STORE_MAX_UPLOAD_RETRIES,
    VIDEO_OUT_STORE_RETRY_BACKOFF_TIME,
)


# ================================================================

logger = logging.getLogger("main.video_out.storage")

if not logger.handlers:  # Avoid duplicate handlers
    video_handler = logging.FileHandler('./logs/video_out_storage.log', mode='w')
    video_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(video_handler)
    logger.setLevel(logging.WARNING)

# ================================================================


class VideoPersistenceProcessConfig(BaseModel):
    """Base configuration for VideoPersistenceProcess."""

    queue_get_timeout: PositiveFloat = VIDEO_OUT_STORE_QUEUE_GET_TIMEOUT
    max_retries: NonNegativeInt = VIDEO_OUT_STORE_MAX_UPLOAD_RETRIES
    retry_backoff_s: PositiveFloat = VIDEO_OUT_STORE_RETRY_BACKOFF_TIME
    delete_local_on_success: bool = VIDEO_OUT_STORE_DELETE_LOCAL_ON_SUCCESS


class AzureBlobStorageConfig(VideoPersistenceProcessConfig):
    """Config for Azure Blob Storage uploads."""

    connection_string: str
    container_name: str
    blob_prefix: str = ""       # Optional subfolder inside the container


class S3StorageConfig(VideoPersistenceProcessConfig):
    """Config for AWS S3 uploads."""

    bucket_name: str
    key_prefix: str = ""        # Optional subfolder inside the bucket
    # Credentials — if None, boto3 falls back to env vars / IAM role
    aws_access_key_id: Optional[str] = None
    aws_secret_access_key: Optional[str] = None
    region_name: Optional[str] = None


class LocalStorageConfig(VideoPersistenceProcessConfig):
    """Config for local-copy 'uploads' (testing / no-cloud fallback)."""

    target_directory: str


class VideoPersistenceProcess(mp.Process):
    """
    Terminal process in the video saving branch.

    Waits for a single value from VideoProducerProcess via input_queue:
    - str  : path to the locally saved video file → upload it
    - None : video save failed upstream → skip upload and exit

    The process polls the queue at queue_get_timeout intervals so it can
    react to error_event being set by another process while waiting. Once
    a value is received (or the error_event fires), the process exits.
    No poison pill needed — this is a run-once process.

    Subclasses implement _upload_file() for their specific storage backend.
    """

    def __init__(
            self,
            input_queue: mp.Queue,
            error_event: multiprocessing.synchronize.Event,
            config: VideoPersistenceProcessConfig,
    ):
        super().__init__()
        self.input_queue = input_queue
        self.error_event = error_event
        self.config = config
        self.work_finished = mp.Event()

    def _upload_file(self, file_path: str) -> bool:
        """Upload file to storage backend. Returns True on success, False on failure."""
        raise NotImplementedError("Subclasses must implement _upload_file().")

    def _cleanup_local_file(self, video_file_path: str) -> None:
        """Delete the local file after a successful upload, if configured to do so."""
        if self.config.delete_local_on_success:
            try:
                os.remove(video_file_path)
                logger.info(f"Local file deleted: {video_file_path}")
            except OSError as e:
                logger.error(f"Could not delete local file '{video_file_path}': {e}")

    def _run_upload_routine(self, video_file_path: str) -> None:
        """Attempt the upload with retries and constant backoff."""
        if not os.path.exists(video_file_path):
            logger.error(f"Upload aborted: file not found at '{video_file_path}'.")
            return

        for attempt in range(1, self.config.max_retries + 1):
            try:
                logger.info(f"Upload attempt {attempt}/{self.config.max_retries} ...")
                if self._upload_file(video_file_path):
                    logger.info(f"Upload complete: '{video_file_path}'.")
                    self._cleanup_local_file(video_file_path)
                    return
                logger.warning(f"Upload attempt #{attempt} failed.")
            except Exception as e:
                logger.error(f"Upload attempt #{attempt} raised: {e}", exc_info=True)

            if attempt < self.config.max_retries:
                logger.info(f"Retrying in {self.config.retry_backoff_s} seconds ...")
                time.sleep(self.config.retry_backoff_s)

        logger.error(
            f"Failed to upload '{video_file_path}' after {self.config.max_retries} attempt(s). "
            "Local file preserved for manual recovery."
        )

    def run(self):
        logger.info("VideoPersistenceProcess started.")

        try:
            # Poll until a message arrives or another process signals an error
            message = None
            while not self.error_event.is_set():
                try:
                    message = self.input_queue.get(timeout=self.config.queue_get_timeout)
                    break
                except QueueEmptyException:
                    logger.debug("Waiting for video path from VideoProducerProcess ...")

            if self.error_event.is_set():
                logger.info("Error event set by another process. Exiting without upload.")
                return

            if message is None:
                logger.info("Received None: video save failed upstream. No upload to perform.")
                return

            if not isinstance(message, str):
                logger.error(f"Unexpected message type {type(message).__name__}. Exiting.")
                return

            logger.info(f"Received upload task for: '{message}'")
            self._run_upload_routine(message)

        except Exception as e:
            logger.critical(f"Critical error in VideoPersistenceProcess: {e}", exc_info=True)
            self.error_event.set()

        finally:
            logger.info("VideoPersistenceProcess terminated.")
            self.work_finished.set()


class AzureBlobStoragePersistenceProcess(VideoPersistenceProcess):
    """Uploads video files to Azure Blob Storage using azure-storage-blob."""

    def __init__(
            self, 
            input_queue: mp.Queue, 
            error_event: multiprocessing.synchronize.Event,
            config: AzureBlobStorageConfig,
        ):
        super().__init__(input_queue, error_event, config)
        self.config: AzureBlobStorageConfig

    def _upload_file(self, file_path: str) -> bool:
        try:
            from azure.storage.blob import BlobServiceClient
        except ImportError:
            logger.error(
                "azure-storage-blob is not installed. "
                "Run: pip install azure-storage-blob"
            )
            return False

        blob_name = os.path.basename(file_path)
        if self.config.blob_prefix:
            blob_name = f"{self.config.blob_prefix.rstrip('/')}/{blob_name}"

        client = BlobServiceClient.from_connection_string(self.config.connection_string)
        container = client.get_container_client(self.config.container_name)

        with open(file_path, "rb") as data:
            container.upload_blob(name=blob_name, data=data, overwrite=True)

        logger.info(
            f"Uploaded to Azure Blob Storage: "
            f"container='{self.config.container_name}', blob='{blob_name}'"
        )
        return True


class S3StoragePersistenceProcess(VideoPersistenceProcess):
    """Uploads video files to AWS S3 using boto3."""

    def __init__(
            self, 
            input_queue: mp.Queue, 
            error_event: multiprocessing.synchronize.Event,
            config: S3StorageConfig,
        ):
        super().__init__(input_queue, error_event, config)
        self.config: S3StorageConfig

    def _upload_file(self, file_path: str) -> bool:
        try:
            import boto3
        except ImportError:
            logger.error(
                "boto3 is not installed. "
                "Run: pip install boto3"
            )
            return False

        key = os.path.basename(file_path)
        if self.config.key_prefix:
            key = f"{self.config.key_prefix.rstrip('/')}/{key}"

        session = boto3.Session(
            aws_access_key_id=self.config.aws_access_key_id,
            aws_secret_access_key=self.config.aws_secret_access_key,
            region_name=self.config.region_name,
        )
        s3 = session.client("s3")
        # upload_file handles multipart uploads automatically for large files
        s3.upload_file(file_path, self.config.bucket_name, key)

        logger.info(
            f"Uploaded to S3: s3://{self.config.bucket_name}/{key}"
        )
        return True


class LocalStoragePersistenceProcess(VideoPersistenceProcess):
    """Copies video files to a local directory (testing / no-cloud fallback)."""

    def __init__(
            self, 
            input_queue: mp.Queue, 
            error_event: multiprocessing.synchronize.Event,
            config: LocalStorageConfig,
        ):
        super().__init__(input_queue, error_event, config)
        self.config: LocalStorageConfig

    def _upload_file(self, file_path: str) -> bool:
        os.makedirs(self.config.target_directory, exist_ok=True)
        target_path = os.path.join(self.config.target_directory, os.path.basename(file_path))
        if os.path.abspath(file_path) == os.path.abspath(target_path):
            logger.info(f"File already at target location, skipping copy: '{file_path}'")
            return True
        shutil.copy2(file_path, target_path)
        logger.info(f"File copied to local storage: '{target_path}'")
        return True


if __name__ == "__main__":

    import tempfile

    # Create a temporary source file to simulate a saved video
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(b"dummy video content")
        dummy_video_path = f.name

    target_dir = tempfile.mkdtemp()
    input_queue = mp.Queue(maxsize=2)
    error_event = mp.Event()

    config = LocalStorageConfig(target_directory=target_dir)
    process = LocalStoragePersistenceProcess(
        input_queue=input_queue,
        error_event=error_event,
        config=config,
    )

    process.start()
    input_queue.put(dummy_video_path)
    process.join(timeout=10)

    print(f"Source: {dummy_video_path}")
    print(f"Target dir: {target_dir}")
    print(f"Error event set: {error_event.is_set()}")
    print(f"Work finished: {process.work_finished.is_set()}")

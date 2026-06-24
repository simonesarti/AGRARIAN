"""
recorder — video segment upload sidecar.

MediaMTX calls POST /on-segment-complete (via wget hook) whenever it finishes
writing a recording segment — either on publisher disconnect or at the
recordSegmentDuration boundary. This service uploads the file to the configured
storage backend in a background task, returning immediately so the hook doesn't
block MediaMTX.

Storage backends:
  local — file stays on the shared recordings volume (no-op upload)
  azure — upload to Azure Blob Storage
  aws   — upload to AWS S3
"""

import logging
import os
from pathlib import Path

import uvicorn
from fastapi import BackgroundTasks, FastAPI, Form

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("recorder")

STORE_SERVICE          = os.getenv("RECORDING_STORE_SERVICE", "local").lower()
DELETE_LOCAL_ON_SUCCESS = os.getenv("RECORDING_DELETE_LOCAL_ON_SUCCESS", "false").lower() == "true"

app = FastAPI(title="Recorder")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/on-segment-complete")
def on_segment_complete(path: str = Form(...), background_tasks: BackgroundTasks = BackgroundTasks()):
    logger.info(f"Segment complete: {path}")
    background_tasks.add_task(_upload, path)
    return {"accepted": True}


# ── Upload dispatcher ─────────────────────────────────────────────────────────

def _upload(path: str):
    logger.info(f"Uploading '{path}' to '{STORE_SERVICE}'")
    try:
        if STORE_SERVICE == "local":
            _local(path)
        elif STORE_SERVICE == "azure":
            _azure(path)
        elif STORE_SERVICE == "aws":
            _aws(path)
        else:
            logger.error(f"Unknown RECORDING_STORE_SERVICE '{STORE_SERVICE}' — skipping upload")
            return
    except Exception as e:
        logger.error(f"Upload failed for '{path}': {e}", exc_info=True)
        return

    if DELETE_LOCAL_ON_SUCCESS and STORE_SERVICE != "local":
        try:
            Path(path).unlink()
            logger.info(f"Deleted local segment: {path}")
        except Exception as e:
            logger.warning(f"Could not delete local segment '{path}': {e}")


# ── Storage backends ──────────────────────────────────────────────────────────

def _local(path: str):
    logger.info(f"Local storage — segment retained at: {path}")


def _azure(path: str):
    from azure.storage.blob import BlobServiceClient

    conn_str  = os.environ["RECORDING_AZURE_CONNECTION_STRING"]
    container = os.environ["RECORDING_AZURE_CONTAINER_NAME"]
    prefix    = os.getenv("RECORDING_AZURE_BLOB_PREFIX", "").strip("/")
    blob_name = f"{prefix}/{Path(path).name}".lstrip("/")

    client = BlobServiceClient.from_connection_string(conn_str)
    with open(path, "rb") as f:
        client.get_blob_client(container=container, blob=blob_name).upload_blob(f, overwrite=True)
    logger.info(f"Uploaded to Azure Blob: {container}/{blob_name}")


def _aws(path: str):
    import boto3

    bucket = os.environ["RECORDING_AWS_BUCKET_NAME"]
    prefix = os.getenv("RECORDING_AWS_KEY_PREFIX", "").strip("/")
    key    = f"{prefix}/{Path(path).name}".lstrip("/")

    kwargs = {}
    if key_id := os.getenv("RECORDING_AWS_ACCESS_KEY_ID"):
        kwargs["aws_access_key_id"]     = key_id
        kwargs["aws_secret_access_key"] = os.environ["RECORDING_AWS_SECRET_ACCESS_KEY"]
    if region := os.getenv("RECORDING_AWS_REGION_NAME"):
        kwargs["region_name"] = region

    boto3.client("s3", **kwargs).upload_file(path, bucket, key)
    logger.info(f"Uploaded to S3: s3://{bucket}/{key}")


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, log_level="info")

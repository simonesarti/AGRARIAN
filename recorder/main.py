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

After a successful upload, the segment is reported to db-writer so it lands against
the flight it belongs to (see _report_upload). This service never sees a flight_id —
only the output path MediaMTX gave it — so db-writer is what resolves the two.

TENANT SEPARATION IS BY KEY PREFIX (CLOUD_ARCHITECTURE.md §11.5), not by account:
one storage account for the deployment, and every object written under
`tenants/<user_id>/recordings/<public_uuid>/`. Holding a customer's own cloud
credentials would mean encrypting them at rest, rotating them, and a breach that
hands out other people's storage accounts rather than only this system's data.

That forces the one structural change in this file: the tenant has to be resolved
BEFORE the upload, because a prefix cannot be applied to an object already written.
db-writer answers it, this service builds the prefix, and a failure to resolve stops
the upload rather than falling back to a shared location.
"""

import json
import logging
import os
import re
import urllib.error
import urllib.request
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
DB_WRITER_URL           = os.environ["DB_WRITER_URL"]

# The output path is out/<public_uuid>, and recordPath is /recordings/%path/<segment>,
# so the uuid is always the path component right after "out/".
_PUBLIC_UUID_RE = re.compile(
    r"out/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)

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

def _tenant_prefix(path: str):
    """
    `tenants/<user_id>/recordings/<public_uuid>` for this segment, or None.

    §11.5: one storage account, tenants separated by key prefix. The prefix has to
    be known BEFORE the object is written — there is no retroactive move that is
    not a copy and a delete — so this runs ahead of the upload rather than beside
    the report that follows it.

    The recorder cannot derive user_id on its own: it is handed an output path by
    MediaMTX and nothing else, and §5 keeps ownership on `streams` where only a
    join can reach it. So it asks db-writer, which is the authority for exactly
    that question, and then builds the prefix itself — keeping this service a
    thing that only ever writes, which is why §11.5 lets it keep its own
    credentials instead of being moved onto minted URLs.
    """
    match = _PUBLIC_UUID_RE.search(path)
    if not match:
        return None
    public_uuid = match.group(1)
    req = urllib.request.Request(f"{DB_WRITER_URL}/recording/tenant/{public_uuid}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            user_id = json.loads(resp.read())["user_id"]
    except Exception as e:
        logger.error(f"Could not resolve the tenant for '{path}': {e}")
        return None
    return f"tenants/{user_id}/recordings/{public_uuid}"


def _upload(path: str):
    logger.info(f"Uploading '{path}' to '{STORE_SERVICE}'")

    # Resolved before the upload, and a failure here STOPS the upload rather than
    # falling back to a shared prefix. That is deliberate and it is the whole point
    # of the feature: an object written outside its tenant's prefix is the tenancy
    # hole this closes, and it would be invisible afterwards. The segment stays on
    # the recordings volume — DELETE_LOCAL_ON_SUCCESS cannot fire, since nothing
    # succeeded — so nothing is lost and the upload can be retried by hand.
    #
    # `local` is exempt because it moves nothing: the file is already where it is,
    # on a volume that belongs to this deployment rather than to a tenant.
    prefix = None
    if STORE_SERVICE in ("azure", "aws"):
        prefix = _tenant_prefix(path)
        if prefix is None:
            logger.error(
                f"Refusing to upload '{path}': no tenant prefix could be derived. "
                f"The segment is retained locally.")
            return

    try:
        if STORE_SERVICE == "local":
            location = _local(path)
        elif STORE_SERVICE == "azure":
            location = _azure(path, prefix)
        elif STORE_SERVICE == "aws":
            location = _aws(path, prefix)
        else:
            logger.error(f"Unknown RECORDING_STORE_SERVICE '{STORE_SERVICE}' — skipping upload")
            return
    except Exception as e:
        logger.error(f"Upload failed for '{path}': {e}", exc_info=True)
        return

    _report_upload(path, location)

    if DELETE_LOCAL_ON_SUCCESS and STORE_SERVICE != "local":
        try:
            Path(path).unlink()
            logger.info(f"Deleted local segment: {path}")
        except Exception as e:
            logger.warning(f"Could not delete local segment '{path}': {e}")


def _report_upload(path: str, location):
    """Tell db-writer this segment landed, so it's tied to the flight it came from."""
    match = _PUBLIC_UUID_RE.search(path)
    if not match:
        logger.warning(f"Could not find an output uuid in '{path}' — recording not logged")
        return

    body = json.dumps({
        "public_uuid": match.group(1),
        "segment_path": path,
        "storage_backend": STORE_SERVICE,
        "storage_location": location,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{DB_WRITER_URL}/recording", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        logger.error(f"db-writer refused the recording for '{path}': {e.code} {e.read()}")
    except Exception as e:
        logger.error(f"Could not report recording for '{path}' to db-writer: {e}")


# ── Storage backends ──────────────────────────────────────────────────────────

def _local(path: str):
    logger.info(f"Local storage — segment retained at: {path}")
    return None


def _azure(path: str, tenant_prefix: str):
    from azure.storage.blob import BlobServiceClient

    conn_str  = os.environ["RECORDING_AZURE_CONNECTION_STRING"]
    container = os.environ["RECORDING_AZURE_CONTAINER_NAME"]
    # RECORDING_AZURE_BLOB_PREFIX still applies and sits OUTSIDE the tenant prefix,
    # so one container can be shared with something else without the two schemes
    # interleaving. The tenant part is never optional — see _upload.
    prefix    = os.getenv("RECORDING_AZURE_BLOB_PREFIX", "").strip("/")
    blob_name = "/".join(p for p in (prefix, tenant_prefix, Path(path).name) if p)

    client = BlobServiceClient.from_connection_string(conn_str)
    with open(path, "rb") as f:
        client.get_blob_client(container=container, blob=blob_name).upload_blob(f, overwrite=True)
    logger.info(f"Uploaded to Azure Blob: {container}/{blob_name}")
    return f"{container}/{blob_name}"


def _aws(path: str, tenant_prefix: str):
    import boto3

    bucket = os.environ["RECORDING_AWS_BUCKET_NAME"]
    # As with Azure: the deployment's own prefix wraps the tenant's, never replaces it.
    prefix = os.getenv("RECORDING_AWS_KEY_PREFIX", "").strip("/")
    key    = "/".join(p for p in (prefix, tenant_prefix, Path(path).name) if p)

    kwargs = {}
    if key_id := os.getenv("RECORDING_AWS_ACCESS_KEY_ID"):
        kwargs["aws_access_key_id"]     = key_id
        kwargs["aws_secret_access_key"] = os.environ["RECORDING_AWS_SECRET_ACCESS_KEY"]
    if region := os.getenv("RECORDING_AWS_REGION_NAME"):
        kwargs["region_name"] = region

    boto3.client("s3", **kwargs).upload_file(path, bucket, key)
    logger.info(f"Uploaded to S3: s3://{bucket}/{key}")
    return f"s3://{bucket}/{key}"


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, log_level="info")

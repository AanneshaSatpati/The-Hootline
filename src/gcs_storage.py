"""Upload episode MP3 files and sync SQLite DB via Google Cloud Storage."""

import json
import logging
import os
import sqlite3
from pathlib import Path

from google.cloud import storage
from google.oauth2 import service_account

from config import settings

logger = logging.getLogger(__name__)


def _is_prod() -> bool:
    """True when running in the production environment."""
    return os.environ.get("NOCTUA_ENV", "dev").lower() == "prod"


def _get_client() -> storage.Client:
    """Create a GCS client from service account credentials."""
    creds_info = json.loads(settings.gcs_credentials_json)
    credentials = service_account.Credentials.from_service_account_info(creds_info)
    return storage.Client(credentials=credentials, project=credentials.project_id)


def upload_episode(local_path: Path, date: str, show_id: str = "noctua") -> str:
    """Upload an episode MP3 to GCS and return the public URL.

    Args:
        local_path: Path to the local MP3 file.
        date: Episode date string (YYYY-MM-DD).
        show_id: Show identifier for namespaced blob paths.

    Returns:
        Public URL of the uploaded file.
    """
    bucket_name = settings.gcs_bucket_name
    blob_name = f"episodes/{show_id}/noctua-{date}.mp3"

    client = _get_client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)

    blob.upload_from_filename(str(local_path), content_type="audio/mpeg")

    url = f"https://storage.googleapis.com/{bucket_name}/{blob_name}"
    logger.info("Uploaded episode to GCS: %s", url)
    return url


def is_configured() -> bool:
    """Check if GCS storage is configured."""
    return bool(settings.gcs_bucket_name and settings.gcs_credentials_json)


# --- SQLite DB sync ---


def _checkpoint_wal(db_path: Path) -> None:
    """Flush WAL journal into the main DB file before upload."""
    if not db_path.exists():
        return
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def upload_db(db_path: Path, show_id: str = "hootline") -> bool:
    """Upload the SQLite DB to GCS. Returns True on success.

    Only runs in prod (NOCTUA_ENV=prod). Dev is read-only against prod GCS.
    Non-fatal: logs errors but never raises.
    """
    if not _is_prod():
        logger.info("[DEV] Skipping DB upload — read-only against prod GCS.")
        return False
    if not is_configured():
        return False
    if not db_path.exists():
        logger.warning("DB file not found at %s — skipping upload.", db_path)
        return False
    try:
        _checkpoint_wal(db_path)
        blob_name = f"db/{show_id}/noctua.db"
        client = _get_client()
        bucket = client.bucket(settings.gcs_bucket_name)
        blob = bucket.blob(blob_name)
        blob.upload_from_filename(str(db_path), content_type="application/x-sqlite3")
        logger.info("Uploaded DB to GCS: %s", blob_name)
        return True
    except Exception as e:
        logger.error("Failed to upload DB to GCS: %s", e)
        return False


def download_db(db_path: Path, show_id: str = "hootline") -> bool:
    """Download the SQLite DB from GCS. Returns True if downloaded.

    Only overwrites if the blob exists in GCS.
    Non-fatal: logs warnings but never raises.
    """
    if not is_configured():
        return False
    try:
        blob_name = f"db/{show_id}/noctua.db"
        client = _get_client()
        bucket = client.bucket(settings.gcs_bucket_name)
        blob = bucket.blob(blob_name)
        if not blob.exists():
            logger.info("No DB blob in GCS at %s — using local DB.", blob_name)
            return False
        db_path.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(db_path))
        logger.info("Downloaded DB from GCS: %s", blob_name)
        return True
    except Exception as e:
        logger.warning("Failed to download DB from GCS: %s — using local DB.", e)
        return False

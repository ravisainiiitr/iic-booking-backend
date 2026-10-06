"""Publish DSA-imported result files to S3 under Results/{virtual_booking_id}/."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

S3_RESULTS_PREFIX = "Results"


def _s3_client():
    import boto3

    bucket = getattr(settings, "AWS_STORAGE_BUCKET_NAME", None)
    if not bucket:
        return None, None
    client = boto3.client(
        "s3",
        region_name=getattr(settings, "AWS_S3_REGION_NAME", "ap-south-1"),
        aws_access_key_id=getattr(settings, "AWS_ACCESS_KEY_ID", None),
        aws_secret_access_key=getattr(settings, "AWS_SECRET_ACCESS_KEY", None),
    )
    return client, bucket


def _safe_file_name(file_name: str) -> str:
    name = (file_name or "result.bin").replace("\\", "/").split("/")[-1].strip()
    name = re.sub(r"[^\w.\- ()\[\]]+", "_", name)
    return name or "result.bin"


def booking_results_folder(booking) -> str:
    """Per-booking-record folder under Results/{vid}/.

    Display IDs restart (e.g. after a booking wipe), so the display ID alone is not unique over time;
    the pk plus creation time is.
    """
    created = getattr(booking, "created_at", None)
    stamp = created.strftime("%Y%m%d%H%M%S") if created else "0"
    return f"b{booking.pk}-{stamp}"


def build_results_s3_key(virtual_booking_id: str, file_name: str, *, booking_folder: str = "") -> str:
    vid = (virtual_booking_id or "").strip().strip("/")
    if not vid:
        raise ValueError("virtual_booking_id is required for Results S3 key")
    folder = (booking_folder or "").strip().strip("/")
    middle = f"{vid}/{folder}" if folder else vid
    return f"{S3_RESULTS_PREFIX}/{middle}/{_safe_file_name(file_name)}"


def upload_local_file_to_results_s3(
    *,
    virtual_booking_id: str,
    local_path: Path,
    file_name: str,
    content_type: str = "",
    booking_folder: str = "",
) -> str | None:
    """
    Upload a local file to Results/{virtual_booking_id}/[{booking_folder}/]{file_name}.
    Returns the S3 object key on success, or None if S3 is not configured / upload failed.
    """
    client, bucket = _s3_client()
    if client is None or bucket is None:
        logger.warning("Results S3 upload skipped — AWS_STORAGE_BUCKET_NAME not configured")
        return None
    if not local_path.is_file():
        logger.error("Results S3 upload failed — local file missing: %s", local_path)
        return None

    key = build_results_s3_key(virtual_booking_id, file_name, booking_folder=booking_folder)
    try:
        if content_type:
            client.upload_file(
                str(local_path),
                bucket,
                key,
                ExtraArgs={"ContentType": content_type},
            )
        else:
            client.upload_file(str(local_path), bucket, key)
        # Verify object exists
        client.head_object(Bucket=bucket, Key=key)
        logger.info(
            "Results S3 upload ok | key=%s | bytes=%s",
            key,
            local_path.stat().st_size,
        )
        return key
    except Exception:
        logger.exception("Results S3 upload failed | key=%s | path=%s", key, local_path)
        return None


def presign_results_s3_get(key: str, *, expires_in: int = 3600) -> str | None:
    client, bucket = _s3_client()
    if client is None or bucket is None or not key:
        return None
    try:
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires_in,
        )
    except Exception:
        logger.exception("Results S3 presign failed | key=%s", key)
        return None


def download_results_s3_bytes(key: str) -> bytes | None:
    client, bucket = _s3_client()
    if client is None or bucket is None or not key:
        return None
    try:
        resp = client.get_object(Bucket=bucket, Key=key)
        body = resp.get("Body")
        return body.read() if body else None
    except Exception:
        logger.exception("Results S3 get_object failed | key=%s", key)
        return None


def open_results_s3_stream(key: str):
    """Streaming body for a results object (caller reads in chunks and closes), or None."""
    client, bucket = _s3_client()
    if client is None or bucket is None or not key:
        return None
    try:
        return client.get_object(Bucket=bucket, Key=key).get("Body")
    except Exception:
        logger.exception("Results S3 get_object failed | key=%s", key)
        return None


def uploaded_before(last_modified, not_before) -> bool:
    """True when an S3 object predates ``not_before`` (e.g. the booking it would be shown on)."""
    if not_before is None or last_modified is None:
        return False
    if isinstance(last_modified, str):
        from django.utils.dateparse import parse_datetime

        last_modified = parse_datetime(last_modified)
        if last_modified is None:
            return False
    return last_modified < not_before


def list_results_s3_objects(virtual_booking_id: str, *, prefix_only: bool = False, not_before=None) -> list[dict]:
    """Objects under Results/ whose path has a segment equal to the virtual booking id.

    Lists only the Results/{vid}/ prefix first; the full Results/ scan (legacy nested
    Results/<year>/<lab>/{vid}/ layouts) runs only when that prefix is empty and not prefix_only.
    Display IDs are reused after a booking wipe, so pass ``not_before=booking.created_at`` to drop
    objects uploaded before the booking existed.
    """
    vid = (virtual_booking_id or "").strip().strip("/")
    client, bucket = _s3_client()
    if client is None or bucket is None or not vid:
        return []
    segment = f"/{vid}/"

    def _scan(prefix: str) -> list[dict]:
        found: list[dict] = []
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents") or []:
                key = obj.get("Key") or ""
                if not key or key.endswith("/") or segment not in f"/{key}":
                    continue
                last_modified = obj.get("LastModified")
                if uploaded_before(last_modified, not_before):
                    continue
                found.append(
                    {
                        "key": key,
                        "s3_key": key,
                        "name": f"/{key}".split(segment, 1)[-1],
                        "size_bytes": int(obj.get("Size") or 0),
                        "etag": str(obj.get("ETag") or "").strip('"'),
                        "last_modified": last_modified.isoformat() if hasattr(last_modified, "isoformat") else None,
                        "source": "s3",
                    }
                )
        return found

    try:
        found = _scan(f"{S3_RESULTS_PREFIX}/{vid}/")
        if found or prefix_only:
            return found
        return _scan(f"{S3_RESULTS_PREFIX}/")
    except Exception:
        logger.warning("Results S3 list failed | vid=%s", vid, exc_info=True)
        raise


def delete_local_upload_copy(local_path: Path) -> bool:
    """Remove portal sync_uploads temp file after successful S3 publish."""
    try:
        if local_path.is_file():
            local_path.unlink()
            # Best-effort: remove empty parent dirs under storage root (session folder).
            parent = local_path.parent
            for _ in range(2):
                try:
                    parent.rmdir()
                    parent = parent.parent
                except OSError:
                    break
            logger.info("Deleted local DSA upload copy | path=%s", local_path)
            return True
    except OSError:
        logger.exception("Failed to delete local DSA upload copy | path=%s", local_path)
    return False

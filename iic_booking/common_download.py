"""Shared helpers for large installer / artifact downloads."""

from __future__ import annotations

import logging
import mimetypes
import os
from typing import Any
from urllib.parse import quote

from django.conf import settings
from django.http import FileResponse, HttpResponseRedirect

logger = logging.getLogger(__name__)


def _content_disposition(filename: str) -> str:
    safe = (filename or "download.bin").replace('"', "").replace("\r", "").replace("\n", "")
    ascii_name = safe.encode("ascii", "ignore").decode("ascii") or "download.bin"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(safe)}"


def _resolve_s3_object_key(storage, name: str) -> str:
    """Return the bucket object key including django-storages location prefix."""
    try:
        return storage._normalize_name(name)  # noqa: SLF001 — storages internal API
    except Exception:
        loc = (getattr(storage, "location", None) or "").strip("/")
        if loc and not str(name).startswith(f"{loc}/"):
            return f"{loc}/{name}"
        return str(name)


def _boto3_presign(
    *,
    bucket: str,
    key: str,
    download_name: str,
    expires_in: int,
    use_accelerate: bool,
) -> str | None:
    try:
        import boto3
        from botocore.config import Config
    except Exception as exc:
        logger.warning("boto3 unavailable for installer presign: %s", exc)
        return None

    disposition = _content_disposition(download_name)
    content_type = mimetypes.guess_type(download_name)[0] or "application/octet-stream"
    params = {
        "Bucket": bucket,
        "Key": key,
        "ResponseContentDisposition": disposition,
        "ResponseContentType": content_type,
    }

    region = getattr(settings, "AWS_S3_REGION_NAME", None) or "ap-south-1"
    access_key = getattr(settings, "AWS_ACCESS_KEY_ID", None) or None
    secret_key = getattr(settings, "AWS_SECRET_ACCESS_KEY", None) or None

    if use_accelerate:
        # Accelerate signatures must use us-east-1.
        client = boto3.client(
            "s3",
            region_name="us-east-1",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "virtual", "use_accelerate_endpoint": True},
            ),
        )
    else:
        client = boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            endpoint_url=f"https://s3.{region}.amazonaws.com",
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )

    try:
        return client.generate_presigned_url("get_object", Params=params, ExpiresIn=expires_in)
    except Exception as exc:
        logger.warning("presign failed accelerate=%s key=%s: %s", use_accelerate, key, exc)
        return None


def build_direct_download_url(
    file_field,
    *,
    download_name: str,
    expires_in: int = 900,
) -> str | None:
    """
    Prefer a direct/presigned URL (S3) so browsers download from object storage
    instead of proxying ~100MB+ through Django/Gunicorn (very slow).

    Order:
      1) S3 Transfer Acceleration (if enabled)
      2) Regional virtual-hosted URL (s3.<region>.amazonaws.com)
      3) django-storages storage.url() fallback
    """
    if file_field is None:
        return None
    name = getattr(file_field, "name", None) or ""
    if not name:
        return None
    storage = getattr(file_field, "storage", None)
    if storage is None:
        return None

    storage_name = type(storage).__name__
    is_s3 = "S3" in storage_name or hasattr(storage, "bucket") or hasattr(storage, "bucket_name")
    if not is_s3:
        # DefaultStorage may wrap S3 — still try url()
        try:
            url = storage.url(name)
            if url and ("amazonaws.com" in url or "cloudfront.net" in url):
                return url
        except Exception:
            return None
        return None

    bucket = (
        getattr(storage, "bucket_name", None)
        or getattr(settings, "AWS_STORAGE_BUCKET_NAME", None)
        or ""
    )
    key = _resolve_s3_object_key(storage, name)

    prefer_accel = bool(getattr(settings, "AWS_S3_TRANSFER_ACCELERATION", False))
    # For IITR (India), regional ap-south-1 is usually faster than accelerate.
    # Accelerate helps mostly for distant clients; keep it opt-in.
    if prefer_accel:
        accel = _boto3_presign(
            bucket=bucket,
            key=key,
            download_name=download_name,
            expires_in=expires_in,
            use_accelerate=True,
        )
        if accel:
            return accel

    regional = _boto3_presign(
        bucket=bucket,
        key=key,
        download_name=download_name,
        expires_in=expires_in,
        use_accelerate=False,
    )
    if regional:
        return regional

    # Last resort: django-storages helper (often global s3.amazonaws.com host)
    disposition = _content_disposition(download_name)
    content_type = mimetypes.guess_type(download_name)[0] or "application/octet-stream"
    parameters = {
        "ResponseContentDisposition": disposition,
        "ResponseContentType": content_type,
    }
    try:
        return storage.url(name, expire=expires_in, parameters=parameters)
    except TypeError:
        try:
            return storage.url(name, expire=expires_in)
        except Exception as exc:
            logger.warning("S3 direct URL failed for %s: %s", name, exc)
            return None
    except Exception as exc:
        logger.warning("S3 direct URL failed for %s: %s", name, exc)
        return None


def build_installer_file_response(
    file_field,
    *,
    download_name: str,
    default_name: str = "download.bin",
    sha256: str = "",
    version: str = "",
    release_date: str = "",
    signature_status: str = "",
    size_bytes: int | None = None,
    prefer_redirect: bool = True,
):
    """
    Stream an installer/artifact, or 302-redirect to a presigned S3 URL when possible.
    Direct S3 download is dramatically faster than proxying through the app server.
    """
    name = (download_name or os.path.basename(getattr(file_field, "name", "") or "") or default_name).replace(
        '"', ""
    )

    if prefer_redirect:
        direct = build_direct_download_url(file_field, download_name=name)
        if direct:
            resp = HttpResponseRedirect(direct)
            resp["Cache-Control"] = "private, no-store"
            if sha256:
                resp["X-Checksum-SHA256"] = sha256
            if version:
                resp["X-Release-Version"] = version
            return resp

    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    fh = file_field.open("rb")
    response = FileResponse(fh, as_attachment=True, filename=name, content_type=content_type)

    size = size_bytes
    if size is None:
        try:
            size = int(getattr(file_field, "size", 0) or 0)
        except Exception:
            size = 0
    if not size:
        try:
            pos = fh.tell()
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(pos)
        except Exception:
            size = 0
    if size:
        response["Content-Length"] = str(size)

    response["Accept-Ranges"] = "bytes"
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    if sha256:
        response["X-Checksum-SHA256"] = sha256
    if version:
        response["X-Release-Version"] = version
    if release_date:
        response["X-Release-Date"] = release_date
    if signature_status:
        response["X-Signature-Status"] = signature_status
    return response


def release_download_headers(rel: Any, *, offline: bool) -> dict[str, Any]:
    return {
        "sha256": "" if offline else (getattr(rel, "sha256", "") or ""),
        "version": getattr(rel, "version", "") or "",
        "release_date": rel.release_date.isoformat() if getattr(rel, "release_date", None) else "",
        "signature_status": getattr(rel, "signature_status", "") or "",
        "size_bytes": int(getattr(rel, "download_size_bytes", 0) or 0) or None,
    }

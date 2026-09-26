"""
Private S3 access for My Research.

Objects are never public: the browser only ever receives short-lived presigned URLs issued after
Django has authorized the request. Keys are opaque (workspace UUID + file UUID + sanitized name),
so moving or renaming a file in the UI only changes database metadata.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any
from urllib.parse import quote

from django.conf import settings

logger = logging.getLogger(__name__)


class ResearchStorageError(Exception):
    """S3 call failed for a reason other than a missing object (treat as temporary)."""


class ObjectNotFound(ResearchStorageError):
    pass


def bucket_name() -> str:
    return (getattr(settings, "MY_RESEARCH_S3_BUCKET", "") or getattr(settings, "AWS_STORAGE_BUCKET_NAME", "") or "").strip()


def storage_configured() -> bool:
    return bool(bucket_name())


def build_object_key(workspace_id, file_id, safe_filename: str) -> str:
    prefix = (getattr(settings, "MY_RESEARCH_S3_PREFIX", "research") or "research").strip("/")
    return f"{prefix}/workspaces/{workspace_id}/files/{file_id}/{safe_filename}"


@lru_cache(maxsize=1)
def _cached_client():
    import boto3
    from botocore.config import Config

    region = getattr(settings, "AWS_S3_REGION_NAME", None) or "ap-south-1"
    return boto3.client(
        "s3",
        region_name=region,
        aws_access_key_id=getattr(settings, "AWS_ACCESS_KEY_ID", None) or None,
        aws_secret_access_key=getattr(settings, "AWS_SECRET_ACCESS_KEY", None) or None,
        endpoint_url=f"https://s3.{region}.amazonaws.com",
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "virtual"},
            # Newer botocore adds CRC32 checksum params to presigned PUTs by default, which browsers
            # cannot satisfy. Only send checksums we explicitly request.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


def _client():
    return _cached_client()


def _is_not_found(exc: Exception) -> bool:
    response = getattr(exc, "response", None) or {}
    code = str((response.get("Error") or {}).get("Code") or "")
    status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    return code in {"404", "NoSuchKey", "NotFound", "NoSuchUpload"} or status == 404


def _sse_params() -> dict[str, str]:
    sse = (getattr(settings, "MY_RESEARCH_S3_SSE", "") or "").strip()
    if not sse:
        return {}
    params = {"ServerSideEncryption": sse}
    kms_key = (getattr(settings, "MY_RESEARCH_S3_KMS_KEY_ID", "") or "").strip()
    if sse == "aws:kms" and kms_key:
        params["SSEKMSKeyId"] = kms_key
    return params


def _sse_headers() -> dict[str, str]:
    params = _sse_params()
    headers = {}
    if "ServerSideEncryption" in params:
        headers["x-amz-server-side-encryption"] = params["ServerSideEncryption"]
    if "SSEKMSKeyId" in params:
        headers["x-amz-server-side-encryption-aws-kms-key-id"] = params["SSEKMSKeyId"]
    return headers


def uses_kms() -> bool:
    return (getattr(settings, "MY_RESEARCH_S3_SSE", "") or "").strip() == "aws:kms"


def presign_put(key: str, *, content_type: str, expires_in: int, checksum_sha256_b64: str = "") -> tuple[str, dict[str, str]]:
    """Presigned single PUT. Returns the URL and the exact headers the browser must send."""
    params: dict[str, Any] = {"Bucket": bucket_name(), "Key": key, "ContentType": content_type, **_sse_params()}
    headers = {"Content-Type": content_type, **_sse_headers()}
    if checksum_sha256_b64:
        params["ChecksumSHA256"] = checksum_sha256_b64
        headers["x-amz-checksum-sha256"] = checksum_sha256_b64
    try:
        url = _client().generate_presigned_url("put_object", Params=params, ExpiresIn=expires_in, HttpMethod="PUT")
    except Exception as exc:
        raise ResearchStorageError(str(exc)) from exc
    return url, headers


def create_multipart_upload(key: str, *, content_type: str) -> str:
    try:
        resp = _client().create_multipart_upload(
            Bucket=bucket_name(), Key=key, ContentType=content_type, **_sse_params()
        )
    except Exception as exc:
        raise ResearchStorageError(str(exc)) from exc
    return resp["UploadId"]


def presign_upload_part(key: str, *, upload_id: str, part_number: int, expires_in: int) -> str:
    try:
        return _client().generate_presigned_url(
            "upload_part",
            Params={"Bucket": bucket_name(), "Key": key, "UploadId": upload_id, "PartNumber": part_number},
            ExpiresIn=expires_in,
            HttpMethod="PUT",
        )
    except Exception as exc:
        raise ResearchStorageError(str(exc)) from exc


def complete_multipart_upload(key: str, *, upload_id: str, parts: list[dict[str, Any]]) -> None:
    try:
        _client().complete_multipart_upload(
            Bucket=bucket_name(),
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": [{"PartNumber": p["part_number"], "ETag": p["etag"]} for p in parts]},
        )
    except Exception as exc:
        if _is_not_found(exc):
            raise ObjectNotFound(str(exc)) from exc
        raise ResearchStorageError(str(exc)) from exc


def list_uploaded_parts(key: str, *, upload_id: str) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    marker = 0
    try:
        while True:
            resp = _client().list_parts(Bucket=bucket_name(), Key=key, UploadId=upload_id, PartNumberMarker=marker)
            for part in resp.get("Parts", []):
                parts.append({"part_number": part["PartNumber"], "etag": part["ETag"], "size": part["Size"]})
            if not resp.get("IsTruncated"):
                return parts
            marker = resp.get("NextPartNumberMarker", 0)
    except Exception as exc:
        if _is_not_found(exc):
            raise ObjectNotFound(key) from exc
        raise ResearchStorageError(str(exc)) from exc


def abort_multipart_upload(key: str, *, upload_id: str) -> None:
    try:
        _client().abort_multipart_upload(Bucket=bucket_name(), Key=key, UploadId=upload_id)
    except Exception as exc:
        if _is_not_found(exc):
            return
        raise ResearchStorageError(str(exc)) from exc


def head_object(key: str, *, with_checksum: bool = False) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"Bucket": bucket_name(), "Key": key}
    if with_checksum:
        kwargs["ChecksumMode"] = "ENABLED"
    try:
        return _client().head_object(**kwargs)
    except Exception as exc:
        if _is_not_found(exc):
            raise ObjectNotFound(key) from exc
        raise ResearchStorageError(str(exc)) from exc


def read_prefix(key: str, length: int) -> bytes:
    if length <= 0:
        return b""
    try:
        resp = _client().get_object(Bucket=bucket_name(), Key=key, Range=f"bytes=0-{length - 1}")
        return resp["Body"].read(length)
    except Exception as exc:
        if _is_not_found(exc):
            raise ObjectNotFound(key) from exc
        raise ResearchStorageError(str(exc)) from exc


def delete_object(key: str) -> None:
    try:
        _client().delete_object(Bucket=bucket_name(), Key=key)
    except Exception as exc:
        if _is_not_found(exc):
            return
        raise ResearchStorageError(str(exc)) from exc


def content_disposition(disposition: str, filename: str) -> str:
    safe = "".join(ch for ch in (filename or "download") if ch >= " " and ch not in '"\\') or "download"
    ascii_name = safe.encode("ascii", "ignore").decode("ascii").strip() or "download"
    kind = "inline" if disposition == "inline" else "attachment"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(safe)}"


def presign_get(key: str, *, filename: str, disposition: str, content_type: str, expires_in: int) -> str:
    params = {
        "Bucket": bucket_name(),
        "Key": key,
        "ResponseContentDisposition": content_disposition(disposition, filename),
        "ResponseContentType": content_type,
        "ResponseCacheControl": "private, no-store",
    }
    try:
        return _client().generate_presigned_url("get_object", Params=params, ExpiresIn=expires_in)
    except Exception as exc:
        raise ResearchStorageError(str(exc)) from exc

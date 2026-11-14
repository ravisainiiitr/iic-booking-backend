"""Presigned S3 helpers for any bucket (results or My Research), using the My Research client and settings.

My Research's own helpers (``iic_booking.my_research.storage``) are bound to the research bucket; the outbox reuses them
directly. The Equipment PC multipart (results bucket) and the speed test use these bucket-explicit variants.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings

from iic_booking.my_research import storage as research_storage

ObjectNotFound = research_storage.ObjectNotFound
StorageError = research_storage.ResearchStorageError


def results_bucket() -> str:
    return (getattr(settings, "AWS_STORAGE_BUCKET_NAME", "") or "").strip()


def _client():
    return research_storage._client()


def _wrap(exc: Exception) -> Exception:
    if research_storage._is_not_found(exc):
        return ObjectNotFound(str(exc))
    return StorageError(str(exc))


def presign_get(bucket: str, key: str, *, expires_in: int) -> str:
    try:
        return _client().generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires_in)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


def presign_put(bucket: str, key: str, *, expires_in: int) -> str:
    try:
        return _client().generate_presigned_url(
            "put_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires_in, HttpMethod="PUT"
        )
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


def presign_put_verified(bucket: str, key: str, *, checksum_sha256_b64: str, expires_in: int) -> tuple[str, dict[str, str]]:
    """Single PUT S3 rejects unless the body matches the SHA-256. Returns the URL and the headers to send."""
    params: dict[str, Any] = {"Bucket": bucket, "Key": key, "ContentType": "application/octet-stream"}
    headers = {"Content-Type": "application/octet-stream"}
    if checksum_sha256_b64:
        params["ChecksumSHA256"] = checksum_sha256_b64
        headers["x-amz-checksum-sha256"] = checksum_sha256_b64
    try:
        url = _client().generate_presigned_url("put_object", Params=params, ExpiresIn=expires_in, HttpMethod="PUT")
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc
    return url, headers


def create_multipart(bucket: str, key: str) -> str:
    try:
        return _client().create_multipart_upload(Bucket=bucket, Key=key, ContentType="application/octet-stream")["UploadId"]
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


def presign_part(bucket: str, key: str, *, upload_id: str, part_number: int, expires_in: int) -> str:
    try:
        return _client().generate_presigned_url(
            "upload_part",
            Params={"Bucket": bucket, "Key": key, "UploadId": upload_id, "PartNumber": part_number},
            ExpiresIn=expires_in,
            HttpMethod="PUT",
        )
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


def list_parts(bucket: str, key: str, *, upload_id: str) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    marker = 0
    try:
        while True:
            resp = _client().list_parts(Bucket=bucket, Key=key, UploadId=upload_id, PartNumberMarker=marker)
            for part in resp.get("Parts", []):
                parts.append({"part_number": part["PartNumber"], "etag": part["ETag"], "size": part["Size"]})
            if not resp.get("IsTruncated"):
                return parts
            marker = resp.get("NextPartNumberMarker", 0)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


def complete_multipart(bucket: str, key: str, *, upload_id: str, parts: list[dict[str, Any]]) -> None:
    try:
        _client().complete_multipart_upload(
            Bucket=bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": [{"PartNumber": p["part_number"], "ETag": p["etag"]} for p in parts]},
        )
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


def head(bucket: str, key: str) -> dict[str, Any]:
    try:
        return _client().head_object(Bucket=bucket, Key=key)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


def delete(bucket: str, key: str) -> None:
    try:
        _client().delete_object(Bucket=bucket, Key=key)
    except Exception as exc:  # noqa: BLE001
        if research_storage._is_not_found(exc):
            return
        raise _wrap(exc) from exc


def parse_parts(raw) -> list[dict[str, Any]] | None:
    """``[{n, etag}]`` (or ``part_number``/``ETag``) sorted by part number; None when malformed."""
    if not isinstance(raw, list) or not raw or len(raw) > 10_000:
        return None
    parts = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            return None
        try:
            number = int(item.get("n") if item.get("n") is not None else item.get("part_number"))
        except (TypeError, ValueError):
            return None
        etag = str(item.get("etag") or item.get("ETag") or "").strip()
        if number < 1 or number > 10_000 or not etag or number in seen:
            return None
        seen.add(number)
        parts.append({"part_number": number, "etag": etag if etag.startswith('"') else f'"{etag}"'})
    return sorted(parts, key=lambda p: p["part_number"])

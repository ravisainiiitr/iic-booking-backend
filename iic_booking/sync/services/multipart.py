"""Phase 3 (RAA large data): the Equipment PC (DSA >= 1.0.11) uploads raw data straight to the results bucket.

Files of 100 MiB or more use presigned multipart (64 MiB parts) with the same shapes as the Analysis PC outbox
(``init/``, ``parts/``, ``state/``, ``complete/``); smaller files keep ``uploads/chunk/``. ``complete/`` records the file
against the booking exactly as ``uploads/complete/`` does: an AgentUploadSession, then ResultProcessingService
.import_results (EquipmentResult + ResultAttachment), with the attachment's ``s3_key`` already set to the object under
``Results/{vid}/b{pk}-{created}/`` (``build_results_s3_key``), so nothing is copied through the portal.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from typing import Any

from django.db import transaction
from django.utils import timezone

from iic_booking.remote_analysis.large_data import s3
from iic_booking.remote_analysis.large_data.common import (
    MAX_PART_URLS_PER_CALL,
    PART_SIZE,
    URL_LIFETIME_SECONDS,
    TransferError,
    as_int,
    check_size_limit,
    clean_rel_path,
    clean_sha256,
    part_size_for,
)
from iic_booking.sync.models import DirectResultUpload, DirectResultUploadStatus

logger = logging.getLogger(__name__)

MAX_FILES = 5000


def _hex_to_b64(hex_digest: str) -> str:
    import base64

    return base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii") if hex_digest else ""


def _booking(data: dict[str, Any]):
    from iic_booking.equipment.models import Booking
    from iic_booking.sync.services.booking_result_gate import ensure_booking_accepts_results

    booking_id = as_int(data.get("booking_id"))
    booking = Booking.objects.select_related("equipment").filter(pk=booking_id).first() if booking_id else None
    if booking is None:
        raise TransferError("booking_missing", "booking_id is required and must exist.", 404)
    ensure_booking_accepts_results(booking)
    return booking


def _payload(row: DirectResultUpload) -> dict[str, Any]:
    done = row.status == DirectResultUploadStatus.DONE
    url = headers = None
    if not done and not row.upload_id:
        url, headers = s3.presign_put_verified(
            s3.results_bucket(), row.key, checksum_sha256_b64=_hex_to_b64(row.sha256), expires_in=URL_LIFETIME_SECONDS
        )
    return {
        "file_id": str(row.id),
        "rel_path": row.rel_path,
        "key": row.key,
        "upload_id": row.upload_id or None,
        "part_size": row.part_size,
        "single_put_url": url,
        "put_headers": headers,
        "done": done,
        "agent_upload_id": str(row.agent_upload_id),
    }


def init(agent, data: dict[str, Any]) -> dict[str, Any]:
    from iic_booking.sync.services.results_s3 import booking_results_folder, build_results_s3_key

    bucket = s3.results_bucket()
    if not bucket:
        raise TransferError("storage_not_configured", "The results bucket is not configured.", 503)
    booking = _booking(data)
    raw = data.get("files")
    if not isinstance(raw, list) or not raw or len(raw) > MAX_FILES:
        raise TransferError("invalid_files", f"files must be a list of 1 to {MAX_FILES} entries.")
    files = []
    for item in raw:
        rel = clean_rel_path((item or {}).get("rel_path")) if isinstance(item, dict) else None
        sha = clean_sha256(item.get("sha256")) if isinstance(item, dict) else None
        if rel is None or not sha:
            raise TransferError("invalid_files", "Each file needs a relative rel_path, size and hex sha256.")
        agent_upload_id = None
        if item.get("agent_upload_id"):
            try:
                agent_upload_id = uuid.UUID(str(item["agent_upload_id"]))
            except (TypeError, ValueError):
                raise TransferError("invalid_files", "agent_upload_id must be a UUID.") from None
        files.append({"rel_path": rel, "size": as_int(item.get("size")), "sha256": sha, "agent_upload_id": agent_upload_id})
    check_size_limit(sum(f["size"] for f in files))
    vid = (booking.virtual_booking_id or "").strip() or f"booking-{booking.pk}"
    folder = booking_results_folder(booking)
    rows = []
    with transaction.atomic():
        for item in files:
            row = (
                DirectResultUpload.objects.select_for_update()
                .filter(sync_agent=agent, booking=booking, rel_path=item["rel_path"], sha256=item["sha256"], size=item["size"])
                .exclude(status=DirectResultUploadStatus.FAILED)
                .order_by("-created_at")
                .first()
            )
            if row is None:
                name = item["rel_path"].rsplit("/", 1)[-1]
                row = DirectResultUpload(
                    sync_agent=agent,
                    booking=booking,
                    equipment_id=as_int(data.get("equipment_id")) or booking.equipment_id,
                    agent_upload_id=item["agent_upload_id"] or uuid.uuid4(),
                    rel_path=item["rel_path"],
                    file_name=name[:500],
                    size=item["size"],
                    sha256=item["sha256"],
                    key=build_results_s3_key(vid, name, booking_folder=folder),
                    part_size=part_size_for(item["size"]),
                )
                if item["size"] >= PART_SIZE:
                    row.upload_id = s3.create_multipart(bucket, row.key)
                row.save()
            rows.append(row)
    return {"booking_id": booking.pk, "files": [_payload(r) for r in rows]}


def _row(agent, file_id) -> DirectResultUpload:
    try:
        row = DirectResultUpload.objects.filter(sync_agent=agent, pk=file_id).first()
    except Exception:  # noqa: BLE001 - malformed uuid
        row = None
    if row is None:
        raise TransferError("unknown_file", "Unknown file_id; call init/ again.", 404)
    return row


def parts(agent, data: dict[str, Any]) -> dict[str, Any]:
    row = _row(agent, data.get("file_id"))
    if row.status == DirectResultUploadStatus.DONE:
        return {"file_id": str(row.id), "urls": {}, "done": True}
    if not row.upload_id:
        raise TransferError("single_put", "This file uses single_put_url; call init/ for a fresh one.", 409)
    numbers = data.get("part_numbers")
    needed = max(1, -(-row.size // row.part_size)) if row.part_size else 1
    if not isinstance(numbers, list) or not numbers or len(numbers) > MAX_PART_URLS_PER_CALL:
        raise TransferError("invalid_parts", f"part_numbers must list 1 to {MAX_PART_URLS_PER_CALL} parts.")
    urls = {}
    for raw in numbers:
        n = as_int(raw)
        if n < 1 or n > needed:
            raise TransferError("invalid_parts", f"Part {raw} is outside 1..{needed}.")
        urls[str(n)] = s3.presign_part(
            s3.results_bucket(), row.key, upload_id=row.upload_id, part_number=n, expires_in=URL_LIFETIME_SECONDS
        )
    return {"file_id": str(row.id), "urls": urls, "url_lifetime_seconds": URL_LIFETIME_SECONDS}


def state(agent, data: dict[str, Any]) -> dict[str, Any]:
    ids = data.get("file_ids")
    qs = DirectResultUpload.objects.filter(sync_agent=agent)
    if isinstance(ids, list) and ids:
        qs = qs.filter(pk__in=[str(i) for i in ids][:MAX_FILES])
    elif data.get("booking_id"):
        qs = qs.filter(booking_id=as_int(data.get("booking_id")))
    else:
        qs = qs.exclude(status=DirectResultUploadStatus.DONE)
    files = []
    bucket = s3.results_bucket()
    for row in qs.order_by("created_at")[:MAX_FILES]:
        done = row.status == DirectResultUploadStatus.DONE
        parts_done: list[int] = []
        restarted = False
        if not done and row.upload_id:
            try:
                parts_done = [p["part_number"] for p in s3.list_parts(bucket, row.key, upload_id=row.upload_id)]
            except s3.ObjectNotFound:
                row.upload_id = s3.create_multipart(bucket, row.key)
                row.save(update_fields=["upload_id"])
                restarted = True
        files.append(
            {
                "file_id": str(row.id),
                "rel_path": row.rel_path,
                "booking_id": row.booking_id,
                "done": done,
                "parts_done": sorted(parts_done),
                "upload_id": row.upload_id or None,
                "restarted": restarted,
            }
        )
    return {"files": files}


def complete(agent, data: dict[str, Any], *, correlation_id=None) -> dict[str, Any]:
    row = _row(agent, data.get("file_id"))
    if row.status == DirectResultUploadStatus.DONE:
        return {"file_id": str(row.id), "done": True, "attachment_id": str(row.attachment_id) if row.attachment_id else None}
    sha = clean_sha256(data.get("sha256"))
    if sha and sha != row.sha256:
        raise TransferError("checksum_mismatch", "sha256 differs from the one sent to init/; call init/ again.", 409)
    bucket = s3.results_bucket()
    if row.upload_id:
        part_list = s3.parse_parts(data.get("parts"))
        if part_list is None:
            raise TransferError("invalid_parts", "parts must be [{n, etag}] for every uploaded part.")
        try:
            s3.complete_multipart(bucket, row.key, upload_id=row.upload_id, parts=part_list)
        except s3.ObjectNotFound:
            pass  # already completed (a lost response); the head check decides
    try:
        head = s3.head(bucket, row.key)
    except s3.ObjectNotFound as exc:
        raise TransferError("not_uploaded", "The file is not in storage yet.", 409) from exc
    size = int(head.get("ContentLength") or 0)
    if size != row.size:
        raise TransferError("size_mismatch", f"Stored size {size} != {row.size}; upload the file again.", 409)
    attachment_id = _record(agent, row, correlation_id=correlation_id)
    return {"file_id": str(row.id), "done": True, "attachment_id": attachment_id}


@transaction.atomic
def _record(agent, row: DirectResultUpload, *, correlation_id=None) -> str | None:
    """Same rows as uploads/complete/ + its auto-import, with the S3 key already in place."""
    from iic_booking.sync.models import AgentUploadSession, AgentUploadSessionStatus, ResultAttachment
    from iic_booking.sync.services.result_processing import ResultProcessingService

    row = DirectResultUpload.objects.select_for_update().get(pk=row.pk)
    if row.status == DirectResultUploadStatus.DONE:
        return str(row.attachment_id) if row.attachment_id else None
    booking = row.booking
    if booking is None:
        raise TransferError("booking_missing", "The booking no longer exists.", 404)
    now = timezone.now()
    session, _ = AgentUploadSession.objects.get_or_create(
        sync_agent=agent,
        agent_upload_id=row.agent_upload_id,
        defaults={
            "equipment_id": row.equipment_id or booking.equipment_id,
            "booking": booking,
            "file_name": row.file_name,
            "relative_path": row.rel_path,
            "expected_size": row.size,
            "expected_chunk_count": 0,
            "chunk_size": int(min(row.part_size, 2**31 - 1)),
            "resume_token": secrets.token_urlsafe(24),
            "server_path": "",
            "bytes_received": row.size,
            "status": AgentUploadSessionStatus.COMPLETED,
            "expires_at": now,
            "completed_at": now,
            "correlation_id": correlation_id,
        },
    )
    equipment_id = row.equipment_id or booking.equipment_id
    result = ResultProcessingService().import_results(
        agent,
        {
            "agent_upload_id": str(row.agent_upload_id),
            "booking_id": int(booking.pk),
            "equipment_id": int(equipment_id),
            "upload_session_id": str(session.id),
            "file_name": row.file_name,
            "relative_path": row.rel_path,
            "size_bytes": row.size,
            "sha256": row.sha256,
            "storage_path": "",
            "parser_used": "direct_s3_multipart",
            "metadata": {},
            "measurements": [],
        },
        correlation_id=correlation_id,
    )
    attachment = None
    if result.get("attachment_id"):
        attachment = ResultAttachment.objects.filter(pk=result["attachment_id"]).first()
    if attachment is None:
        attachment = ResultAttachment.objects.filter(upload_session=session).order_by("-id").first()
    if attachment is not None and not (attachment.s3_key or "").strip():
        attachment.s3_key = row.key
        attachment.storage_path = ""
        attachment.save(update_fields=["s3_key", "storage_path"])
    row.status = DirectResultUploadStatus.DONE
    row.attachment = attachment
    row.completed_at = now
    row.bytes_done = row.size
    row.save(update_fields=["status", "attachment", "completed_at", "bytes_done"])
    try:
        from iic_booking.equipment.remote_analysis_integration.raw_staging import _listing_cache_key
        from django.core.cache import cache

        vid = (booking.virtual_booking_id or "").strip() or f"booking-{booking.pk}"
        cache.delete_many([_listing_cache_key(vid), _listing_cache_key(vid, True)])
    except Exception:  # noqa: BLE001
        pass
    return str(attachment.pk) if attachment is not None else None

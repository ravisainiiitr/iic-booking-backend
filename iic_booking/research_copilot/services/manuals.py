"""
Equipment manual PDFs for Research Copilot.

Upload flow: validate (header, size, pages, encryption), store as a private S3 object, register a
KnowledgeDocument (category operator_manual, equipment-scoped), then extract text and index it
page-aware in the background. The browser never receives a bucket URL except short-lived presigned
GETs issued after authorization.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from typing import Any

from django.conf import settings
from django.db import transaction

from iic_booking.research_copilot.models import (
    DocumentCategory,
    DocumentStatus,
    IndexStatus,
    KnowledgeChunk,
    KnowledgeDocument,
    SecurityLevel,
)
from iic_booking.research_copilot.services import pdf_extract

logger = logging.getLogger(__name__)

MANUAL_CATEGORY = DocumentCategory.OPERATOR_MANUAL
MANUAL_SOURCE_TYPE = "pdf"
ALLOWED_SECURITY_LEVELS = {
    SecurityLevel.PUBLIC,
    SecurityLevel.AUTHENTICATED,
    SecurityLevel.OPERATOR,
    SecurityLevel.DEPT_ADMIN,
    SecurityLevel.ADMIN,
}
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")


class ManualError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def _storage():
    from iic_booking.my_research import storage

    return storage


def storage_configured() -> bool:
    return _storage().storage_configured()


def build_manual_key(equipment_id: int, document_id) -> str:
    prefix = (getattr(settings, "RESEARCH_COPILOT_MANUAL_PREFIX", "") or "research/copilot-manuals").strip("/")
    return f"{prefix}/{int(equipment_id)}/{document_id}.pdf"


def safe_filename(name: str) -> str:
    base = (name or "manual.pdf").replace("\\", "/").rsplit("/", 1)[-1]
    base = _SAFE_NAME.sub("_", base).strip(" .") or "manual.pdf"
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    return base[:200]


def _put_object(key: str, data: bytes) -> None:
    storage = _storage()
    try:
        storage._client().put_object(
            Bucket=storage.bucket_name(),
            Key=key,
            Body=data,
            ContentType="application/pdf",
            **storage._sse_params(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("copilot manual upload to S3 failed")
        raise ManualError("STORAGE_UNAVAILABLE", "The manual could not be stored. Try again later.", 503) from exc


def _get_object(key: str) -> bytes:
    storage = _storage()
    try:
        resp = storage._client().get_object(Bucket=storage.bucket_name(), Key=key)
        return resp["Body"].read(pdf_extract.max_bytes() + 1)
    except Exception as exc:  # noqa: BLE001
        raise ManualError("STORAGE_UNAVAILABLE", "The stored manual could not be read.", 503) from exc


def upload_manual(
    *,
    data: bytes,
    filename: str,
    equipment,
    title: str = "",
    security_level: str = SecurityLevel.AUTHENTICATED,
    version: str = "",
    created_by=None,
) -> tuple[KnowledgeDocument, bool]:
    """Validate, store and register a manual. Returns (document, duplicate)."""
    if not storage_configured():
        raise ManualError("STORAGE_NOT_CONFIGURED", "Manual storage is not configured.", 503)
    if security_level not in ALLOWED_SECURITY_LEVELS:
        raise ManualError("INVALID_SECURITY_LEVEL", "Unknown security level.")
    try:
        info = pdf_extract.validate_pdf(data)
    except pdf_extract.PdfRejected as exc:
        raise ManualError(exc.code, exc.message) from exc

    digest = hashlib.sha256(data).hexdigest()
    existing = (
        KnowledgeDocument.objects.filter(
            equipment_id=equipment.pk,
            file_sha256=digest,
            category=MANUAL_CATEGORY,
        )
        .exclude(status=DocumentStatus.ARCHIVED)
        .first()
    )
    if existing is not None:
        return existing, True

    doc_id = uuid.uuid4()
    key = build_manual_key(equipment.pk, doc_id)
    _put_object(key, data)
    clean_name = safe_filename(filename)
    doc = KnowledgeDocument.objects.create(
        id=doc_id,
        title=(title or clean_name.rsplit(".", 1)[0] or f"{equipment.name} manual")[:512],
        source_type=MANUAL_SOURCE_TYPE,
        category=MANUAL_CATEGORY,
        security_level=security_level,
        status=DocumentStatus.DRAFT,
        index_status=IndexStatus.PENDING,
        version=(version or "1.0")[:64],
        equipment_id=equipment.pk,
        department_id=None,
        tags=["manual", f"equipment:{equipment.pk}"],
        source_file_key=key,
        original_filename=clean_name,
        file_sha256=digest,
        file_size=len(data),
        page_count=info.page_count,
        created_by=created_by,
    )
    dispatch_processing(doc.id)
    return doc, False


def dispatch_processing(document_id) -> None:
    """Queue extraction + indexing after commit; run inline if the broker is unavailable."""

    def _send():
        from iic_booking.research_copilot.tasks import process_manual_task

        try:
            process_manual_task.delay(str(document_id))
        except Exception:  # noqa: BLE001
            logger.warning("copilot manual: Celery unavailable, processing inline", exc_info=True)
            process_manual(document_id)

    transaction.on_commit(_send)


def process_manual(document_id) -> dict[str, Any]:
    """Download, extract per-page text and index. Safe to re-run (re-index)."""
    from iic_booking.research_copilot.services.ingestion import PAGE_BREAK, content_hash, index_document

    doc = KnowledgeDocument.objects.filter(id=document_id).first()
    if doc is None or not doc.source_file_key:
        return {"ok": False, "error": "NOT_FOUND"}
    if doc.status == DocumentStatus.ARCHIVED:
        return {"ok": False, "error": "ARCHIVED"}

    doc.index_status = IndexStatus.INDEXING
    doc.error_message = ""
    doc.save(update_fields=["index_status", "error_message", "updated_at"])
    try:
        text = pdf_extract.extract_pages(_get_object(doc.source_file_key))
    except (pdf_extract.PdfRejected, ManualError) as exc:
        had_chunks = KnowledgeChunk.objects.filter(document=doc).exists()
        doc.index_status = IndexStatus.FAILED
        if not had_chunks:
            doc.status = DocumentStatus.FAILED
        doc.error_message = getattr(exc, "message", str(exc))[:2000]
        doc.save(update_fields=["index_status", "status", "error_message", "updated_at"])
        return {"ok": False, "error": getattr(exc, "code", "FAILED")}

    doc.content_text = PAGE_BREAK.join(text.pages)
    doc.content_hash = content_hash(doc.content_text)
    doc.page_count = text.page_count
    doc.save(update_fields=["content_text", "content_hash", "page_count", "updated_at"])
    job = index_document(doc)
    doc.refresh_from_db()
    return {"ok": job.status == IndexStatus.INDEXED, "chunks": doc.chunk_count, "status": doc.index_status}


def archive_manual(doc: KnowledgeDocument) -> KnowledgeDocument:
    """Hide from retrieval immediately. The S3 object is kept for audit; re-index re-activates it."""
    from iic_booking.research_copilot.services.vector_store import get_vector_store

    doc.status = DocumentStatus.ARCHIVED
    doc.index_status = IndexStatus.STALE
    doc.chunk_count = 0
    doc.save(update_fields=["status", "index_status", "chunk_count", "updated_at"])
    get_vector_store().delete_document(doc.id)
    return doc


def reindex_manual(doc: KnowledgeDocument) -> KnowledgeDocument:
    if doc.status == DocumentStatus.ARCHIVED:
        doc.status = DocumentStatus.DRAFT
    doc.index_status = IndexStatus.PENDING
    doc.save(update_fields=["status", "index_status", "updated_at"])
    dispatch_processing(doc.id)
    return doc


def presigned_url(doc: KnowledgeDocument) -> str:
    expires = int(getattr(settings, "RESEARCH_COPILOT_MANUAL_URL_EXPIRY_SECONDS", 300) or 300)
    return _storage().presign_get(
        doc.source_file_key,
        filename=doc.original_filename or "manual.pdf",
        disposition="inline",
        content_type="application/pdf",
        expires_in=expires,
    )


def serialize_manual(doc: KnowledgeDocument, *, equipment_name: str | None = None) -> dict[str, Any]:
    return {
        "id": str(doc.id),
        "title": doc.title,
        "equipment_id": doc.equipment_id,
        "equipment_name": equipment_name,
        "original_filename": doc.original_filename,
        "file_size": doc.file_size,
        "page_count": doc.page_count,
        "security_level": doc.security_level,
        "version": doc.version,
        "status": doc.status,
        "index_status": doc.index_status,
        "chunk_count": doc.chunk_count,
        "error_message": doc.error_message,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
        "indexed_at": doc.indexed_at.isoformat() if doc.indexed_at else None,
        "has_file": bool(doc.source_file_key),
    }


def manuals_for_equipment(equipment_id: int, *, include_archived: bool = False):
    qs = KnowledgeDocument.objects.filter(equipment_id=int(equipment_id), category=MANUAL_CATEGORY)
    if not include_archived:
        qs = qs.exclude(status=DocumentStatus.ARCHIVED)
    return qs.order_by("-created_at")


def active_manual_count(equipment_id: int) -> int:
    return KnowledgeDocument.objects.filter(
        equipment_id=int(equipment_id),
        category=MANUAL_CATEGORY,
        status=DocumentStatus.ACTIVE,
        chunk_count__gt=0,
    ).count()

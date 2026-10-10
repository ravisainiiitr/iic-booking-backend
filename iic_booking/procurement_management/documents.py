"""Private document storage: PDF / JPG / PNG only, content-sniffed, size-capped, hashed, never public.

Files are written through the default storage (private S3 in production, signed URLs never handed out) and are
only readable through the authorised download view, which re-checks object visibility on every request.
"""

from __future__ import annotations

import hashlib
import os
import uuid

from django.conf import settings
from django.db import transaction

from . import access, audit
from . import constants as c
from .errors import ProcurementError
from .models import ProcurementDocument

MAGIC = {
    "application/pdf": (b"%PDF-",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
}
LINK_FIELDS = ("purchase_request", "procurement_record", "invoice", "asset", "proposal", "amc_record", "quotation")


def max_bytes() -> int:
    return int(getattr(settings, "PROCUREMENT_MAX_DOCUMENT_MB", 15)) * 1024 * 1024


def _safe_name(name: str) -> str:
    base = os.path.basename(str(name or "document")).replace("\x00", "")
    return base[:255] or "document"


def validate_upload(upload) -> tuple[str, str]:
    """Returns (content_type, sha256). Extension, declared size and magic bytes must all agree."""
    if upload is None:
        raise ProcurementError("Attach a file.", code="file_required", field="file")
    name = _safe_name(getattr(upload, "name", ""))
    ext = os.path.splitext(name)[1].lower()
    content_type = c.ALLOWED_DOCUMENT_EXTENSIONS.get(ext)
    if content_type is None:
        raise ProcurementError("Only PDF, JPG and PNG files are accepted.", code="invalid_file_type", field="file")
    size = getattr(upload, "size", 0) or 0
    if size <= 0:
        raise ProcurementError("The file is empty.", code="empty_file", field="file")
    if size > max_bytes():
        raise ProcurementError("The file is too large.", code="file_too_large", field="file")
    upload.seek(0)
    head = upload.read(16)
    if not any(head.startswith(sig) for sig in MAGIC[content_type]):
        raise ProcurementError("The file content does not match its type.", code="invalid_file_content", field="file")
    upload.seek(0)
    digest = hashlib.sha256()
    for chunk in upload.chunks():
        digest.update(chunk)
    upload.seek(0)
    return content_type, digest.hexdigest()


@transaction.atomic
def create_document(
    scope,
    department,
    upload,
    *,
    doc_type: str,
    links: dict | None = None,
    description: str = "",
    page_group=None,
    page_number: int = 1,
    request=None,
) -> ProcurementDocument:
    if doc_type not in c.DocumentType.values:
        raise ProcurementError("Unknown document type.", code="invalid_choice", field="doc_type")
    content_type, sha = validate_upload(upload)
    links = links or {}
    for key, obj in links.items():
        if key not in LINK_FIELDS:
            raise ProcurementError("Unknown document link.", code="invalid")
        if obj is not None and obj.department_id != department.pk:
            raise ProcurementError("The document and record belong to different departments.", code="department_mismatch")
    doc = ProcurementDocument(
        department=department,
        doc_type=doc_type,
        original_name=_safe_name(upload.name),
        content_type=content_type,
        size_bytes=upload.size,
        sha256=sha,
        page_group=page_group,
        page_number=max(1, int(page_number or 1)),
        description=str(description or "")[:255],
        uploaded_by=scope.user,
        **links,
    )
    doc.file.save(_safe_name(upload.name), upload, save=False)
    doc.save()
    audit.record(
        scope.user,
        "document.uploaded",
        doc,
        new={"doc_type": doc_type, "name": doc.original_name, "sha256": sha, **{f"{k}_id": getattr(v, "pk", None) for k, v in links.items()}},
        request=request,
    )
    return doc


def parse_page_group(raw):
    if raw in (None, ""):
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        raise ProcurementError("page_group must be a UUID.", code="invalid", field="page_group")


def can_view(scope, doc: ProcurementDocument) -> bool:
    """Visibility follows the linked record; unlinked documents are department-wide only."""
    from .models import PurchaseRequest

    dept = doc.department_id
    if dept not in scope.department_ids():
        return False
    if scope.dept_wide(dept):
        return True
    if doc.uploaded_by_id == scope.user.pk:
        return True
    if doc.purchase_request_id:
        return PurchaseRequest.objects.filter(access.visible_requests_q(scope), pk=doc.purchase_request_id).exists()
    lab_eq = set(scope.lab_equipment)
    if doc.asset_id and doc.asset.equipment_id in lab_eq:
        return True
    if doc.amc_record_id and doc.amc_record.equipment_id in lab_eq:
        return True
    return False


@transaction.atomic
def archive_document(scope, doc: ProcurementDocument, reason: str, *, request=None) -> ProcurementDocument:
    from django.utils import timezone

    if not (scope.dept_wide(doc.department_id) or doc.uploaded_by_id == scope.user.pk):
        from .errors import forbidden

        raise forbidden()
    if doc.is_archived:
        raise ProcurementError("Already archived.", code="archived")
    doc.is_archived = True
    doc.archived_at = timezone.now()
    doc.archived_by = scope.user
    doc.archive_reason = reason
    doc.save(update_fields=["is_archived", "archived_at", "archived_by", "archive_reason", "updated_at"])
    audit.record(scope.user, "document.archived", doc, reason=reason, request=request)
    return doc

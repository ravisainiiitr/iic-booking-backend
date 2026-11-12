"""Private attachments: validated upload, authorised streaming download. No public or long-lived URL ever exists.

* Stored with Django's default storage (a private S3 bucket in production, ACLs off) under
  ``<IITR_CONNECT_S3_PREFIX>/c<community>/<uuid>/<safe name>``. Keys are opaque; the display name is metadata.
* The real type is sniffed from the first bytes (``my_research.file_policy.sniff_type``), never trusted from the
  extension or the browser. Only an allowlist is accepted: images, PDF, Office documents, plain text / CSV.
* Downloads re-check access on every request and are streamed through Django with ``nosniff``, ``no-store`` and
  ``attachment`` disposition (inline only for images and PDF). This is type validation, not malware scanning.
"""

from __future__ import annotations

import hashlib
import uuid

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.http import FileResponse
from django.http import Http404

from iic_booking.my_research.file_policy import InvalidFilename
from iic_booking.my_research.file_policy import clean_display_name
from iic_booking.my_research.file_policy import extension_of
from iic_booking.my_research.file_policy import sniff_type
from iic_booking.my_research.file_policy import storage_safe_name

from .. import access
from ..models import Community
from ..models import ConnectSettings
from ..models import DiscussionAttachment
from ..models import Visibility
from .threads import NotAllowed
from .threads import ThreadError

SNIFF_BYTES = 8192
IMAGE_TYPES = {"png": "image/png", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
OFFICE_ZIP_EXT = {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"}
OFFICE_OLE_EXT = {".doc", ".xls", ".ppt"}
TEXT_EXT = {".txt", ".csv", ".tsv", ".md", ".log", ".json", ".xml", ".dat", ".xy", ".asc"}
BLOCKED_TEXT_EXT = {".html", ".htm", ".svg", ".js", ".mjs", ".xhtml", ".php", ".sh", ".bat", ".ps1", ".py"}


def classify(name: str, head: bytes) -> tuple[str, str]:
    """(kind, served content type) or raise ThreadError. kind: image | pdf | office | text."""
    detected = sniff_type(head)
    ext = extension_of(name)
    if detected in IMAGE_TYPES:
        return "image", IMAGE_TYPES[detected]
    if detected == "pdf":
        return "pdf", "application/pdf"
    if detected == "zip" and ext in OFFICE_ZIP_EXT:
        return "office", "application/octet-stream"
    if detected == "ole" and ext in OFFICE_OLE_EXT:
        return "office", "application/octet-stream"
    if detected in ("text", "empty") and ext in TEXT_EXT and ext not in BLOCKED_TEXT_EXT:
        return "text", "text/plain; charset=utf-8"
    raise ThreadError(
        "This file type is not accepted. Attach images, PDF, Word / Excel / PowerPoint documents, or text and CSV files.",
        "file",
    )


def upload(user, community: Community, uploaded_file, *, inline: bool = False) -> DiscussionAttachment:
    if not access.can_post_in(user, community.pk):
        raise NotAllowed("You cannot upload files here.")
    limit_mb = ConnectSettings.load().max_attachment_mb
    size = getattr(uploaded_file, "size", 0) or 0
    if size <= 0:
        raise ThreadError("The file is empty.", "file")
    if size > limit_mb * 1024 * 1024:
        raise ThreadError(f"Files can be at most {limit_mb} MB.", "file")
    try:
        name = clean_display_name(getattr(uploaded_file, "name", "") or "file")
    except InvalidFilename as exc:
        raise ThreadError(str(exc), "file") from exc
    data = uploaded_file.read(limit_mb * 1024 * 1024 + 1)
    if len(data) > limit_mb * 1024 * 1024:
        raise ThreadError(f"Files can be at most {limit_mb} MB.", "file")
    kind, content_type = classify(name, data[:SNIFF_BYTES])
    if inline and kind != "image":
        raise ThreadError("Only images can be shown inside the text.", "file")
    attachment_id = uuid.uuid4()
    prefix = (getattr(settings, "IITR_CONNECT_S3_PREFIX", "connect") or "connect").strip("/")
    key = f"{prefix}/c{community.pk}/{attachment_id}/{storage_safe_name(name)}"
    stored_key = default_storage.save(key, ContentFile(data))
    return DiscussionAttachment.objects.create(
        id=attachment_id,
        uploader=user,
        community=community,
        storage_key=stored_key,
        original_name=name,
        content_type=content_type,
        sniffed_kind=kind,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        is_inline_image=inline and kind == "image",
    )


def get_readable_attachment_or_404(user, attachment_id) -> DiscussionAttachment:
    """An attachment is readable when its post is readable, or (unbound) when the caller uploaded it."""
    try:
        attachment_uuid = uuid.UUID(str(attachment_id))
    except (TypeError, ValueError) as exc:
        raise Http404 from exc
    attachment = DiscussionAttachment.objects.select_related("post", "post__thread").filter(pk=attachment_uuid).first()
    if attachment is None:
        raise Http404
    if attachment.post_id is None:
        if attachment.uploader_id != user.pk or not access.can_read_community(user, attachment.community_id):
            raise Http404
        return attachment
    if not access.visible_posts(user).filter(pk=attachment.post_id).exists():
        raise Http404
    if attachment.post.visibility != Visibility.VISIBLE and not access.can_moderate(user, attachment.community_id):
        raise Http404
    return attachment


def download_response(attachment: DiscussionAttachment, *, inline: bool = False) -> FileResponse:
    try:
        fh = default_storage.open(attachment.storage_key, "rb")
    except (FileNotFoundError, OSError) as exc:
        raise Http404 from exc
    show_inline = inline and attachment.sniffed_kind in ("image", "pdf")
    content_type = attachment.content_type if show_inline or attachment.sniffed_kind == "text" else "application/octet-stream"
    if attachment.sniffed_kind == "text" and not show_inline:
        content_type = "application/octet-stream"
    response = FileResponse(fh, as_attachment=not show_inline, filename=attachment.original_name, content_type=content_type)
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox"
    response["Cache-Control"] = "private, no-store, max-age=0"
    response["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    response["Content-Length"] = str(attachment.size)
    return response


def purge_unbound(older_than_hours: int = 24) -> int:
    from datetime import timedelta

    from django.utils import timezone

    cutoff = timezone.now() - timedelta(hours=older_than_hours)
    count = 0
    for attachment in DiscussionAttachment.objects.filter(post__isnull=True, created_at__lt=cutoff)[:500]:
        try:
            default_storage.delete(attachment.storage_key)
        except Exception:  # noqa: BLE001 - a missing object must not stop the purge
            pass
        attachment.delete()
        count += 1
    return count

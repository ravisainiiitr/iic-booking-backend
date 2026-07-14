"""Helpers for resolving equipment image paths in storage (S3 or local)."""

from __future__ import annotations

import logging
import mimetypes
import os
from typing import Iterable, Optional, Tuple

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

logger = logging.getLogger(__name__)


def local_equipment_image_path(stored_path: str) -> str:
    """Absolute path for the on-disk backup copy of an equipment image."""
    rel = (stored_path or "").strip().lstrip("/")
    return os.path.join(settings.MEDIA_ROOT, "equipment_images_local", rel)


def save_local_equipment_image_backup(stored_path: str, content: bytes) -> None:
    """Write a local backup so images survive S3 issues and dev restarts."""
    if not (stored_path and stored_path.strip() and content):
        return
    path = local_equipment_image_path(stored_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(content)


def local_equipment_image_exists(stored_path: str) -> bool:
    path = local_equipment_image_path(stored_path)
    return os.path.isfile(path) and os.path.getsize(path) > 0


def open_local_equipment_image_backup(stored_path: str) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    path = local_equipment_image_path(stored_path)
    if not os.path.isfile(path):
        return None, None, None
    try:
        with open(path, "rb") as fh:
            content = fh.read()
        if not content:
            return None, None, None
        content_type, _ = mimetypes.guess_type(stored_path)
        if not content_type:
            content_type = "image/jpeg"
        return content, stored_path, content_type
    except Exception:
        return None, None, None


def _allow_local_equipment_image_fallback() -> bool:
    """
    Local backups live under MEDIA_ROOT inside the container.

    In production that path is wiped on every image rebuild unless a named volume
    is mounted — so local-only "success" must never satisfy availability checks.
    """
    return bool(getattr(settings, "ALLOW_LOCAL_EQUIPMENT_IMAGE_FALLBACK", False))


def normalize_storage_path(path: str) -> str:
    """Return path suitable for storage with location=media. Avoids double 'media/' prefix."""
    if not path:
        return path
    path = (path or "").strip()
    if path.startswith("media/"):
        return path[6:]
    if path.startswith("media") and (len(path) == 5 or path[5:6] in ("/", "")):
        return path[5:].lstrip("/") or path
    return path


def storage_path_candidates(stored_path: str) -> Iterable[str]:
    """
    Yield plausible storage keys for a stored DB path.

    Historically we stored S3 keys sometimes with and sometimes without a leading `media/`.
    With S3Boto3Storage(location="media"), the name stored in the DB must NOT include `media/`
    or open/exists will look for media/media/... .
    Prefer the normalized (no media/) key first.
    """
    if not (stored_path and stored_path.strip()):
        return []

    raw = stored_path.strip().lstrip("/")
    normalized = normalize_storage_path(raw)

    candidates = []
    if normalized:
        candidates.append(normalized)
    if raw and raw != normalized:
        candidates.append(raw)
    # Legacy keys that were uploaded without the storage location prefix applied correctly.
    if normalized and not normalized.startswith("media/"):
        candidates.append("media/" + normalized)

    seen = set()
    out = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def open_storage_bytes_first_match(stored_path: str) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """
    Try to open the file from storage using candidate keys.
    Returns (content_bytes, resolved_path, content_type).
    """
    for key in storage_path_candidates(stored_path):
        if not key:
            continue
        try:
            with default_storage.open(key, "rb") as fh:
                content = fh.read()
            content_type, _ = mimetypes.guess_type(key)
            if not content_type:
                content_type = "image/jpeg"
            return content, key, content_type
        except Exception:
            continue
    return None, None, None


def resolve_storage_path_for_open(stored_path: str) -> Optional[str]:
    """
    Backwards-compatible helper: return the first candidate key that can be opened.
    """
    content, resolved_path, _content_type = open_storage_bytes_first_match(stored_path)
    if content is None or not resolved_path:
        return None
    return resolved_path


def open_via_field_storage(file_field) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """Open a FileField via its storage backend, trying legacy path variants."""
    if not file_field or not getattr(file_field, "name", None):
        return None, None, None

    storage = file_field.storage
    stored_path = (file_field.name or "").strip()
    if not stored_path:
        return None, None, None

    seen: set[str] = set()
    for key in storage_path_candidates(stored_path):
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            # Prefer open over exists: S3 exists() with a double-prefix key is a false miss,
            # and exists() can also lag briefly after PutObject.
            with storage.open(key, "rb") as fh:
                content = fh.read()
            if not content:
                continue
            content_type, _ = mimetypes.guess_type(key)
            if not content_type:
                content_type = "image/jpeg"
            return content, key, content_type
        except Exception:
            continue
    return None, None, None


def verify_file_field_in_storage(file_field) -> bool:
    """
    Return True if the FileField/ImageField object can be opened from storage.

    Uses the same path-candidate logic as open/read so a DB value like
    media/equipment_images/... still matches an object under location=media.
    """
    content, resolved_path, _ = open_via_field_storage(file_field)
    return bool(content and resolved_path)


def equipment_image_path_available(stored_path: str, storage=None) -> bool:
    """True when stored_path exists in configured storage (S3). Optional local fallback."""
    if not (stored_path and stored_path.strip()):
        return False
    if storage is not None:
        seen: set[str] = set()
        for key in storage_path_candidates(stored_path):
            if not key or key in seen:
                continue
            seen.add(key)
            try:
                with storage.open(key, "rb") as fh:
                    if fh.read(1):
                        return True
            except Exception:
                continue
    if _allow_local_equipment_image_fallback() and local_equipment_image_exists(
        normalize_storage_path(stored_path)
    ):
        return True
    if _allow_local_equipment_image_fallback() and local_equipment_image_exists(stored_path):
        return True
    return False


def equipment_image_available(file_field) -> bool:
    """True when the image exists in configured storage (S3). Optional local fallback."""
    if verify_file_field_in_storage(file_field):
        return True
    if not file_field or not getattr(file_field, "name", None):
        return False
    name = (file_field.name or "").strip()
    if not name:
        return False
    if _allow_local_equipment_image_fallback() and (
        local_equipment_image_exists(normalize_storage_path(name))
        or local_equipment_image_exists(name)
    ):
        return True
    return False


def get_equipment_image_storage_path(equipment) -> Optional[str]:
    """Relative path stored on the ImageField, or None if no image."""
    if getattr(equipment, "image", None) and equipment.image and getattr(equipment.image, "name", None):
        name = (equipment.image.name or "").strip()
        return name or None
    return None


def normalize_equipment_image_db_path(equipment, *, save: bool = True) -> Optional[str]:
    """
    If Equipment.image.name has a redundant media/ prefix, strip it and optionally save.
    Returns the normalized path, or None if there was no image.
    """
    path = get_equipment_image_storage_path(equipment)
    if not path:
        return None
    normalized = normalize_storage_path(path)
    if normalized != path:
        equipment.image.name = normalized
        if save:
            equipment.save(update_fields=["image"])
            logger.info(
                "Normalized equipment %s image path %s -> %s",
                getattr(equipment, "equipment_id", None),
                path,
                normalized,
            )
    return normalized


def persist_equipment_image_upload(equipment, uploaded_file) -> str:
    """
    Save an uploaded image to configured storage (S3 in production) and local backup.

    Raises if the file cannot be read back from remote storage. Does NOT clear the
    DB path on a false-negative verify — that historically wiped valid S3 objects
    whose keys used a media/ prefix mismatch.
    """
    uploaded_file.seek(0)
    content = uploaded_file.read()
    if not content:
        raise ValueError("Empty image file")

    upload_name = os.path.basename(getattr(uploaded_file, "name", "") or "upload.jpg")
    equipment.image.save(upload_name, ContentFile(content), save=True)
    path = normalize_equipment_image_db_path(equipment, save=True) or (
        equipment.image.name or ""
    ).strip()
    if not path:
        raise ValueError("Failed to persist equipment image path")

    save_local_equipment_image_backup(path, content)

    if not verify_file_field_in_storage(equipment.image):
        # Leave DB path intact; clearing it made images permanently disappear in prod.
        raise ValueError(
            f"Equipment image was not found in remote storage after save. "
            f"Check AWS credentials/bucket. Path: {path}"
        )

    return path


def open_equipment_image_bytes(equipment) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """
    Open equipment image using the field's own storage first.
    Falls back to legacy default_storage path resolution for old records.
    """
    stored_path = get_equipment_image_storage_path(equipment)
    if not stored_path:
        return None, None, None

    if getattr(equipment, "image", None):
        content, resolved_path, content_type = open_via_field_storage(equipment.image)
        if content is not None and resolved_path:
            return content, resolved_path, content_type

    content, resolved_path, content_type = open_local_equipment_image_backup(
        normalize_storage_path(stored_path)
    )
    if content is not None and resolved_path:
        return content, resolved_path, content_type

    content, resolved_path, content_type = open_local_equipment_image_backup(stored_path)
    if content is not None and resolved_path:
        return content, resolved_path, content_type

    return open_storage_bytes_first_match(stored_path)

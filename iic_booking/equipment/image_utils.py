"""Helpers for resolving equipment image paths in storage (S3 or local)."""

from __future__ import annotations

import mimetypes
import os
from typing import Iterable, Optional, Tuple

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage


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


def equipment_image_path_available(stored_path: str, storage=None) -> bool:
    """True when stored_path exists in S3 (via storage) or local backup."""
    if not (stored_path and stored_path.strip()):
        return False
    if local_equipment_image_exists(stored_path):
        return True
    if storage is not None:
        try:
            if hasattr(storage, "exists") and storage.exists(stored_path):
                return True
            with storage.open(stored_path, "rb") as fh:
                fh.read(1)
            return True
        except Exception:
            pass
    return False


def equipment_image_available(file_field) -> bool:
    """True when the image exists in S3 (field storage) or the local backup."""
    if not file_field or not getattr(file_field, "name", None):
        return False
    name = (file_field.name or "").strip()
    if not name:
        return False
    if local_equipment_image_exists(name):
        return True
    try:
        storage = file_field.storage
        with storage.open(name, "rb") as fh:
            fh.read(1)
        return True
    except Exception:
        return False


def persist_equipment_image_upload(equipment, uploaded_file) -> str:
    """
    Save an uploaded image to S3 and local backup.
    Clears the DB path and raises if the file cannot be read back from storage.
    """
    uploaded_file.seek(0)
    content = uploaded_file.read()
    if not content:
        raise ValueError("Empty image file")

    upload_name = os.path.basename(getattr(uploaded_file, "name", "") or "upload.jpg")
    equipment.image.save(upload_name, ContentFile(content), save=True)
    path = (equipment.image.name or "").strip()
    if not path:
        raise ValueError("Failed to persist equipment image path")

    save_local_equipment_image_backup(path, content)

    if not equipment_image_available(equipment.image):
        equipment.image = ""
        equipment.save(update_fields=["image"])
        raise ValueError(f"Equipment image was not found in storage after save. Path: {path}")

    return path


def normalize_storage_path(path: str) -> str:
    """Return path suitable for default_storage (location=media). Avoids double 'media/' prefix."""
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
    Yield plausible default_storage keys for a stored DB path.

    Historically we stored S3 keys sometimes with and sometimes without a leading `media/`.
    This yields both variants.
    """
    if not (stored_path and stored_path.strip()):
        return []

    raw = stored_path.strip()
    normalized = raw

    if normalized.startswith("media/"):
        normalized = normalized[6:]
    elif normalized.startswith("media") and (
        len(normalized) == 5 or normalized[5:6] in ("/", "")
    ):
        normalized = normalized[5:].lstrip("/") or normalized

    candidates = [normalized, raw]
    if normalized and not normalized.startswith("media/"):
        candidates.append("media/" + normalized)
    if raw and not raw.startswith("media/"):
        candidates.append("media/" + raw)

    # De-dupe while keeping order
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


def verify_file_field_in_storage(file_field) -> bool:
    """Return True if the FileField/ImageField object exists in its configured storage backend."""
    if not file_field or not getattr(file_field, "name", None):
        return False
    name = (file_field.name or "").strip()
    if not name:
        return False
    try:
        storage = file_field.storage
        with storage.open(name, "rb") as fh:
            fh.read(1)
        return True
    except Exception:
        return False


def get_equipment_image_storage_path(equipment) -> Optional[str]:
    """Relative path stored on the ImageField, or None if no image."""
    if getattr(equipment, "image", None) and equipment.image and getattr(equipment.image, "name", None):
        name = (equipment.image.name or "").strip()
        return name or None
    return None


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
            if hasattr(storage, "exists") and not storage.exists(key):
                continue
            with storage.open(key, "rb") as fh:
                content = fh.read()
            content_type, _ = mimetypes.guess_type(key)
            if not content_type:
                content_type = "image/jpeg"
            return content, key, content_type
        except Exception:
            continue
    return None, None, None


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

    content, resolved_path, content_type = open_local_equipment_image_backup(stored_path)
    if content is not None and resolved_path:
        return content, resolved_path, content_type

    return open_storage_bytes_first_match(stored_path)

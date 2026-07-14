"""Stable media helpers for user profile pictures (no expiring signed URLs)."""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from django.http import HttpResponse
from django.urls import NoReverseMatch, reverse

logger = logging.getLogger(__name__)


def profile_picture_proxy_path(user_id: int) -> str:
    try:
        return reverse("user-profile-picture-proxy", kwargs={"user_id": user_id})
    except NoReverseMatch:
        return reverse("api:user-profile-picture-proxy", kwargs={"user_id": user_id})


def build_profile_picture_proxy_url(user_id: int, request=None) -> str:
    path = profile_picture_proxy_path(user_id)
    if request is not None:
        try:
            return request.build_absolute_uri(path)
        except Exception:
            return path
    return path


def open_profile_picture_bytes(user) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """
    Open profile picture bytes from storage (path-candidate aware).
    Returns (content, resolved_path, content_type).
    """
    if not user or not getattr(user, "profile_picture", None):
        return None, None, None
    name = getattr(user.profile_picture, "name", None)
    if not name:
        return None, None, None

    # Reuse equipment path-candidate openers (same S3 location=media pitfalls).
    from iic_booking.equipment.image_utils import open_storage_bytes_first_match, open_via_field_storage

    content, resolved, content_type = open_via_field_storage(user.profile_picture)
    if content and resolved:
        return content, resolved, content_type
    return open_storage_bytes_first_match(name)


def stream_profile_picture_response(user) -> HttpResponse | None:
    """Return an HttpResponse that streams the picture, or None if unavailable."""
    content, resolved_path, content_type = open_profile_picture_bytes(user)
    if not content or not resolved_path:
        logger.warning(
            "Profile picture not found in storage. user_id=%s path=%s",
            getattr(user, "pk", None),
            getattr(getattr(user, "profile_picture", None), "name", None),
        )
        return None
    response = HttpResponse(content, content_type=content_type or "image/jpeg")
    # Stable proxy URL: safe to cache in the browser; admin replace uses a new upload.
    response["Cache-Control"] = "public, max-age=86400"
    response["Content-Length"] = str(len(content))
    return response

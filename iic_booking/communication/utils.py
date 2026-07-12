"""Utility functions for communication app."""

import threading
from urllib.parse import urlparse

from django.conf import settings

_thread_locals = threading.local()


def booking_display_id_for_email(booking) -> str:
    """
    Public-facing booking reference for emails and notifications.

    Prefer ``virtual_booking_id``; if missing, use ``{equipment.code}-{booking_id}`` when
    equipment is available, else the numeric primary key as a last resort.
    """
    if booking is None:
        return ""
    v = (getattr(booking, "virtual_booking_id", None) or "").strip()
    if v:
        return v
    bid = getattr(booking, "booking_id", None)
    eq = getattr(booking, "equipment", None)
    code = (getattr(eq, "code", None) or "").strip() if eq is not None else ""
    if code and bid is not None:
        return f"{code}-{bid}"
    return str(bid) if bid is not None else ""


def get_frontend_absolute_url(path: str = "") -> str:
    """
    Build an absolute URL for the frontend app (for use in emails and notifications).
    Use this so links in emails work when clicked (relative links do not work in email clients).

    Args:
        path: Path without leading slash, or with leading slash (e.g. "/my-bookings" or "my-bookings?booking=1")

    Returns:
        Absolute URL (e.g. "https://yoursite.com/my-bookings?booking=1") or empty string if FRONTEND_URL not set.
    """
    base = (getattr(settings, "FRONTEND_URL", "") or "").strip().rstrip("/")
    if not base:
        return ""
    path = (path or "").strip()
    if path.startswith(("http://", "https://")):
        return path
    path = path.lstrip("/")
    return f"{base}/{path}" if path else base


def get_backend_absolute_url(path: str = "") -> str:
    """
    Build absolute URL for backend API links (used in one-click email actions).

    Resolution order:
    1) settings.BACKEND_PUBLIC_URL
    2) derive from OMNIPORT_REDIRECT_URI origin
    3) derive from FRONTEND_URL (8080 -> 8000 for local dev)
    """
    base = (getattr(settings, "BACKEND_PUBLIC_URL", "") or "").strip().rstrip("/")
    if not base:
        omni = (getattr(settings, "OMNIPORT_REDIRECT_URI", "") or "").strip()
        if omni.startswith(("http://", "https://")):
            p = urlparse(omni)
            if p.scheme and p.netloc:
                base = f"{p.scheme}://{p.netloc}"
    if not base:
        frontend = (getattr(settings, "FRONTEND_URL", "") or "").strip()
        if frontend.startswith(("http://", "https://")):
            p = urlparse(frontend)
            if p.scheme and p.netloc:
                host = p.netloc.replace(":8080", ":8000")
                base = f"{p.scheme}://{host}"
    if not base:
        base = "http://127.0.0.1:8000"

    path = (path or "").strip()
    if path.startswith(("http://", "https://")):
        return path
    path = path.lstrip("/")
    return f"{base}/{path}" if path else base


def get_current_user():
    """Get the current user from thread-local storage."""
    return getattr(_thread_locals, 'user', None)


def set_current_user(user):
    """Set the current user in thread-local storage."""
    _thread_locals.user = user


def clear_current_user():
    """Clear the current user from thread-local storage."""
    if hasattr(_thread_locals, 'user'):
        delattr(_thread_locals, 'user')


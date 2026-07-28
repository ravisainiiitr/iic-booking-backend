"""Display helpers for the Department Sync Operations Console."""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.hashers import make_password
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

from .constants import heartbeat_timeout_seconds

_ADMIN_ACTION_BTN = (
    "display:inline-block;float:none;white-space:nowrap;"
    "padding:4px 10px;text-decoration:none;border-radius:3px;margin:0;line-height:1.3;"
)
_ADMIN_ACTION_WRAP = (
    "display:inline-flex;flex-wrap:wrap;gap:6px;align-items:center;"
    "max-width:28rem;white-space:normal;"
)

LIFECYCLE_COLORS = {
    "REGISTERED": "#6c757d",
    "ENROLLED": "#28a745",
    "DISABLED": "#ffc107",
    "REVOKED": "#dc3545",
}

SEVERITY_COLORS = {
    "DEBUG": "#6c757d",
    "INFO": "#17a2b8",
    "WARNING": "#ffc107",
    "ERROR": "#dc3545",
    "CRITICAL": "#7b1e1e",
}


def admin_action_buttons(*html_buttons):
    parts = [b for b in html_buttons if b]
    if not parts:
        return format_html('<span style="color:#666;">-</span>')
    return format_html(
        '<span class="iic-admin-actions" style="{}">{}</span>',
        _ADMIN_ACTION_WRAP,
        mark_safe("".join(str(p) for p in parts)),
    )


def action_button(url: str, label: str, *, bg: str = "#417690", color: str = "white"):
    return format_html(
        '<a class="button" href="{}" style="{}background:{};color:{};">{}</a>',
        url,
        _ADMIN_ACTION_BTN,
        bg,
        color,
        label,
    )


def color_badge(label: str, bg: str, *, color: str = "white"):
    return format_html(
        '<span style="background-color:{};color:{};padding:3px 8px;border-radius:3px;'
        'font-size:11px;font-weight:bold;white-space:nowrap;">{}</span>',
        bg,
        color,
        label,
    )


def lifecycle_badge(status: str, display: str | None = None):
    bg = LIFECYCLE_COLORS.get(status, "#6c757d")
    text_color = "#212529" if status == "DISABLED" else "white"
    return color_badge(display or status, bg, color=text_color)


def severity_badge(severity: str, display: str | None = None):
    bg = SEVERITY_COLORS.get(severity, "#6c757d")
    text_color = "#212529" if severity == "WARNING" else "white"
    return color_badge(display or severity, bg, color=text_color)


def is_agent_online(last_heartbeat_at, *, now=None, timeout_seconds: int | None = None) -> bool:
    if last_heartbeat_at is None:
        return False
    now = now or timezone.now()
    timeout = timeout_seconds if timeout_seconds is not None else heartbeat_timeout_seconds()
    return last_heartbeat_at >= now - timedelta(seconds=timeout)


def online_badge(last_heartbeat_at, *, now=None, timeout_seconds: int | None = None):
    online = is_agent_online(last_heartbeat_at, now=now, timeout_seconds=timeout_seconds)
    if online:
        return color_badge(_("Online"), "#28a745")
    return color_badge(_("Offline"), "#6c757d")


def warning_html(message: str):
    return format_html(
        '<div style="background:#fff3cd;border:1px solid #ffc107;color:#856404;'
        'padding:8px 12px;border-radius:4px;margin:4px 0;">⚠ {}</div>',
        message,
    )


def hash_secret(plaintext: str) -> str:
    return make_password(plaintext)


def heartbeat_age_display(last_heartbeat_at, *, now=None):
    if last_heartbeat_at is None:
        return _("Never")
    now = now or timezone.now()
    delta = now - last_heartbeat_at
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return _("%(n)s seconds ago") % {"n": seconds}
    if seconds < 3600:
        return _("%(n)s minutes ago") % {"n": seconds // 60}
    if seconds < 86400:
        return _("%(n)s hours ago") % {"n": seconds // 3600}
    return _("%(n)s days ago") % {"n": seconds // 86400}

"""Equipment flash messages: sanitising, scheduling, audience and the cached per-equipment read used by the
equipment detail payload.

Who can manage them: the OIC and an active temporary OIC (their equipment), the Department Administrator
(their department's equipment) and the Main Administrator (everything).
"""

from __future__ import annotations

import re
from datetime import timedelta

import nh3
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from iic_booking.users.models.user_type import UserType

from .rich_text import rich_text_to_plain

MESSAGE_MAX_CHARS = 300
MESSAGE_HTML_MAX_CHARS = 3000
MAX_DURATION_DAYS = 30
MAX_START_AHEAD_DAYS = 90
LINK_LABEL_MAX_CHARS = 40
CACHE_SECONDS = 300
_CACHE_PREFIX = "equipment_flash_messages:v1:"

_TAG_RENAMES = (("b", "strong"), ("i", "em"))
_BLOCK_BREAK = re.compile(r"</?(p|div|br|li|ul|ol|h[1-6])(\s[^>]*)?/?>", re.I)
_EMPTY_WRAPPER = re.compile(r"<(strong|em|a)(\s[^>]*)?>\s*</\1>")
_SPACES = re.compile(r"\s+")


def _href_filter(element: str, attribute: str, value: str) -> str | None:
    if element == "a" and attribute == "href":
        v = value.strip()
        return v if len(v) <= 500 and re.match(r"^(https?://|mailto:)", v, re.I) else None
    return value


def sanitize_flash_html(raw) -> str:
    """One line of text with bold, italic and links only; plain text is escaped."""
    text = "" if raw is None else str(raw).replace("\r\n", "\n").strip()
    if not text:
        return ""
    text = _BLOCK_BREAK.sub(" ", text)
    for old, new in _TAG_RENAMES:
        text = re.sub(rf"<(/?){old}(\s[^>]*)?>", rf"<\1{new}>", text, flags=re.I)
    html = nh3.clean(
        text,
        tags={"strong", "em", "a"},
        clean_content_tags={"script", "style", "iframe", "object", "embed", "noscript", "template", "title"},
        attributes={"a": {"href"}},
        attribute_filter=_href_filter,
        url_schemes={"http", "https", "mailto"},
        url_relative="deny",
        link_rel="noopener noreferrer",
        set_tag_attribute_values={"a": {"target": "_blank"}},
        strip_comments=True,
    )
    html = re.sub(r"<a(?![^>]*\shref=)[^>]*>(.*?)</a>", r"\1", html, flags=re.S)
    previous = None
    while previous != html:
        previous, html = html, _EMPTY_WRAPPER.sub("", html)
    return _SPACES.sub(" ", html).strip()


def clean_flash_message(raw) -> tuple[str | None, str | None]:
    html = sanitize_flash_html(raw)
    plain = rich_text_to_plain(html).strip()
    if not plain:
        return None, "Enter the message."
    if len(plain) > MESSAGE_MAX_CHARS:
        return None, f"Keep the message under {MESSAGE_MAX_CHARS} characters."
    if len(html) > MESSAGE_HTML_MAX_CHARS:
        return None, "The message has too much formatting. Remove some links or styles."
    return html, None


def clean_link(url, label) -> tuple[tuple[str, str] | None, str | None]:
    url = str(url or "").strip()
    label = _SPACES.sub(" ", str(label or "")).strip()
    if not url:
        return ("", ""), None
    if len(url) > 500 or not re.match(r"^https?://[^\s<>\"']+$", url, re.I):
        return None, "Enter a link starting with https:// (or leave it empty)."
    if len(label) > LINK_LABEL_MAX_CHARS:
        return None, f"Keep the link text under {LINK_LABEL_MAX_CHARS} characters."
    return (url, label or "Learn more"), None


def parse_when(raw):
    """Aware datetime from an ISO string; ``None`` when missing, ``False`` when invalid."""
    if raw in (None, ""):
        return None
    value = parse_datetime(str(raw).strip())
    if value is None:
        return False
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_current_timezone())
    return value


def schedule_error(start_at, end_at, now, *, creating: bool) -> str | None:
    if end_at <= start_at:
        return "The end must be after the start."
    if creating and end_at <= now:
        return "The end must be in the future."
    if start_at > now + timedelta(days=MAX_START_AHEAD_DAYS):
        return f"The start can be at most {MAX_START_AHEAD_DAYS} days ahead."
    if end_at > max(start_at, now) + timedelta(days=MAX_DURATION_DAYS, minutes=1):
        return f"A flash message can run for at most {MAX_DURATION_DAYS} days."
    return None


def audience_user_type_choices() -> list[tuple[str, str]]:
    return [
        (code, str(label))
        for code, label in UserType.get_choices()
        if UserType.is_end_user_booking_type(code) or code == UserType.OTHER
    ]


def clean_user_types(raw) -> list[str]:
    allowed = {code.lower(): code for code, _ in audience_user_type_choices()}
    items = raw if isinstance(raw, (list, tuple)) else str(raw or "").split(",")
    out: list[str] = []
    for item in items:
        code = allowed.get(str(item).strip().lower())
        if code and code not in out:
            out.append(code)
    return out


def message_status(msg, now=None) -> str:
    now = now or timezone.now()
    if not msg.is_active:
        return "OFF"
    if msg.end_at <= now:
        return "EXPIRED"
    if msg.start_at > now:
        return "SCHEDULED"
    return "LIVE"


# --- Permissions ---------------------------------------------------------------------------------------


def flash_equipment_ids(user):
    """None = every equipment; list = equipment the user may manage; PermissionError for other roles."""
    from .models import Equipment
    from .reports import get_equipment_ids_managed_by_oic

    ut = getattr(user, "user_type", None)
    if not getattr(user, "is_authenticated", False):
        raise PermissionError
    if ut == UserType.ADMIN:
        return None
    if ut == UserType.MANAGER:
        return list(get_equipment_ids_managed_by_oic(user.id))
    if ut == UserType.DEPT_ADMIN:
        from iic_booking.users.rbac import get_user_department_scope_id

        dept_id = get_user_department_scope_id(user)
        if not dept_id:
            return []
        return list(Equipment.objects.filter(internal_department_id=dept_id).values_list("equipment_id", flat=True))
    raise PermissionError


def can_manage_equipment_flash(user, equipment) -> bool:
    """Cheap check for the equipment page shortcut; only staff roles run a query."""
    ut = getattr(user, "user_type", None)
    if not getattr(user, "is_authenticated", False) or ut not in (UserType.ADMIN, UserType.MANAGER, UserType.DEPT_ADMIN):
        return False
    if ut == UserType.ADMIN:
        return True
    if ut == UserType.DEPT_ADMIN:
        from iic_booking.users.rbac import get_user_department_scope_id

        dept_id = get_user_department_scope_id(user)
        return bool(dept_id) and getattr(equipment, "internal_department_id", None) == dept_id
    from .models import EquipmentManager, EquipmentTemporaryOIC

    return (
        EquipmentManager.objects.filter(equipment_id=equipment.pk, manager_id=user.pk).exists()
        or EquipmentTemporaryOIC.objects.active().filter(equipment_id=equipment.pk, temporary_oic_id=user.pk).exists()
    )


def actor_role(user, equipment_id=None) -> str:
    ut = getattr(user, "user_type", None)
    if ut == UserType.ADMIN:
        return "MAIN_ADMIN"
    if ut == UserType.DEPT_ADMIN:
        return "DEPT_ADMIN"
    if ut == UserType.MANAGER and equipment_id:
        from .models import EquipmentManager

        if not EquipmentManager.objects.filter(equipment_id=equipment_id, manager_id=user.pk).exists():
            return "TEMP_OIC"
        return "OIC"
    return str(ut or "")


# --- Public read (cached per equipment) -----------------------------------------------------------------


def _cache_key(equipment_id) -> str:
    return f"{_CACHE_PREFIX}{equipment_id}"


def invalidate_flash_cache(equipment_id) -> None:
    key = _cache_key(equipment_id)
    cache.delete(key)
    transaction.on_commit(lambda: cache.delete(key))


def _pending_rows(equipment_id, now) -> list[dict]:
    """Messages of one equipment that are on and not yet ended (live or scheduled); time filtering at read."""
    key = _cache_key(equipment_id)
    rows = cache.get(key)
    if rows is None:
        from .models import EquipmentFlashMessage

        rows = list(
            EquipmentFlashMessage.objects.filter(equipment_id=equipment_id, is_active=True, end_at__gt=now)
            .order_by("start_at", "id")
            .values(
                "id", "message", "tone", "start_at", "end_at", "audience", "audience_user_types",
                "show_on_modes", "link_url", "link_label",
            )
        )
        cache.set(key, rows, CACHE_SECONDS)
    return rows


def _viewer_sees(row, user) -> bool:
    audience = row.get("audience") or "ALL"
    if audience == "ALL":
        return True
    if not getattr(user, "is_authenticated", False):
        return False
    ut = str(getattr(user, "user_type", "") or "")
    if UserType.is_management_user(ut) or ut in (UserType.OC_STORES, UserType.HOD):
        return True
    if audience == "INTERNAL":
        return UserType.is_internal_user(ut)
    if audience == "EXTERNAL":
        return UserType.is_external_user(ut)
    codes = {str(c).lower() for c in (row.get("audience_user_types") or [])}
    if ut.lower() == UserType.INDIVIDUAL_STUDENT and UserType.STUDENT in codes:
        return True
    return ut.lower() in codes


def public_flash_messages(equipment, user=None, now=None) -> list[dict]:
    """Messages live now for this viewer: the equipment's own, then its base instrument's 'show on all modes'."""
    from .models import FlashAudience

    if equipment is None or not getattr(equipment, "pk", None):
        return []
    now = now or timezone.now()
    staff = bool(getattr(user, "is_authenticated", False)) and UserType.is_management_user(
        str(getattr(user, "user_type", "") or "")
    )
    audience_labels = dict(FlashAudience.choices)
    sources = [(equipment.pk, False)]
    parent_id = getattr(equipment, "parent_equipment_id", None)
    if parent_id:
        sources.append((parent_id, True))
    out = []
    for eq_id, inherited in sources:
        for row in _pending_rows(eq_id, now):
            if inherited and not row.get("show_on_modes"):
                continue
            if not (row["start_at"] <= now < row["end_at"]) or not _viewer_sees(row, user):
                continue
            item = {
                "id": row["id"],
                "message": row["message"],
                "tone": row["tone"],
                "end_at": row["end_at"],
                "link_url": row["link_url"] or "",
                "link_label": row["link_label"] or "",
                "from_base_instrument": inherited,
            }
            if staff:
                item["audience"] = row["audience"]
                item["audience_display"] = str(audience_labels.get(row["audience"], row["audience"]))
            out.append(item)
    return out

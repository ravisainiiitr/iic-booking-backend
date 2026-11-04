"""Equipment flash messages: sanitising, scheduling, audience and the cached per-equipment read used by the
equipment detail payload.

Who can manage them: the OIC and an active temporary OIC (their equipment), the Department Administrator
(their department's equipment) and the Main Administrator (everything).
"""

from __future__ import annotations

import re
from datetime import timedelta
from html import escape

from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from iic_booking.users.models.user_type import UserType

from .rich_text import looks_like_html
from .rich_text import rich_text_to_plain
from .rich_text import sanitize_rich_text

MESSAGE_MAX_CHARS = 2000
MESSAGE_HTML_MAX_CHARS = 20000
MAX_EXTEND_DAYS = 3650
LINK_LABEL_MAX_CHARS = 40
CACHE_SECONDS = 300
_CACHE_PREFIX = "equipment_flash_messages:v2:"

_SPACES = re.compile(r"\s+")


def sanitize_flash_html(raw) -> str:
    """The Important Instruction's rich text (headings, lists, colours, sizes, fonts, alignment, links); plain
    text is escaped and kept line by line."""
    text = "" if raw is None else str(raw).replace("\r\n", "\n").strip()
    if not text:
        return ""
    if not looks_like_html(text):
        return "".join(f"<p>{escape(line)}</p>" for line in text.split("\n") if line.strip())
    return sanitize_rich_text(text)


def ends_after_q(now) -> Q:
    """Messages that have not ended at ``now`` (no end = until changed)."""
    return Q(end_at__isnull=True) | Q(end_at__gt=now)


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
    """Any future start and end (any year); ``end_at`` None = until changed."""
    if end_at is None:
        return None
    if end_at <= start_at:
        return "The end must be after the start."
    if creating and end_at <= now:
        return "The end must be in the future."
    return None


def extend_by_days(end_at, now, days: int):
    return max(end_at, now) + timedelta(days=days)


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
    if msg.end_at is not None and msg.end_at <= now:
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
            EquipmentFlashMessage.objects.filter(ends_after_q(now), equipment_id=equipment_id, is_active=True)
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


def public_flash_messages(equipment, user=None, now=None, *, include_base: bool = True) -> list[dict]:
    """Messages live now for this viewer: the equipment's own, then (``include_base``) its base instrument's
    messages marked 'also show on each mode'. The page listing all modes of an instrument shows the base
    instrument's own messages (``include_base=False`` on the base instrument)."""
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
    if parent_id and include_base:
        sources.append((parent_id, True))
    out = []
    for eq_id, inherited in sources:
        for row in _pending_rows(eq_id, now):
            if inherited and not row.get("show_on_modes"):
                continue
            ends = row.get("end_at")
            if row["start_at"] > now or (ends is not None and ends <= now) or not _viewer_sees(row, user):
                continue
            item = {
                "id": row["id"],
                "message": row["message"],
                "tone": row["tone"],
                "end_at": row["end_at"],
                "until_changed": row["end_at"] is None,
                "link_url": row["link_url"] or "",
                "link_label": row["link_label"] or "",
                "from_base_instrument": inherited,
            }
            if staff:
                item["audience"] = row["audience"]
                item["audience_display"] = str(audience_labels.get(row["audience"], row["audience"]))
            out.append(item)
    return out

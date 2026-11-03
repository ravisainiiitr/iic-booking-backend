"""Equipment configuration audit trail: one ``EquipmentConfigAuditLog`` row per changed setting.

Changes are caught at the model layer (``pre_save`` / ``post_save`` / ``post_delete``), so every path is covered:
the React dashboard and the API, Django admin, management commands, Celery tasks and other system code. Bulk
``QuerySet.update`` calls skip model signals, so code that bulk-updates equipment uses ``audited_update``.

Who and where come from the booking audit scope (``booking_audit``): the signed-in request user for web requests
(Django admin is told apart by its URL), the Celery task name for jobs, ``manage.py <command>`` for scripts, and
"System process" otherwise. Values of secret-looking fields are never stored.
"""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime, time
from decimal import Decimal

from django.db import models
from django.db.models.signals import post_delete, post_save, pre_save

from .config_audit_models import ConfigChangeAction, ConfigChangeSource, EquipmentConfigAuditLog

logger = logging.getLogger(__name__)

MAX_VALUE_LENGTH = 2000
REDACTED = "[hidden]"

# Bookkeeping fields that change on every save and are not configuration.
IGNORED_FIELDS = frozenset({"created_at", "updated_at", "last_modified", "modified_at", "search_vector"})
_SECRET_WORDS = ("password", "secret", "token", "api_key", "apikey", "private_key", "credential")


def _is_secret(field_name: str) -> bool:
    name = field_name.lower()
    return any(word in name for word in _SECRET_WORDS)


def _equipment_of(instance):
    from .models import Equipment

    if isinstance(instance, Equipment):
        return instance
    return getattr(instance, "equipment", None)


# Models whose rows are equipment configuration: (model path, object type label).
TRACKED = (
    ("equipment.Equipment", "Equipment"),
    ("equipment.SlotMaster", "Slot Master"),
    ("equipment.ChargeProfile", "Charge Profile"),
)


def _fields(model) -> list[models.Field]:
    return [
        f
        for f in model._meta.concrete_fields
        if not f.primary_key and f.name not in IGNORED_FIELDS and not isinstance(f, models.AutoField)
    ]


def _render(field: models.Field, value) -> str:
    if value is None or value == "":
        return ""
    if _is_secret(field.name):
        return REDACTED
    if field.is_relation and field.related_model is not None:
        related = field.related_model._base_manager.filter(pk=value).first()
        return f"{related} (#{value})" if related is not None else f"#{value}"
    if field.choices:
        for key, label in field.flatchoices:
            if key == value:
                return f"{label} ({value})" if str(label) != str(value) else str(value)
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value.normalize(), "f") if value == value.to_integral() else str(value)
    if isinstance(field, (models.FileField,)):
        return str(getattr(value, "name", value) or "")
    text = str(value)
    return text if len(text) <= MAX_VALUE_LENGTH else text[: MAX_VALUE_LENGTH - 1] + "…"


def _raw(instance, field: models.Field):
    value = getattr(instance, field.attname)
    if isinstance(field, models.FileField):
        # The database holds the file name; the instance holds a FieldFile.
        return getattr(value, "name", value) or ""
    return value


def _context() -> dict:
    """Actor, source, process and reference for the change being recorded."""
    from .booking_audit import SOURCE_BACKGROUND_JOB, SOURCE_SCHEDULED_JOB, SOURCE_WEB, _scope, current_actor

    scope = _scope.get() or {}
    actor = current_actor()
    request = scope.get("request")
    src = scope.get("source")
    process = ""
    reference = ""
    if src == SOURCE_WEB and request is not None:
        path = str(getattr(request, "path", "") or "")
        process = f"{getattr(request, 'method', '')} {path}".strip()
        reference = str(
            request.META.get("HTTP_X_REQUEST_ID") or request.META.get("HTTP_X_AMZN_TRACE_ID") or ""
        )[:128]
        if path.startswith("/admin/") or "/django-admin/" in path:
            source = ConfigChangeSource.DJANGO_ADMIN
        elif path.startswith("/api/"):
            source = ConfigChangeSource.DASHBOARD if _from_dashboard(request) else ConfigChangeSource.API
        else:
            source = ConfigChangeSource.DASHBOARD
    elif src in (SOURCE_SCHEDULED_JOB, SOURCE_BACKGROUND_JOB):
        source = ConfigChangeSource.SCHEDULED_TASK
        process = str(scope.get("source_label") or "")
    elif scope.get("source_label"):
        source = ConfigChangeSource.SYSTEM
        process = str(scope.get("source_label") or "")
    else:
        command = _management_command()
        source = ConfigChangeSource.MANAGEMENT_COMMAND if command else ConfigChangeSource.SYSTEM
        process = f"manage.py {command}" if command else "System process"
    return {"actor": actor, "source": source, "process": process[:255], "reference": reference}


def _from_dashboard(request) -> bool:
    """API calls made by the portal's own React pages (they send the portal Origin / Referer)."""
    origin = str(request.META.get("HTTP_ORIGIN") or request.META.get("HTTP_REFERER") or "")
    return bool(origin)


def _management_command() -> str:
    argv = list(getattr(sys, "argv", []) or [])
    if len(argv) >= 2 and str(argv[0]).replace("\\", "/").endswith("manage.py"):
        if argv[1] not in ("runserver", "shell", "test") and not argv[1].startswith("-"):
            return str(argv[1])
    return ""


def _actor_fields(actor) -> dict:
    if actor is None:
        return {"changed_by": None, "changed_by_label": "System", "changed_by_role": "", "changed_by_department": "", "is_system": True}
    from iic_booking.users.display import get_user_display_name

    dept = getattr(actor, "department", None)
    return {
        "changed_by": actor,
        "changed_by_label": (get_user_display_name(actor) or getattr(actor, "email", "") or "")[:255],
        "changed_by_role": str(getattr(actor, "user_type", "") or "")[:64],
        "changed_by_department": str(getattr(dept, "name", "") or "")[:255],
        "is_system": False,
    }


def _label(instance) -> str:
    """``str(instance)``; some ``__str__`` need values the caller has not normalised yet (e.g. times given as text)."""
    try:
        return str(instance)
    except Exception:
        return f"{type(instance).__name__} #{instance.pk}"


def _rows(instance, object_type: str, action: str, changes: list[tuple[models.Field, str, str]]) -> list:
    equipment = _equipment_of(instance)
    if equipment is None:
        return []
    ctx = _context()
    base = {
        "equipment_id": equipment.pk,
        "equipment_code": str(getattr(equipment, "code", "") or "")[:100],
        "equipment_name": str(getattr(equipment, "name", "") or "")[:255],
        "object_type": object_type,
        "object_id": str(instance.pk or ""),
        "object_label": "" if object_type == "Equipment" else _label(instance)[:255],
        "action": action,
        "source": ctx["source"],
        "process": ctx["process"],
        "reference": ctx["reference"],
        **_actor_fields(ctx["actor"]),
    }
    return [
        EquipmentConfigAuditLog(
            **base,
            setting=field.name if field is not None else "*",
            setting_label=str(field.verbose_name if field is not None else object_type)[:255],
            previous_value=old,
            new_value=new,
        )
        for field, old, new in changes
    ]


def _write(rows: list) -> None:
    if not rows:
        return
    try:
        EquipmentConfigAuditLog.objects.bulk_create(rows)
    except Exception:
        # The audit must never break the change itself; a failure is logged for follow-up.
        logger.exception("equipment config audit write failed (%s rows)", len(rows))


def _on_pre_save(sender, instance, raw=False, **kwargs):
    if raw or instance.pk is None:
        instance._config_audit_before = None
        return
    try:
        before = sender._base_manager.filter(pk=instance.pk).values(*[f.attname for f in _fields(sender)]).first()
    except Exception:
        before = None
    instance._config_audit_before = before


def _on_post_save(sender, instance, created=False, raw=False, update_fields=None, **kwargs):
    if raw:
        return
    object_type = _object_type(sender)
    if created:
        _write(_rows(instance, object_type, ConfigChangeAction.CREATED, [(None, "", _label(instance)[:MAX_VALUE_LENGTH])]))
        return
    before = getattr(instance, "_config_audit_before", None)
    instance._config_audit_before = None
    if before is None:
        return
    changes = []
    for field in _fields(sender):
        if update_fields is not None and field.name not in update_fields and field.attname not in update_fields:
            continue
        old, new = before.get(field.attname), _raw(instance, field)
        if _same(field, old, new):
            continue
        changes.append((field, _render(field, old), _render(field, new)))
    _write(_rows(instance, object_type, ConfigChangeAction.UPDATED, changes))


def _on_post_delete(sender, instance, **kwargs):
    from .models import Equipment

    if sender is not Equipment and getattr(instance, "equipment_id", None) is not None:
        # Rows deleted together with their equipment are covered by the equipment's own DELETED row.
        if not Equipment.objects.filter(pk=instance.equipment_id).exists():
            return
    _write(_rows(instance, _object_type(sender), ConfigChangeAction.DELETED, [(None, _label(instance)[:MAX_VALUE_LENGTH], "")]))


def _same(field, old, new) -> bool:
    if old == new:
        return True
    try:
        return field.to_python(old) == field.to_python(new)
    except Exception:
        return False


def _object_type(model) -> str:
    label = f"{model._meta.app_label}.{model.__name__}"
    for path, name in TRACKED:
        if path == label:
            return name
    return model.__name__


def audited_update(queryset, **values) -> int:
    """``queryset.update(**values)`` for tracked models, recording each changed field per row first."""
    model = queryset.model
    fields = {f.name: f for f in _fields(model)}
    fields.update({f.attname: f for f in _fields(model)})
    names = [n for n in values if n in fields]
    rows = []
    if names:
        attnames = [fields[n].attname for n in names]
        for before in queryset.values("pk", *attnames):
            instance = model._base_manager.filter(pk=before["pk"]).first()
            if instance is None:
                continue
            changes = []
            for name in names:
                field = fields[name]
                new = values[name]
                new = getattr(new, "pk", new)
                old = before[field.attname]
                if not _same(field, old, new):
                    changes.append((field, _render(field, old), _render(field, new)))
            rows += _rows(instance, _object_type(model), ConfigChangeAction.UPDATED, changes)
    count = queryset.update(**values)
    _write(rows)
    return count


def connect_signals() -> None:
    from django.apps import apps

    for path, _name in TRACKED:
        model = apps.get_model(path)
        uid = f"equipment_config_audit_{path}"
        pre_save.connect(_on_pre_save, sender=model, weak=False, dispatch_uid=f"{uid}_pre")
        post_save.connect(_on_post_save, sender=model, weak=False, dispatch_uid=f"{uid}_post")
        post_delete.connect(_on_post_delete, sender=model, weak=False, dispatch_uid=f"{uid}_del")

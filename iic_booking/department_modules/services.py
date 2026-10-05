"""Main Administrator control of the per-department module switches.

Shared by the admin API and the ``department_modules`` management command so every path writes the same rows and
the same audit entries. Procurement & Assets is written through its own audited configuration service (its
``ProcurementManagementConfiguration`` stays the single source of truth) and mirrored into this audit trail.
"""

from __future__ import annotations

from datetime import datetime

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from . import audit
from .access import installed_at, is_main_admin, new_department_ids
from .constants import ALL_MODULES, LOCAL_MODULES, OFF_HELP, REASON_MIN_LENGTH, TEST_USERS_ONLY_HELP, ModuleKey
from .errors import DepartmentModuleError
from .models import DepartmentModuleAuditLog, DepartmentModuleSetting, SettingSource


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _user_name(user) -> str | None:
    if not user:
        return None
    return getattr(user, "name", None) or getattr(user, "email", None) or str(user.pk)


def _state(enabled: bool, test_users_only: bool, configured: bool) -> dict:
    return {"enabled": bool(enabled), "test_users_only": bool(test_users_only), "configured": bool(configured)}


NEW_DEPARTMENT_NOTE = "New department: off until the Main Administrator turns it on"


def _local_state(row: DepartmentModuleSetting | None, *, new_department: bool = False) -> dict:
    if row is None:
        # No row: a department created after installation is off; an older one behaves as before the switches.
        return _state(not new_department, False, False)
    return _state(row.enabled, row.test_users_only, True)


def _is_new_department(department) -> bool:
    return bool(department and department.pk in new_department_ids([department.pk]))


def start_new_department_off(department) -> list[DepartmentModuleSetting]:
    """Create the "off" rows of a department created after the switches were installed (idempotent).

    Called when a department is created. If this fails the department is still treated as off (see
    ``access.default_cell``), so creating a department never depends on it.
    """
    if installed_at() is None:
        return []
    created: list[DepartmentModuleSetting] = []
    now = timezone.now()
    with transaction.atomic():
        for key in LOCAL_MODULES:
            row, was_created = DepartmentModuleSetting.objects.get_or_create(
                department=department,
                module_key=key,
                defaults={
                    "enabled": False,
                    "test_users_only": False,
                    "disabled_at": now,
                    "source": SettingSource.NEW_DEPARTMENT,
                    "seed_note": NEW_DEPARTMENT_NOTE,
                },
            )
            if was_created:
                audit.record(
                    None,
                    department,
                    key,
                    "module.new_department_off",
                    old={},
                    new=_state(False, False, True),
                    reason=NEW_DEPARTMENT_NOTE,
                )
                created.append(row)
    return created


def _procurement_state(cfg) -> dict:
    if cfg is None:
        return _state(False, True, False)
    return _state(cfg.module_enabled, cfg.pilot_mode, True)


def _action(before: dict, after: dict) -> str:
    if before["enabled"] != after["enabled"]:
        return "module.enabled" if after["enabled"] else "module.disabled"
    if before["test_users_only"] != after["test_users_only"]:
        return "module.test_users_only_on" if after["test_users_only"] else "module.test_users_only_off"
    return "module.configured"


def _require_admin(actor) -> None:
    if not is_main_admin(actor):
        raise DepartmentModuleError(
            "Only the Main Administrator can change department modules.", status=403, code="forbidden"
        )


def _clean_reason(reason) -> str:
    text = str(reason or "").strip()
    if len(text) < REASON_MIN_LENGTH:
        raise DepartmentModuleError(
            f"Give a reason for this change (at least {REASON_MIN_LENGTH} characters).",
            code="reason_required",
            field="reason",
        )
    return text


def set_module(
    actor,
    department,
    module_key: str,
    *,
    enabled: bool | None = None,
    test_users_only: bool | None = None,
    reason: str = "",
    request=None,
) -> dict:
    """Change one cell of the matrix. Returns the cell as shown in the matrix."""
    _require_admin(actor)
    if module_key not in ALL_MODULES:
        raise DepartmentModuleError("Unknown module.", code="invalid_module", field="module_key")
    if enabled is None and test_users_only is None:
        raise DepartmentModuleError("Nothing to change.", code="nothing_to_change")
    reason = _clean_reason(reason)
    if module_key == ModuleKey.PROCUREMENT:
        return _set_procurement(actor, department, enabled, test_users_only, reason, request)

    with transaction.atomic():
        row = (
            DepartmentModuleSetting.objects.select_for_update()
            .filter(department=department, module_key=module_key)
            .first()
        )
        new_department = row is None and _is_new_department(department)
        before = _local_state(row, new_department=new_department)
        if row is None:
            row = DepartmentModuleSetting(
                department=department,
                module_key=module_key,
                enabled=before["enabled"],
                disabled_at=None if before["enabled"] else timezone.now(),
            )
        now = timezone.now()
        if enabled is not None and bool(enabled) != row.enabled:
            row.enabled = bool(enabled)
            row.disabled_at = None if row.enabled else now
        if test_users_only is not None and bool(test_users_only) != row.test_users_only:
            row.test_users_only = bool(test_users_only)
            row.test_only_since = now if row.test_users_only else None
        after = _state(row.enabled, row.test_users_only, True)
        if after != before:
            row.source = SettingSource.ADMIN
            row.updated_by = actor
            row.save()
            audit.record(actor, department, module_key, _action(before, after), old=before, new=after,
                         reason=reason, request=request)
    return local_cell(row, usage=None)


def _set_procurement(actor, department, enabled, test_users_only, reason, request) -> dict:
    from iic_booking.procurement_management import config_service
    from iic_booking.procurement_management.errors import ProcurementError
    from iic_booking.procurement_management.models import ProcurementManagementConfiguration

    data: dict = {"reason": reason}
    if enabled is not None:
        data["module_enabled"] = bool(enabled)
    if test_users_only is not None:
        data["pilot_mode"] = bool(test_users_only)
    with transaction.atomic():
        before = _procurement_state(ProcurementManagementConfiguration.objects.filter(department=department).first())
        try:
            cfg = config_service.update_config(actor, department, data, request=request)
        except ProcurementError as exc:
            raise DepartmentModuleError(exc.message, status=exc.status, code=exc.code) from exc
        after = _procurement_state(cfg)
        if after != before:
            audit.record(actor, department, ModuleKey.PROCUREMENT, _action(before, after), old=before, new=after,
                         reason=reason, request=request)
    return procurement_cell(cfg, pilot_user_count=cfg.pilot_users.count())


# ---------------------------------------------------------------------------
# Read models for the admin page and the management command
# ---------------------------------------------------------------------------
def local_cell(row: DepartmentModuleSetting | None, *, usage: int | None, new_department: bool = False) -> dict:
    new_default = row is None and new_department
    out = {
        **_local_state(row, new_department=new_department),
        "source": row.source if row else (SettingSource.NEW_DEPARTMENT.value if new_default else None),
        "note": row.seed_note if row else (NEW_DEPARTMENT_NOTE if new_default else ""),
        "disabled_at": _iso(row.disabled_at) if row else None,
        "test_only_since": _iso(row.test_only_since) if row else None,
        "updated_at": _iso(row.updated_at) if row else None,
        "updated_by": _user_name(row.updated_by) if row and row.updated_by_id else None,
    }
    if usage is not None:
        out["usage"] = usage
    return out


def procurement_cell(cfg, *, pilot_user_count: int = 0) -> dict:
    return {
        **_procurement_state(cfg),
        "source": "procurement",
        "note": "",
        "pilot_user_count": pilot_user_count,
        "disabled_at": None,
        "test_only_since": None,
        "updated_at": _iso(cfg.updated_at) if cfg else None,
        "updated_by": _user_name(cfg.updated_by) if cfg and cfg.updated_by_id else None,
    }


def _counts(qs, field: str) -> dict[int, int]:
    return {k: n for k, n in qs.values_list(field).annotate(n=Count("pk")).values_list(field, "n") if k}


def usage_by_department() -> dict[str, dict[int, int]]:
    """What each department uses today, shown next to the switches so the admin sees the impact."""
    from iic_booking.equipment.models import Equipment

    usage: dict[str, dict[int, int]] = {"equipment": _counts(Equipment.objects.all(), "internal_department_id")}
    try:
        from iic_booking.sync.models import EquipmentSyncProfile

        usage[ModuleKey.DSA] = _counts(EquipmentSyncProfile.objects.all(), "equipment__internal_department_id")
    except Exception:  # noqa: BLE001
        usage[ModuleKey.DSA] = {}
    usage[ModuleKey.REMOTE_ANALYSIS] = _counts(
        Equipment.objects.filter(enable_remote_analysis=True), "internal_department_id"
    )
    try:
        from iic_booking.training.models import TrainingEquipmentSetting

        usage[ModuleKey.TRAINING] = _counts(
            TrainingEquipmentSetting.objects.filter(enabled=True), "equipment__internal_department_id"
        )
    except Exception:  # noqa: BLE001
        usage[ModuleKey.TRAINING] = {}
    return usage


def modules_meta() -> list[dict]:
    return [
        {
            "key": key.value,
            "label": key.label,
            "test_users_only_help": TEST_USERS_ONLY_HELP[key],
            "off_help": OFF_HELP[key],
            "usage_label": {
                ModuleKey.DSA: "sync profiles",
                ModuleKey.REMOTE_ANALYSIS: "RA-enabled equipment",
                ModuleKey.TRAINING: "Training equipment",
                ModuleKey.PROCUREMENT: "pilot users",
            }[key],
        }
        for key in ALL_MODULES
    ]


def matrix(*, include_usage: bool = True) -> dict:
    from iic_booking.procurement_management.models import ProcurementManagementConfiguration
    from iic_booking.users.models import Department

    rows = {
        (r.department_id, r.module_key): r
        for r in DepartmentModuleSetting.objects.select_related("updated_by")
    }
    procurement = {
        c.department_id: c
        for c in ProcurementManagementConfiguration.objects.select_related("updated_by").annotate(
            pilot_count=Count("pilot_users", distinct=True)
        )
    }
    usage = usage_by_department() if include_usage else {}
    new_ids = new_department_ids()
    departments = []
    for dept in Department.objects.order_by("name"):
        cells = {
            key: local_cell(
                rows.get((dept.id, key)),
                usage=usage.get(key, {}).get(dept.id, 0) if include_usage else None,
                new_department=dept.id in new_ids,
            )
            for key in LOCAL_MODULES
        }
        cfg = procurement.get(dept.id)
        cells[ModuleKey.PROCUREMENT] = procurement_cell(cfg, pilot_user_count=getattr(cfg, "pilot_count", 0) or 0)
        departments.append(
            {
                "id": dept.id,
                "name": dept.name,
                "code": dept.code or "",
                "department_type": dept.department_type,
                "equipment_count": usage.get("equipment", {}).get(dept.id, 0),
                "cells": cells,
            }
        )
    return {"modules": modules_meta(), "departments": departments}


def history(*, department_id=None, module_key: str | None = None, limit: int = 200) -> list[dict]:
    qs = DepartmentModuleAuditLog.objects.select_related("actor", "department")
    if department_id:
        qs = qs.filter(department_id=department_id)
    if module_key:
        qs = qs.filter(module_key=module_key)
    return [
        {
            "id": e.id,
            "created_at": _iso(e.created_at),
            "department_id": e.department_id,
            "department": e.department.name if e.department_id else e.department_label,
            "module_key": e.module_key,
            "action": e.action,
            "old": e.old_value,
            "new": e.new_value,
            "reason": e.reason,
            "actor": _user_name(e.actor) if e.actor_id else None,
        }
        for e in qs[: max(1, min(int(limit or 200), 1000))]
    ]


def get_department(raw):
    """Department by id or code (used by the command and the API)."""
    from iic_booking.users.models import Department

    text = str(raw or "").strip()
    if not text:
        raise DepartmentModuleError("Department is required.", code="department_required", status=400)
    q = Q(code__iexact=text)
    if text.isdigit():
        q |= Q(pk=int(text))
    dept = Department.objects.filter(q).first()
    if dept is None:
        raise DepartmentModuleError("Department not found.", status=404, code="not_found")
    return dept

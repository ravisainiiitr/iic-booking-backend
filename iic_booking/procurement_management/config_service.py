"""Main Administrator control of per-department configuration and role assignments (all audited)."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.db import transaction

from . import audit
from . import constants as c
from .access import is_main_admin, user_allowed_in
from .defaults import ensure_department_defaults
from .errors import ProcurementError, forbidden
from .fy import is_valid_fy_label
from .models import ProcurementManagementConfiguration, ProcurementRoleAssignment

BOOL_FIELDS = (
    "module_enabled",
    "consumables_enabled",
    "non_consumables_enabled",
    "asset_register_enabled",
    "plan_enabled",
    "non_plan_enabled",
    "amc_enabled",
    "general_purchase_enabled",
    "minor_purchase_enabled",
    "major_purchase_enabled",
    "limited_life_enabled",
    "require_invoice",
    "require_specification",
    "require_comparative_statement",
    "require_asset_allocation",
    "allow_office_direct_purchase_entry",
    "allow_resubmission",
    "plan_submission_open",
    "pilot_mode",
)
MONEY_FIELDS = (
    "small_purchase_threshold",
    "hod_approval_threshold",
    "comparative_quotation_threshold",
    "asset_capitalization_threshold",
)
CHOICE_FIELDS = {
    "variance_action": c.VarianceAction.values,
    "hod_approval_mode": c.HodApprovalMode.values,
}
CONFIG_FIELDS = BOOL_FIELDS + MONEY_FIELDS + tuple(CHOICE_FIELDS) + (
    "variance_tolerance_percent",
    "current_financial_year",
    "amc_reminder_days",
)


def _decimal(name, value) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ProcurementError(f"{name} must be a number.", code="invalid_number")
    if d < 0 or not d.is_finite():
        raise ProcurementError(f"{name} cannot be negative.", code="invalid_number")
    return d.quantize(Decimal("0.01"))


def get_or_create_config(department) -> ProcurementManagementConfiguration:
    cfg, _ = ProcurementManagementConfiguration.objects.get_or_create(department=department)
    return cfg


@transaction.atomic
def update_config(actor, department, data: dict, *, request=None) -> ProcurementManagementConfiguration:
    if not is_main_admin(actor):
        raise forbidden("Only the Main Administrator can change Procurement & Assets configuration.")
    cfg, _ = ProcurementManagementConfiguration.objects.select_for_update().get_or_create(department=department)
    before = audit.snapshot(cfg, CONFIG_FIELDS)
    for name in BOOL_FIELDS:
        if name in data:
            setattr(cfg, name, bool(data[name]))
    for name in MONEY_FIELDS:
        if name in data:
            setattr(cfg, name, _decimal(name, data[name]))
    if "variance_tolerance_percent" in data:
        pct = _decimal("variance_tolerance_percent", data["variance_tolerance_percent"])
        if pct > Decimal("100"):
            raise ProcurementError("Variance tolerance cannot exceed 100%.", code="invalid_number")
        cfg.variance_tolerance_percent = pct
    for name, allowed in CHOICE_FIELDS.items():
        if name in data:
            if data[name] not in allowed:
                raise ProcurementError(f"{name} must be one of {', '.join(allowed)}.", code="invalid_choice")
            setattr(cfg, name, data[name])
    if "current_financial_year" in data:
        fy = (data["current_financial_year"] or "").strip()
        if fy and not is_valid_fy_label(fy):
            raise ProcurementError("current_financial_year must look like 2026-27.", code="invalid_fy")
        cfg.current_financial_year = fy
    if "amc_reminder_days" in data:
        try:
            days = int(data["amc_reminder_days"])
        except (TypeError, ValueError):
            raise ProcurementError("amc_reminder_days must be a whole number.", code="invalid_number")
        if days < 0 or days > 365:
            raise ProcurementError("amc_reminder_days must be between 0 and 365.", code="invalid_number")
        cfg.amc_reminder_days = days
    after = audit.snapshot(cfg, CONFIG_FIELDS)
    old, new = audit.diff(before, after)
    if old or new or cfg._state.adding:
        cfg.updated_by = actor
        cfg.save()
        audit.record(
            actor,
            "config.module_enabled" if "module_enabled" in new and new["module_enabled"] else
            "config.module_disabled" if "module_enabled" in new else "config.updated",
            cfg,
            department=department,
            old=old,
            new=new,
            reason=str(data.get("reason") or ""),
            request=request,
        )
    if "pilot_user_ids" in data:
        _set_pilot_users(actor, cfg, data["pilot_user_ids"], reason=str(data.get("reason") or ""), request=request)
    if cfg.module_enabled:
        ensure_department_defaults(department)
    return cfg


def _set_pilot_users(actor, cfg, raw_ids, *, reason: str = "", request=None) -> None:
    from iic_booking.users.models import User

    if not isinstance(raw_ids, (list, tuple)):
        raise ProcurementError("pilot_user_ids must be a list.", code="invalid_pilot_users", field="pilot_user_ids")
    try:
        ids = sorted({int(str(x)) for x in raw_ids})
    except (TypeError, ValueError):
        raise ProcurementError("pilot_user_ids must be user ids.", code="invalid_pilot_users", field="pilot_user_ids")
    found = {u.pk: u for u in User.objects.filter(pk__in=ids, is_active=True)}
    missing = [i for i in ids if i not in found]
    if missing:
        raise ProcurementError(
            f"Unknown or inactive users: {', '.join(map(str, missing))}.", code="invalid_pilot_users", field="pilot_user_ids"
        )
    not_allowed = [i for i in ids if not user_allowed_in(found[i], cfg.department_id)]
    if not_allowed:
        raise ProcurementError(
            "These users' account types cannot use Procurement & Assets and they do not head this department: "
            f"{', '.join(map(str, not_allowed))}.",
            code="user_type_not_allowed",
            field="pilot_user_ids",
        )
    before = sorted(cfg.pilot_users.values_list("pk", flat=True))
    if before == ids:
        return
    cfg.pilot_users.set(ids)
    audit.record(
        actor, "config.pilot_users", cfg, department=cfg.department,
        old={"pilot_user_ids": before}, new={"pilot_user_ids": ids}, reason=reason, request=request,
    )


def _clean_permissions(role: str, permissions) -> list[str]:
    if role != c.ModuleRole.OFFICE and role != c.ModuleRole.OC_STORES:
        return []
    if permissions is None:
        return list(c.ALL_OFFICE_PERMISSIONS) if role == c.ModuleRole.OFFICE else []
    if not isinstance(permissions, (list, tuple)):
        raise ProcurementError("permissions must be a list.", code="invalid_permissions")
    bad = [p for p in permissions if p not in c.ALL_OFFICE_PERMISSIONS]
    if bad:
        raise ProcurementError(f"Unknown permissions: {', '.join(map(str, bad))}.", code="invalid_permissions")
    return sorted(set(permissions))


@transaction.atomic
def assign_role(actor, department, user, role: str, permissions=None, *, request=None) -> ProcurementRoleAssignment:
    if not is_main_admin(actor):
        raise forbidden("Only the Main Administrator can assign Procurement & Assets roles.")
    if role not in [r.value for r in c.ASSIGNABLE_ROLES]:
        raise ProcurementError("Unknown role.", code="invalid_role")
    if not user_allowed_in(user, department.pk):
        raise ProcurementError(
            "This user's account type cannot use Procurement & Assets and they do not head this department.",
            code="user_type_not_allowed",
            field="user_id",
        )
    perms = _clean_permissions(role, permissions)
    row, created = ProcurementRoleAssignment.objects.select_for_update().get_or_create(
        department=department, user=user, role=role, defaults={"permissions": perms, "assigned_by": actor}
    )
    before = {} if created else {"active": row.active, "permissions": row.permissions}
    row.permissions = perms
    row.active = True
    row.assigned_by = actor
    row.save()
    audit.record(
        actor,
        "role.assigned",
        row,
        department=department,
        old=before,
        new={"user_id": user.pk, "role": role, "permissions": perms, "active": True},
        request=request,
    )
    return row


@transaction.atomic
def revoke_role(actor, assignment: ProcurementRoleAssignment, *, reason: str = "", request=None) -> None:
    if not is_main_admin(actor):
        raise forbidden("Only the Main Administrator can revoke Procurement & Assets roles.")
    if not assignment.active:
        return
    assignment.active = False
    assignment.save(update_fields=["active", "updated_at"])
    audit.record(
        actor,
        "role.revoked",
        assignment,
        department=assignment.department,
        old={"active": True},
        new={"active": False},
        reason=reason,
        request=request,
    )

"""Main Admin control of the Training module switch, audience and per-equipment enablement.

Shared by the admin API, the ``training_config`` management command and Django admin so every path
writes the same rows and the same audit entries.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import Q

from . import access
from . import serializers as s
from .audit import audit
from .errors import TrainingError
from .models import TrainingAudience, TrainingEquipmentSetting, TrainingModuleSettings


def _actor(actor):
    return actor if getattr(actor, "pk", None) else None


def equipment_row(eq, db_enabled: set[int], env_codes: set[str]) -> dict:
    in_env = (eq.code or "").upper() in env_codes
    on = eq.equipment_id in db_enabled
    return {
        **s.equipment_brief(eq),
        "status": eq.status,
        "enabled": on,
        "env_pilot": in_env,
        "training_active": on or in_env,
    }


def module_state() -> dict:
    row = access.module_settings()
    scope = access.pilot_equipment_ids()
    db_enabled = access.db_enabled_equipment_ids()
    env_codes = access.pilot_equipment_codes()
    from iic_booking.equipment.models import Equipment

    listed = Equipment.objects.filter(Q(equipment_id__in=db_enabled) | Q(code__in=env_codes)).select_related(
        "internal_department"
    )
    return {
        "module_enabled": access.module_enabled(),
        "db_module_enabled": bool(row.module_enabled),
        "env_module_enabled": access.env_module_enabled(),
        "audience": row.audience,
        "audience_label": row.get_audience_display(),
        "audience_choices": [{"value": v, "label": str(label)} for v, label in TrainingAudience.choices],
        "env_pilot_equipment_codes": sorted(env_codes),
        "pilot_oic_count": len(access.pilot_oic_emails()),
        "all_equipment_in_scope": scope is None,
        "enabled_equipment": [equipment_row(e, db_enabled, env_codes) for e in listed.order_by("name")],
        "updated_at": s.iso(row.updated_at) if row.pk and row.updated_at else None,
        "updated_by": s.user_brief(row.updated_by)["name"] if row.updated_by_id else None,
    }


def update_module(actor, *, module_enabled: bool | None = None, audience: str | None = None) -> TrainingModuleSettings:
    if audience is not None and audience not in TrainingAudience.values:
        raise TrainingError(f"Audience must be one of {', '.join(TrainingAudience.values)}.")
    with transaction.atomic():
        row, _ = TrainingModuleSettings.objects.select_for_update().get_or_create(pk=TrainingModuleSettings.SINGLETON_PK)
        before = {"module_enabled": row.module_enabled, "audience": row.audience}
        if module_enabled is not None:
            row.module_enabled = bool(module_enabled)
        if audience is not None:
            row.audience = audience
        after = {"module_enabled": row.module_enabled, "audience": row.audience}
        if after != before or not row.updated_at:
            row.updated_by = _actor(actor)
            row.save()
            audit(_actor(actor), "module.settings_updated", row, before=before, after=after)
    return row


def set_equipment_enabled(actor, equipment, enabled: bool) -> TrainingEquipmentSetting:
    with transaction.atomic():
        row, created = TrainingEquipmentSetting.objects.select_for_update().get_or_create(equipment=equipment)
        before = None if created else row.enabled
        if created or row.enabled != bool(enabled):
            row.enabled = bool(enabled)
            row.updated_by = _actor(actor)
            row.save()
            audit(
                _actor(actor),
                "equipment.training_enabled" if row.enabled else "equipment.training_disabled",
                row,
                before={"enabled": before},
                after={"enabled": row.enabled, "equipment_code": equipment.code},
            )
    return row

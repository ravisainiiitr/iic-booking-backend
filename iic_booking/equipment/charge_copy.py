"""Copy one user type's charges to another (IITR Startup gets the IITR Student rates by default).

Copies, per equipment, everything that is priced per user type:
  * ChargeProfile rows (standard and discounted variants) with rates, formulas, breakpoint and flags;
  * charge-profile scoped input fields (DynamicInputField with the source user type);
  * slot options with their charges (MultiParamDefinition with the source user type).

Never overwrites a target row, never edits source or other rows. Each applied run is recorded in
ChargeCopyBatch so it can be rolled back. Reports carry codes and counts only, never amounts.
"""

from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from iic_booking.users.models.user_type import UserType

from .models import (
    ChargeCopyBatch,
    ChargeProfile,
    ChargeProfilePricingProfile,
    DynamicInputField,
    Equipment,
    MultiParamDefinition,
    PrintMaterial,
    UserTypeQuota,
)

DEFAULT_SOURCE = UserType.STUDENT
DEFAULT_TARGET = UserType.STARTUP_INCUBATED_IITR
COPIED_PRICING_PROFILES = (ChargeProfilePricingProfile.STANDARD, ChargeProfilePricingProfile.DISCOUNTED)
PI_SKIP_REASON = "PI rate not copied: it applies only when the booking is paid from an equipment PI's wallet"

_NOT_COPIED = {"equipment", "user_type", "created_at", "updated_at"}


def _copy_values(row) -> dict:
    """Every stored field of ``row`` except its key, equipment, user type and timestamps."""
    return {
        f.attname: getattr(row, f.attname)
        for f in row._meta.concrete_fields
        if not f.primary_key and f.name not in _NOT_COPIED
    }


def _equipment_plan(equipment, source: str, target: str) -> dict:
    entry = {
        "equipment_id": equipment.equipment_id,
        "code": equipment.code,
        "status": equipment.status,
        "charge_profiles": [],
        "input_fields": [],
        "param_definitions": [],
        "skipped": [],
    }
    profiles = ChargeProfile.objects.filter(equipment=equipment)
    existing_target = {cp.pricing_profile: cp.pk for cp in profiles.filter(user_type=target)}
    for cp in profiles.filter(user_type=source).order_by("pricing_profile", "pk"):
        if cp.pricing_profile not in COPIED_PRICING_PROFILES:
            entry["skipped"].append(f"{cp.pricing_profile}: {PI_SKIP_REASON}")
        elif cp.pricing_profile in existing_target:
            entry["skipped"].append(
                f"{cp.pricing_profile}: target row already exists (id {existing_target[cp.pricing_profile]}), left unchanged"
            )
        else:
            entry["charge_profiles"].append(cp)

    fields = DynamicInputField.objects.filter(equipment=equipment)
    source_fields = list(fields.filter(user_type=source).order_by("field_key"))
    if source_fields:
        if fields.filter(user_type=target).exists():
            entry["skipped"].append("input fields: target already has its own input fields, left unchanged")
        else:
            entry["input_fields"] = source_fields

    params = MultiParamDefinition.objects.filter(equipment=equipment)
    target_codes = set(params.filter(user_type=target).values_list("param_code", flat=True))
    for p in params.filter(user_type=source).order_by("param_code"):
        if p.param_code in target_codes:
            entry["skipped"].append(f"slot option {p.param_code}: target already has it, left unchanged")
        else:
            entry["param_definitions"].append(p)

    materials = PrintMaterial.objects.filter(equipment=equipment, user_type=source).count()
    if materials:
        entry["skipped"].append(
            f"{materials} 3D print material(s) limited to {source}: not copied (codes are unique per printer); "
            "add them for the target in the equipment form if needed"
        )
    return entry


def plan_copy(source: str = DEFAULT_SOURCE, target: str = DEFAULT_TARGET) -> list[dict]:
    """Per equipment that has any charge row for ``source``: what would be created or skipped."""
    if source == target:
        raise ValueError("Source and target user types must differ.")
    equipment_ids = (
        ChargeProfile.objects.filter(user_type=source).values_list("equipment_id", flat=True).distinct()
    )
    equipment = Equipment.objects.filter(equipment_id__in=list(equipment_ids)).order_by("code", "equipment_id")
    return [_equipment_plan(eq, source, target) for eq in equipment]


def summarize(plan: list[dict]) -> dict:
    return {
        "equipment_with_source_rates": len(plan),
        "equipment_to_change": sum(
            1 for e in plan if e["charge_profiles"] or e["input_fields"] or e["param_definitions"]
        ),
        "charge_profiles": sum(len(e["charge_profiles"]) for e in plan),
        "charge_profiles_by_variant": {
            variant: sum(1 for e in plan for cp in e["charge_profiles"] if cp.pricing_profile == variant)
            for variant in COPIED_PRICING_PROFILES
        },
        "input_fields": sum(len(e["input_fields"]) for e in plan),
        "param_definitions": sum(len(e["param_definitions"]) for e in plan),
        "skipped": sum(len(e["skipped"]) for e in plan),
    }


def context_counts(source: str = DEFAULT_SOURCE, target: str = DEFAULT_TARGET) -> dict:
    """Related per-user-type settings that are not charges and are reported, not copied."""
    from iic_booking.users.models import User

    return {
        "source_user_type_quotas_not_copied": UserTypeQuota.objects.filter(user_type=source).count(),
        "target_user_type_quotas": UserTypeQuota.objects.filter(user_type=target).count(),
        "equipment_with_source_specific_instruction": sum(
            1
            for value in Equipment.objects.exclude(important_instruction_by_user_type={}).values_list(
                "important_instruction_by_user_type", flat=True
            )
            if isinstance(value, dict) and (value.get(source) or "").strip()
        ),
        "target_users": User.objects.filter(user_type=target).count(),
        "legacy_alias_users": User.objects.filter(
            user_type=UserType.INDIVIDUAL_STUDENT, user_type_alias="IITR Startups"
        ).count(),
    }


def apply_copy(source: str = DEFAULT_SOURCE, target: str = DEFAULT_TARGET) -> tuple[ChargeCopyBatch | None, list[dict]]:
    """Create the planned rows in one transaction. Returns (batch or None when nothing to do, plan)."""
    with transaction.atomic():
        plan = plan_copy(source, target)
        created = {"charge_profiles": [], "input_fields": [], "param_definitions": []}
        for entry in plan:
            equipment_id = entry["equipment_id"]
            for cp in entry["charge_profiles"]:
                row = ChargeProfile.objects.create(equipment_id=equipment_id, user_type=target, **_copy_values(cp))
                created["charge_profiles"].append(row.pk)
            for field in entry["input_fields"]:
                row = DynamicInputField.objects.create(
                    equipment_id=equipment_id, user_type=target, **_copy_values(field)
                )
                created["input_fields"].append(row.pk)
            for param in entry["param_definitions"]:
                row = MultiParamDefinition.objects.create(
                    equipment_id=equipment_id, user_type=target, **_copy_values(param)
                )
                created["param_definitions"].append(row.pk)
        if not any(created.values()):
            return None, plan
        batch = ChargeCopyBatch.objects.create(
            source_user_type=source,
            target_user_type=target,
            created=created,
            summary=summarize(plan),
        )
    return batch, plan


def rollback_copy(batch_id: int) -> dict:
    """Remove the rows a batch created. Charge rows already used by bookings are deactivated instead."""
    with transaction.atomic():
        batch = ChargeCopyBatch.objects.select_for_update().get(pk=batch_id)
        if batch.rolled_back_at:
            raise ValueError(f"Batch {batch_id} was already rolled back at {batch.rolled_back_at:%Y-%m-%d %H:%M}.")
        target = batch.target_user_type
        ids = batch.created or {}
        report = {
            "charge_profiles_deleted": 0,
            "charge_profiles_deactivated_in_use": 0,
            "input_fields_deleted": 0,
            "param_definitions_deleted": 0,
            "already_gone": 0,
            "edited_since_copy": 0,
        }
        profiles = (
            ChargeProfile.objects.filter(pk__in=ids.get("charge_profiles", []), user_type=target)
            .annotate(booking_count=Count("bookings"))
        )
        found = 0
        for cp in profiles:
            found += 1
            if cp.updated_at and cp.updated_at > batch.created_at + timedelta(seconds=5):
                report["edited_since_copy"] += 1
            if cp.booking_count:
                if cp.is_active:
                    cp.is_active = False
                    cp.save(update_fields=["is_active", "updated_at"])
                report["charge_profiles_deactivated_in_use"] += 1
            else:
                cp.delete()
                report["charge_profiles_deleted"] += 1
        report["already_gone"] += len(ids.get("charge_profiles", [])) - found
        for key, model in (("input_fields", DynamicInputField), ("param_definitions", MultiParamDefinition)):
            wanted = ids.get(key, [])
            deleted, _ = model.objects.filter(pk__in=wanted, user_type=target).delete()
            report[f"{key}_deleted"] = deleted
            report["already_gone"] += len(wanted) - deleted
        batch.rolled_back_at = timezone.now()
        batch.rollback_summary = report
        batch.save(update_fields=["rolled_back_at", "rollback_summary"])
    return report

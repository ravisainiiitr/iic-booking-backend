"""Clone an Equipment together with its configuration rows (no bookings, slots or history)."""

from __future__ import annotations

import logging
import os

from django.core.files.base import ContentFile
from django.db import transaction

from .models import (
    ChargeProfile,
    DynamicInputField,
    Equipment,
    EquipmentAccessory,
    EquipmentAdditionalAccessory,
    EquipmentManager,
    EquipmentOperator,
    EquipmentPI,
    EquipmentPublication,
    EquipmentSpecification,
    EquipmentStatus,
    ExternalUserQuota,
    MultiParamDefinition,
    SlotMaster,
    UserTypeQuota,
)

logger = logging.getLogger(__name__)

CONFIG_MODELS = (
    EquipmentManager,
    EquipmentPI,
    EquipmentOperator,
    EquipmentSpecification,
    EquipmentPublication,
    EquipmentAccessory,
    EquipmentAdditionalAccessory,
    ChargeProfile,
    DynamicInputField,
    MultiParamDefinition,
    SlotMaster,
    UserTypeQuota,
    ExternalUserQuota,
)

# Identify one physical unit / its data PC; a copy must not inherit them.
UNIT_SPECIFIC_FIELDS = (
    "asset_serial_number",
    "purchase_order_ref",
    "purchase_invoice_ref",
    "purchase_date",
    "warranty_start",
    "warranty_end",
    "commissioning_date",
    "lifecycle_notes",
    "dsa_hostname",
    "dsa_ip_address",
    "dsa_share_name",
    "dsa_unc_path",
    "dsa_enabled",
    "dsa_watch_folder_enabled",
)

_CODE_MAX = Equipment._meta.get_field("code").max_length
_NAME_MAX = Equipment._meta.get_field("name").max_length


def suggest_copy_code(code: str) -> str:
    base = f"{code}-COPY"[:_CODE_MAX]
    candidate = base
    n = 2
    while Equipment.objects.filter(code__iexact=candidate).exists():
        suffix = str(n)
        candidate = f"{base[: _CODE_MAX - len(suffix)]}{suffix}"
        n += 1
    return candidate


def suggest_copy_name(name: str) -> str:
    return f"{name} (Copy)"[:_NAME_MAX]


def _copy_image(source: Equipment, target: Equipment) -> str | None:
    """Give the copy its own image object: replacing an image deletes the old storage key."""
    name = getattr(source.image, "name", "") or ""
    if not name:
        return None
    try:
        with source.image.storage.open(name, "rb") as fh:
            content = fh.read()
        if not content:
            return "Source image is empty; the copy has no image."
        target.image.save(os.path.basename(name), ContentFile(content), save=False)
        Equipment.objects.filter(pk=target.pk).update(image=target.image.name)
    except Exception as exc:
        logger.warning("Could not copy image for equipment %s -> %s: %s", source.pk, target.pk, exc)
        target.image.name = ""
        return "Image could not be copied; upload one on the copy."
    return None


def duplicate_equipment(
    source: Equipment,
    *,
    code: str | None = None,
    name: str | None = None,
    status: str = EquipmentStatus.INACTIVE,
    copy_image: bool = True,
) -> tuple[Equipment, list[str]]:
    """
    Create a new Equipment from ``source`` with the same settings, staff, pricing,
    input fields, slot masters and quotas. Bookings, daily slots and logs are not copied.
    Returns (new_equipment, warnings).
    """
    code = (code or "").strip() or suggest_copy_code(source.code)
    name = (name or "").strip() or suggest_copy_name(source.name)
    if Equipment.objects.filter(code__iexact=code).exists():
        raise ValueError(f'Equipment code "{code}" already exists.')

    warnings: list[str] = []
    with transaction.atomic():
        new = Equipment.objects.get(pk=source.pk)
        new.pk = None
        new._state.adding = True
        new.code = code
        new.name = name
        new.status = status
        new.image = ""
        new.video_file = ""
        for field_name in UNIT_SPECIFIC_FIELDS:
            field = Equipment._meta.get_field(field_name)
            setattr(new, field.attname, field.get_default())
        new.save()

        for model in CONFIG_MODELS:
            for row in model.objects.filter(equipment=source).order_by("pk"):
                row.pk = None
                row._state.adding = True
                row.equipment = new
                row.save()

        # Fabrication materials live in the master list; the copy supports the same ones as the source.
        new.supported_print_materials.set(source.supported_print_materials.all())
        new.supported_laser_sheet_materials.set(source.supported_laser_sheet_materials.all())

    if copy_image:
        msg = _copy_image(source, new)
        if msg:
            warnings.append(msg)
    if getattr(source.video_file, "name", ""):
        warnings.append("Video was not copied; upload one on the copy if needed.")
    return new, warnings

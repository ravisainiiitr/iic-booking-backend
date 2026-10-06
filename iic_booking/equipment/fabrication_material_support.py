"""Per-equipment material support for fabrication equipment.

The Fabrication Materials list is the master list: 3D print materials (``PrintMaterial``) and laser sheet
materials (``LaserSheetMaterial``). Each 3D printer / laser cutter supports one or more materials of its
own category from that list. Users can pick a material for a new booking only when it is supported by the
equipment AND enabled in the master list. Disabling a material keeps its links, so it comes back when it is
re-enabled. A material's ``equipment`` is the equipment it was added for; that equipment's managers edit it.

Rules enforced here (and on every write to the link tables, see the ``m2m_changed`` guard):
- 3D print materials may only be supported by 3D print equipment; sheet materials only by laser cutters.
- Two supported materials of one equipment may not share a code, because bookings store the code.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db.models.signals import m2m_changed, post_save, pre_save
from django.dispatch import receiver

from .models import Equipment, EquipmentProfileType, LaserSheetMaterial, PrintMaterial

NO_MATERIALS_MESSAGE = "No materials configured — contact the OIC."

MATERIAL_MODEL_BY_PROFILE = {
    EquipmentProfileType.PRINT_3D: PrintMaterial,
    EquipmentProfileType.LASER_CUT_2D: LaserSheetMaterial,
}
PROFILE_BY_MATERIAL_MODEL = {model: profile for profile, model in MATERIAL_MODEL_BY_PROFILE.items()}
CATEGORY_LABEL = {
    EquipmentProfileType.PRINT_3D: "3D print materials",
    EquipmentProfileType.LASER_CUT_2D: "laser sheet materials",
}


def material_model_for(equipment):
    return MATERIAL_MODEL_BY_PROFILE.get(getattr(equipment, "profile_type", None))


def supported_materials(equipment):
    """All materials (enabled or not) the equipment supports; empty for non-fabrication equipment."""
    model = material_model_for(equipment)
    if model is None or not getattr(equipment, "pk", None):
        return PrintMaterial.objects.none()
    return model.objects.filter(supported_equipment=equipment).order_by("display_order", "name", "pk")


def bookable_materials(equipment):
    """Materials users may choose for a new booking: supported by the equipment and enabled."""
    return supported_materials(equipment).filter(is_active=True)


def is_bookable_material(equipment, material) -> bool:
    if material is None or not isinstance(material, (PrintMaterial, LaserSheetMaterial)):
        return False
    if material_model_for(equipment) is not type(material) or not material.is_active:
        return False
    return material.supported_equipment.filter(pk=equipment.pk).exists()


def bookable_material_or_none(equipment, material_id):
    model = material_model_for(equipment)
    if model is None or material_id in (None, ""):
        return None
    try:
        return bookable_materials(equipment).filter(pk=int(material_id)).first()
    except (TypeError, ValueError):
        return None


def _category_error(equipment, model) -> str | None:
    profile = getattr(equipment, "profile_type", None)
    wanted = PROFILE_BY_MATERIAL_MODEL.get(model)
    if profile == wanted:
        return None
    label = CATEGORY_LABEL.get(wanted, "these materials")
    if profile in MATERIAL_MODEL_BY_PROFILE:
        return f"{equipment.code}: only {CATEGORY_LABEL[profile]} can be supported, not {label}."
    return f"{equipment.code}: only 3D print or laser cutting equipment can support {label}."


def _duplicate_code_error(equipment, materials) -> str | None:
    seen: dict[str, object] = {}
    for m in materials:
        key = (m.code or "").strip().lower()
        if key in seen and seen[key].pk != m.pk:
            return (
                f"{equipment.code}: '{seen[key].name}' and '{m.name}' share the code '{m.code}'. "
                "An equipment can support only one material per code."
            )
        seen[key] = m
    return None


def supported_links_error(equipment, materials) -> str | None:
    """Why ``materials`` cannot be the supported set of ``equipment`` (None when it can)."""
    for m in materials:
        err = _category_error(equipment, type(m))
        if err:
            return err
    return _duplicate_code_error(equipment, materials)


def set_supported_materials(equipment, material_ids):
    """Replace the supported set with master-list materials of the equipment's own category (ids are looked
    up in that category only). Returns (materials, error)."""
    model = material_model_for(equipment)
    if model is None:
        return None, "Only 3D print or laser cutting equipment can support fabrication materials."
    if not isinstance(material_ids, (list, tuple)):
        return None, "Send supported_material_ids as a list."
    try:
        ids = sorted({int(x) for x in material_ids})
    except (TypeError, ValueError):
        return None, "supported_material_ids must be material ids."
    materials = list(model.objects.filter(pk__in=ids))
    if len(materials) != len(ids):
        label = CATEGORY_LABEL[equipment.profile_type]
        return None, f"One or more selected materials are not {label} in the master list. Reload the page and try again."
    err = supported_links_error(equipment, materials)
    if err:
        return None, err
    getattr(equipment, related_name_for(model)).set(materials)
    return materials, None


def related_name_for(model) -> str:
    return "supported_print_materials" if model is PrintMaterial else "supported_laser_sheet_materials"


def code_change_error(material, new_code) -> str | None:
    """A code edit must not make two materials supported by the same equipment share a code."""
    new_key = (new_code or "").strip().lower()
    if not material.pk or new_key == (material.code or "").strip().lower():
        return None
    model = type(material)
    for eq in material.supported_equipment.all():
        clash = (
            model.objects.filter(supported_equipment=eq, code__iexact=new_key).exclude(pk=material.pk).first()
        )
        if clash is not None:
            return f"{eq.code} already supports '{clash.name}' with the code '{clash.code}'."
    return None


def new_material_code_error(equipment, code) -> str | None:
    """A material added for ``equipment`` is supported by it straight away, so its code must be free there."""
    model = material_model_for(equipment)
    if model is None:
        return None
    clash = model.objects.filter(supported_equipment=equipment, code__iexact=(code or "").strip()).first()
    if clash is not None:
        return f"{equipment.code} already supports '{clash.name}' with the code '{clash.code}'."
    return None


def remove_incompatible_links(equipment) -> int:
    """Drop links that no longer match the equipment's profile type (the materials stay in the master list)."""
    removed = 0
    for profile, model in MATERIAL_MODEL_BY_PROFILE.items():
        if equipment.profile_type != profile:
            removed += model.supported_equipment.through.objects.filter(equipment_id=equipment.pk).delete()[0]
    return removed


# --------------------------------------------------------------------------- signals


def _guard_links(sender, instance, action, reverse, model, pk_set, material_model, **kwargs):
    if action != "pre_add" or not pk_set:
        return
    if not reverse:
        # material.supported_equipment.add(<equipment ids>)
        for eq in Equipment.objects.filter(pk__in=pk_set):
            current = list(material_model.objects.filter(supported_equipment=eq).exclude(pk=instance.pk))
            err = supported_links_error(eq, current + [instance])
            if err:
                raise ValidationError(err)
        return
    # equipment.supported_*_materials.add(<material ids>)
    adding = list(material_model.objects.filter(pk__in=pk_set))
    current = list(material_model.objects.filter(supported_equipment=instance).exclude(pk__in=pk_set))
    err = supported_links_error(instance, current + adding)
    if err:
        raise ValidationError(err)


@receiver(m2m_changed, sender=PrintMaterial.supported_equipment.through)
def _guard_print_links(sender, **kwargs):
    _guard_links(sender, material_model=PrintMaterial, **kwargs)


@receiver(m2m_changed, sender=LaserSheetMaterial.supported_equipment.through)
def _guard_laser_links(sender, **kwargs):
    _guard_links(sender, material_model=LaserSheetMaterial, **kwargs)


def _support_on_own_equipment(instance, created, raw):
    """A material added for an equipment is offered on it (as before), unless the code is already taken there."""
    if not created or raw:
        return
    eq = instance.equipment
    if PROFILE_BY_MATERIAL_MODEL[type(instance)] != getattr(eq, "profile_type", None):
        return
    if new_material_code_error(eq, instance.code):
        return
    instance.supported_equipment.add(eq)


@receiver(post_save, sender=PrintMaterial)
def _support_new_print_material(sender, instance, created, raw=False, **kwargs):
    _support_on_own_equipment(instance, created, raw)


@receiver(post_save, sender=LaserSheetMaterial)
def _support_new_laser_material(sender, instance, created, raw=False, **kwargs):
    _support_on_own_equipment(instance, created, raw)


@receiver(pre_save, sender=Equipment)
def _remember_profile_type(sender, instance, raw=False, **kwargs):
    # Instances loaded from a queryset already know it (Equipment.from_db); others are looked up once.
    if raw or instance._state.adding or not instance.pk or hasattr(instance, "_loaded_profile_type"):
        return
    instance._loaded_profile_type = (
        Equipment.objects.filter(pk=instance.pk).values_list("profile_type", flat=True).first()
    )


@receiver(post_save, sender=Equipment)
def _drop_links_after_profile_change(sender, instance, created, raw=False, **kwargs):
    if created or raw:
        instance._loaded_profile_type = instance.profile_type
        return
    loaded = getattr(instance, "_loaded_profile_type", None)
    if loaded is None or loaded == instance.profile_type:
        return
    instance._loaded_profile_type = instance.profile_type
    if loaded in MATERIAL_MODEL_BY_PROFILE:
        remove_incompatible_links(instance)

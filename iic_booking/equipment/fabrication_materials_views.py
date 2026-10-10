"""Material management for fabrication equipment (3D printers and 2D laser cutters).

Allowed: Main Admin (all), Officer In Charge (equipment they manage), Department Admin with the
``equipment.manage`` grant (equipment in their own department).
"""

from decimal import Decimal, InvalidOperation

from django.core.validators import validate_email
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Count, Q
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.models.user_type import UserType

from .fabrication_material_support import (
    code_change_error,
    new_material_code_error,
    set_supported_materials,
    supported_materials,
)
from .models import (
    FABRICATION_PROFILE_TYPES,
    Equipment,
    EquipmentProfileType,
    LaserSheetMaterial,
    PrintMaterial,
)
from .laser_estimate_settings import ESTIMATE_PROFILE_KEYS as LASER_ESTIMATE_PROFILE_KEYS
from .laser_estimate_settings import apply_profile_update as apply_laser_profile_update
from .laser_estimate_settings import profile_payload as laser_profile_payload
from .print_estimate_calibration import apply_profile_update, profile_payload
from .serializers import LaserSheetMaterialSerializer, LaserSheetMaterialWriteSerializer, PrintMaterialSerializer

MAX_NOTIFICATION_EMAILS = 10


def _is_main_admin(user) -> bool:
    return getattr(user, "user_type", None) == UserType.ADMIN


def _dept_admin_department_id(user):
    from iic_booking.users.rbac import is_department_admin, user_has_permission

    if not is_department_admin(user) or not getattr(user, "department_id", None):
        return None
    if not user_has_permission(user, "equipment.manage", department_id=user.department_id):
        return None
    return user.department_id


def fabrication_manageable_equipment_qs(user):
    """Fabrication equipment whose materials and notification settings ``user`` may manage."""
    from .reports import get_equipment_ids_managed_by_oic

    base = Equipment.objects.filter(profile_type__in=FABRICATION_PROFILE_TYPES)
    if not user or not getattr(user, "is_authenticated", False):
        return base.none()
    if _is_main_admin(user):
        return base.order_by("code", "name")
    scope = Q(pk__in=[])
    if getattr(user, "user_type", None) == UserType.MANAGER:
        scope |= Q(equipment_id__in=list(get_equipment_ids_managed_by_oic(user.id)))
    dept_id = _dept_admin_department_id(user)
    if dept_id:
        scope |= Q(internal_department_id=dept_id)
    return base.filter(scope).order_by("code", "name")


def user_can_manage_fabrication_materials(user, equipment) -> bool:
    if equipment is None or getattr(equipment, "profile_type", None) not in FABRICATION_PROFILE_TYPES:
        return False
    return fabrication_manageable_equipment_qs(user).filter(pk=equipment.pk).exists()


def user_has_fabrication_equipment(user) -> bool:
    return fabrication_manageable_equipment_qs(user).exists()


def clean_notification_emails(raw):
    """Normalise a list (or comma/newline separated string) of addresses. Returns (emails, error)."""
    if raw is None:
        return [], None
    if isinstance(raw, str):
        items = [p for chunk in raw.replace(";", ",").splitlines() for p in chunk.split(",")]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        return None, "Send a list of email addresses."
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in items:
        email = str(item or "").strip()
        if not email:
            continue
        try:
            validate_email(email)
        except DjangoValidationError:
            return None, f"'{email}' is not a valid email address."
        if email.lower() in seen:
            continue
        seen.add(email.lower())
        cleaned.append(email)
    if len(cleaned) > MAX_NOTIFICATION_EMAILS:
        return None, f"Add at most {MAX_NOTIFICATION_EMAILS} notification emails."
    return cleaned, None


def clean_own_material_fixed_charge(raw):
    """Returns (value_or_None, error)."""
    if raw in (None, ""):
        return None, None
    try:
        value = Decimal(str(raw)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return None, "Enter the own-material charge as a number, or leave it empty."
    if value < 0:
        return None, "The own-material charge cannot be negative."
    return value, None


ESTIMATE_PROFILE_KEYS = (
    "print_estimate_preset",
    "print_estimate_overrides",
    "print_estimate_calibration",
    "print_estimate_support_material_ids",
)
MAX_PRINT_SIZE_LIMIT_MM = Decimal("10000")
PRINT_SIZE_FIELDS = ("max_print_size_x_mm", "max_print_size_y_mm", "max_print_size_z_mm")


def clean_max_print_size(raw, axis: str):
    """Returns (Decimal_or_None, error). Blank means no limit on this axis."""
    if raw in (None, ""):
        return None, None
    try:
        value = Decimal(str(raw).strip()).quantize(Decimal("0.1"))
    except (InvalidOperation, ValueError, TypeError):
        return None, f"Enter the maximum print size {axis} in mm as a number, or leave it empty for no limit."
    if value <= 0:
        return None, f"The maximum print size {axis} must be more than 0 mm."
    if value > MAX_PRINT_SIZE_LIMIT_MM:
        return None, f"The maximum print size {axis} must be at most {MAX_PRINT_SIZE_LIMIT_MM} mm."
    return value, None


def _decimal_or_none(value):
    return str(value) if value is not None else None


def _equipment_row(eq):
    row = {
        "equipment_id": eq.equipment_id,
        "equipment_code": eq.code,
        "equipment_name": eq.name,
        "profile_type": eq.profile_type,
        "internal_department_name": getattr(eq.internal_department, "name", None),
        "fabrication_notification_emails": list(eq.fabrication_notification_emails or []),
        "own_material_fixed_charge": (
            str(eq.own_material_fixed_charge) if eq.own_material_fixed_charge is not None else None
        ),
        "fabrication_replace_window_hours": eq.fabrication_replace_window_hours,
    }
    if eq.profile_type == EquipmentProfileType.PRINT_3D:
        for field in PRINT_SIZE_FIELDS:
            row[field] = _decimal_or_none(getattr(eq, field))
        row["allow_print_rotation_to_fit"] = eq.allow_print_rotation_to_fit is not False
        row["print_estimate"] = profile_payload(eq)
        row["print_materials"] = PrintMaterialSerializer(
            eq.print_materials.all().order_by("display_order", "name"), many=True
        ).data
    else:
        row["laser_sheet_materials"] = LaserSheetMaterialSerializer(
            eq.laser_sheet_materials.all().order_by("display_order", "name"), many=True
        ).data
        row["laser_estimate"] = laser_profile_payload(eq)
    row["supported_material_ids"] = sorted(supported_materials(eq).values_list("pk", flat=True))
    return row


def _master_list(model, serializer_cls, managed_ids):
    """Every material of one category, with the equipment it was added for and who may edit it."""
    rows = []
    qs = (
        model.objects.select_related("equipment")
        .annotate(supported_equipment_count=Count("supported_equipment", distinct=True))
        .order_by("display_order", "name", "pk")
    )
    for m in qs:
        data = dict(serializer_cls(m).data)
        data.update(
            {
                "home_equipment_id": m.equipment_id,
                "home_equipment_code": m.equipment.code,
                "home_equipment_name": m.equipment.name,
                "can_edit": m.equipment_id in managed_ids,
                "supported_equipment_count": m.supported_equipment_count,
            }
        )
        rows.append(data)
    return rows


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def fabrication_material_equipment(request):
    """
    GET: fabrication equipment the user manages, with all materials (active and disabled), each
         equipment's supported_material_ids, and the master list of every category they manage.
    PATCH: {equipment_id, fabrication_notification_emails?, own_material_fixed_charge?,
            fabrication_replace_window_hours?, supported_material_ids?,
            max_print_size_x_mm? / _y_mm? / _z_mm? (blank = no limit), allow_print_rotation_to_fit?,
            print_estimate_preset? ("" = detect from Make / Model), print_estimate_overrides? {param: value},
            print_estimate_calibration? ("fit" | "apply" | "off"),
            laser_estimate_preset? ("" = detect from Make / Model / Name), laser_estimate_overrides? {param: value},
            laser_estimate_material_overrides? {material_id: {cut_speed_mm_s?, pierce_s?}}}.
    """
    qs = fabrication_manageable_equipment_qs(request.user).select_related("internal_department")
    if request.method == "GET":
        equipments = list(qs.prefetch_related("print_materials", "laser_sheet_materials"))
        rows = [_equipment_row(eq) for eq in equipments]
        managed_ids = {eq.pk for eq in equipments}
        has_print = any(r["profile_type"] == EquipmentProfileType.PRINT_3D for r in rows)
        has_laser = any(r["profile_type"] == EquipmentProfileType.LASER_CUT_2D for r in rows)
        return Response(
            {
                "equipments": rows,
                "has_fabrication_equipment": bool(rows),
                "has_print_3d_equipment": has_print,
                "has_laser_cut_equipment": has_laser,
                "master_print_materials": (
                    _master_list(PrintMaterial, PrintMaterialSerializer, managed_ids) if has_print else []
                ),
                "master_laser_sheet_materials": (
                    _master_list(LaserSheetMaterial, LaserSheetMaterialSerializer, managed_ids) if has_laser else []
                ),
            }
        )

    data = request.data or {}
    try:
        eq_id = int(data.get("equipment_id"))
    except (TypeError, ValueError):
        return Response({"error": "equipment_id is required."}, status=status.HTTP_400_BAD_REQUEST)
    eq = qs.filter(pk=eq_id).first()
    if eq is None:
        return Response({"error": "Permission denied for this equipment."}, status=status.HTTP_403_FORBIDDEN)
    update_fields = []
    if "fabrication_notification_emails" in data:
        emails, err = clean_notification_emails(data.get("fabrication_notification_emails"))
        if err:
            return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)
        eq.fabrication_notification_emails = emails
        # Deprecated single-address column, kept in step so a rollback to an older release still has it.
        eq.print_3d_stl_notification_email = emails[0] if emails else ""
        update_fields += ["fabrication_notification_emails", "print_3d_stl_notification_email"]
    if "own_material_fixed_charge" in data:
        value, err = clean_own_material_fixed_charge(data.get("own_material_fixed_charge"))
        if err:
            return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)
        eq.own_material_fixed_charge = value
        update_fields.append("own_material_fixed_charge")
    if "fabrication_replace_window_hours" in data:
        from .fabrication_workflow import clean_replace_window_hours

        hours, err = clean_replace_window_hours(data.get("fabrication_replace_window_hours"))
        if err:
            return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)
        eq.fabrication_replace_window_hours = hours
        update_fields.append("fabrication_replace_window_hours")
    size_keys = [f for f in PRINT_SIZE_FIELDS if f in data]
    if (size_keys or "allow_print_rotation_to_fit" in data) and eq.profile_type != EquipmentProfileType.PRINT_3D:
        return Response(
            {"error": "The maximum print size applies to 3D printing equipment only."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    for field in size_keys:
        value, err = clean_max_print_size(data.get(field), field[len("max_print_size_")].upper())
        if err:
            return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)
        setattr(eq, field, value)
        update_fields.append(field)
    if "allow_print_rotation_to_fit" in data:
        raw = data.get("allow_print_rotation_to_fit")
        eq.allow_print_rotation_to_fit = str(raw).strip().lower() not in ("false", "0", "no", "off", "none", "")
        update_fields.append("allow_print_rotation_to_fit")
    estimate_keys = [k for k in ESTIMATE_PROFILE_KEYS if k in data]
    if estimate_keys:
        if eq.profile_type != EquipmentProfileType.PRINT_3D:
            return Response(
                {"error": "The estimate profile applies to 3D printing equipment only."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        err = apply_profile_update(eq, data)
        if err:
            return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)
        update_fields.append("print_estimate_profile")
    if any(k in data for k in LASER_ESTIMATE_PROFILE_KEYS):
        if eq.profile_type != EquipmentProfileType.LASER_CUT_2D:
            return Response(
                {"error": "The cutting time estimate applies to 2D laser cutting equipment only."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        err = apply_laser_profile_update(eq, data)
        if err:
            return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)
        update_fields.append("laser_estimate_profile")
    # A rejected material list rolls back the other settings sent in the same request.
    with transaction.atomic():
        if update_fields:
            eq.save(update_fields=update_fields)
        if "supported_material_ids" in data:
            _materials, err = set_supported_materials(eq, data.get("supported_material_ids"))
            if err:
                transaction.set_rollback(True)
                return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)
    return Response({"equipment": _equipment_row(eq)})


def _laser_equipment_or_error(user, equipment_id):
    try:
        eq_id = int(equipment_id)
    except (TypeError, ValueError):
        return None, Response({"error": "equipment_id is required."}, status=status.HTTP_400_BAD_REQUEST)
    eq = Equipment.objects.filter(pk=eq_id).first()
    if eq is None:
        return None, Response({"error": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)
    if eq.profile_type != EquipmentProfileType.LASER_CUT_2D:
        return None, Response(
            {"error": "Sheet materials can only be managed for 2D laser cutting equipment."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not user_can_manage_fabrication_materials(user, eq):
        return None, Response({"error": "Permission denied for this equipment."}, status=status.HTTP_403_FORBIDDEN)
    return eq, None


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def laser_sheet_materials_manage(request):
    """GET ?equipment_id=: all sheet materials of a laser cutter. POST: create one."""
    if request.method == "GET":
        eq, err = _laser_equipment_or_error(request.user, request.query_params.get("equipment_id"))
        if err:
            return err
        materials = eq.laser_sheet_materials.all().order_by("display_order", "name")
        return Response({"materials": LaserSheetMaterialSerializer(materials, many=True).data})

    data = request.data or {}
    eq, err = _laser_equipment_or_error(request.user, data.get("equipment_id"))
    if err:
        return err
    serializer = LaserSheetMaterialWriteSerializer(data=data)
    if not serializer.is_valid():
        return Response({"error": "Please correct the material details.", "errors": serializer.errors},
                        status=status.HTTP_400_BAD_REQUEST)
    vals = dict(serializer.validated_data)
    vals.pop("id", None)
    vals["code"] = vals["code"].strip()
    if LaserSheetMaterial.objects.filter(equipment=eq, code=vals["code"]).exists():
        return Response(
            {"error": f"A material with code '{vals['code']}' already exists for this equipment."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    clash = new_material_code_error(eq, vals["code"])
    if clash:
        return Response({"error": f"{clash} Use a different code."}, status=status.HTTP_400_BAD_REQUEST)
    vals["user_type"] = (vals.get("user_type") or "").strip() or None
    material = LaserSheetMaterial.objects.create(equipment=eq, **vals)
    return Response({"material": LaserSheetMaterialSerializer(material).data}, status=status.HTTP_201_CREATED)


@api_view(["PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def laser_sheet_material_detail(request, material_id):
    material = LaserSheetMaterial.objects.select_related("equipment").filter(pk=material_id).first()
    if material is None:
        return Response({"error": "Material not found."}, status=status.HTTP_404_NOT_FOUND)
    _eq, err = _laser_equipment_or_error(request.user, material.equipment_id)
    if err:
        return err

    if request.method == "DELETE":
        if material.analyses.exists():
            return Response(
                {"error": "This material is used by uploaded parts or bookings, so it cannot be deleted. Disable it instead."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        material.delete()
        return Response({"ok": True})

    current = LaserSheetMaterialSerializer(material).data
    merged = {**current, **{k: v for k, v in (request.data or {}).items() if k in current}}
    serializer = LaserSheetMaterialWriteSerializer(data=merged)
    if not serializer.is_valid():
        return Response({"error": "Please correct the material details.", "errors": serializer.errors},
                        status=status.HTTP_400_BAD_REQUEST)
    vals = dict(serializer.validated_data)
    vals.pop("id", None)
    vals["code"] = vals["code"].strip()
    if (
        LaserSheetMaterial.objects.filter(equipment_id=material.equipment_id, code=vals["code"])
        .exclude(pk=material.pk)
        .exists()
    ):
        return Response(
            {"error": f"A material with code '{vals['code']}' already exists for this equipment."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    clash = code_change_error(material, vals["code"])
    if clash:
        return Response({"error": f"{clash} Use a different code."}, status=status.HTTP_400_BAD_REQUEST)
    vals["user_type"] = (vals.get("user_type") or "").strip() or None
    for key, value in vals.items():
        setattr(material, key, value)
    material.save()
    return Response({"material": LaserSheetMaterialSerializer(material).data})

"""Material management for fabrication equipment (3D printers and 2D laser cutters).

Allowed: Main Admin (all), Officer In Charge (equipment they manage), Department Admin with the
``equipment.manage`` grant (equipment in their own department).
"""

from decimal import Decimal, InvalidOperation

from django.core.validators import validate_email
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.models.user_type import UserType

from .models import (
    FABRICATION_PROFILE_TYPES,
    Equipment,
    EquipmentProfileType,
    LaserSheetMaterial,
)
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
        row["print_materials"] = PrintMaterialSerializer(
            eq.print_materials.all().order_by("display_order", "name"), many=True
        ).data
    else:
        row["laser_sheet_materials"] = LaserSheetMaterialSerializer(
            eq.laser_sheet_materials.all().order_by("display_order", "name"), many=True
        ).data
    return row


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def fabrication_material_equipment(request):
    """
    GET: fabrication equipment the user manages, with all materials (active and disabled).
    PATCH: {equipment_id, fabrication_notification_emails?, own_material_fixed_charge?,
            fabrication_replace_window_hours?}.
    """
    qs = fabrication_manageable_equipment_qs(request.user).select_related("internal_department")
    if request.method == "GET":
        rows = [_equipment_row(eq) for eq in qs.prefetch_related("print_materials", "laser_sheet_materials")]
        return Response(
            {
                "equipments": rows,
                "has_fabrication_equipment": bool(rows),
                "has_print_3d_equipment": any(r["profile_type"] == EquipmentProfileType.PRINT_3D for r in rows),
                "has_laser_cut_equipment": any(
                    r["profile_type"] == EquipmentProfileType.LASER_CUT_2D for r in rows
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
    if update_fields:
        eq.save(update_fields=update_fields)
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
    vals["user_type"] = (vals.get("user_type") or "").strip() or None
    for key, value in vals.items():
        setattr(material, key, value)
    material.save()
    return Response({"material": LaserSheetMaterialSerializer(material).data})

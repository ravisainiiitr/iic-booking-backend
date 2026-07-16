"""Public propose + Admin approve API for equipment addition requests."""

from __future__ import annotations

import logging
from datetime import datetime

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone
from rest_framework import permissions, serializers, status, throttling
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    parser_classes,
    permission_classes,
    throttle_classes,
)
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from iic_booking.users.api.token_auth import TokenAuthenticationWithInactivity
from iic_booking.users.models.department import (
    Department,
    DepartmentType,
    InternalDepartmentSubcategory,
)
from iic_booking.users.models.user import User
from iic_booking.users.models.user_type import UserType
from iic_booking.users.rbac import is_department_admin, user_has_permission

from .models import (
    Equipment,
    EquipmentAdditionRequest,
    EquipmentAdditionRequestStatus,
    EquipmentStatus,
)

logger = logging.getLogger(__name__)


class EquipmentAdditionSubmitThrottle(throttling.AnonRateThrottle):
    scope = "equipment_addition_submit"

    def get_rate(self):
        return "10/hour"


class IsAdminUserType(permissions.BasePermission):
    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and getattr(user, "user_type", None) == UserType.ADMIN
        )


class IsEquipmentAdditionReviewer(permissions.BasePermission):
    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if getattr(user, "user_type", None) == UserType.ADMIN:
            return True
        return is_department_admin(user) and user_has_permission(user, "equipment.request_add", department_id=user.department_id)


def _parse_optional_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise serializers.ValidationError("Must be a whole number.")


def _parse_optional_time(value):
    if value is None or value == "":
        return None
    if hasattr(value, "hour"):
        return value
    s = str(value).strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            continue
    raise serializers.ValidationError("Use HH:MM (24-hour) format.")


class EquipmentAdditionRequestCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255)
    code = serializers.CharField(max_length=255)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    make = serializers.CharField(required=False, allow_blank=True, default="", max_length=255)
    model_information = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=255
    )
    year_of_installation = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=20
    )
    location = serializers.CharField(required=False, allow_blank=True, default="")
    specifications = serializers.CharField(required=False, allow_blank=True, default="")
    sample_requirements = serializers.CharField(required=False, allow_blank=True, default="")
    slots_per_day = serializers.CharField(required=False, allow_blank=True, default="")
    slot_duration_minutes = serializers.CharField(required=False, allow_blank=True, default="")
    slot_start_time = serializers.CharField(required=False, allow_blank=True, default="")
    slot_end_time = serializers.CharField(required=False, allow_blank=True, default="")
    charge_calculation_basis = serializers.CharField(required=False, allow_blank=True, default="")
    time_calculation_basis = serializers.CharField(required=False, allow_blank=True, default="")
    charge_iitr_student = serializers.CharField(required=False, allow_blank=True, default="", max_length=255)
    charge_iitr_faculty = serializers.CharField(required=False, allow_blank=True, default="", max_length=255)
    charge_external_educational_student = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=255
    )
    charge_external_govt_rnd = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=255
    )
    charge_industry = serializers.CharField(required=False, allow_blank=True, default="", max_length=255)
    charge_startup_incubated_iitr = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=255
    )
    charge_external_startup_msme = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=255
    )
    equipment_image = serializers.ImageField(required=False, allow_null=True)
    supporting_document = serializers.FileField(required=False, allow_null=True)
    internal_department = serializers.IntegerField(required=False, allow_null=True)
    proposed_oic_name = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=255
    )
    proposed_oic_email = serializers.EmailField(required=False, allow_blank=True, default="")
    proposed_operator_name = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=255
    )
    proposed_operator_email = serializers.EmailField(
        required=False, allow_blank=True, default=""
    )
    submitter_name = serializers.CharField(max_length=255)
    submitter_email = serializers.EmailField()
    submitter_phone = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=40
    )
    notes = serializers.CharField(required=False, allow_blank=True, default="")
    website = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_website(self, value):
        if (value or "").strip():
            raise serializers.ValidationError("Invalid submission.")
        return ""

    def validate_code(self, value):
        code = (value or "").strip()
        if not code:
            raise serializers.ValidationError("Code is required.")
        if Equipment.objects.filter(code__iexact=code).exists():
            raise serializers.ValidationError(
                "An equipment with this code already exists. Choose a different code."
            )
        if EquipmentAdditionRequest.objects.filter(
            code__iexact=code,
            status=EquipmentAdditionRequestStatus.PENDING,
        ).exists():
            raise serializers.ValidationError(
                "A pending request with this code already exists."
            )
        return code

    def validate_name(self, value):
        name = (value or "").strip()
        if not name:
            raise serializers.ValidationError("Name is required.")
        return name

    def validate_internal_department(self, value):
        if value is None or value == "":
            return None
        dept = Department.objects.filter(
            pk=value,
            department_type=DepartmentType.INTERNAL,
            internal_subcategory=InternalDepartmentSubcategory.IIT_ROORKEE_DEPT_CENTRES,
        ).first()
        if not dept:
            raise serializers.ValidationError(
                "Select an IIT Roorkee Department/Centre."
            )
        return int(value)

    def validate_slots_per_day(self, value):
        return _parse_optional_int(value)

    def validate_slot_duration_minutes(self, value):
        return _parse_optional_int(value)

    def validate_slot_start_time(self, value):
        return _parse_optional_time(value)

    def validate_slot_end_time(self, value):
        return _parse_optional_time(value)

    def validate(self, attrs):
        start = attrs.get("slot_start_time")
        end = attrs.get("slot_end_time")
        if start and end and end <= start:
            raise serializers.ValidationError(
                {"slot_end_time": "End time must be after start time."}
            )
        return attrs

    def create(self, validated_data):
        validated_data.pop("website", None)
        dept_id = validated_data.pop("internal_department", None)
        image = validated_data.pop("equipment_image", None)
        document = validated_data.pop("supporting_document", None)
        obj = EquipmentAdditionRequest(
            internal_department_id=dept_id,
            **validated_data,
        )
        if image:
            obj.equipment_image = image
        if document:
            obj.supporting_document = document
        obj.save()
        return obj


class EquipmentAdditionRequestSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    internal_department_name = serializers.SerializerMethodField()
    internal_department_code = serializers.SerializerMethodField()
    reviewed_by_name = serializers.SerializerMethodField()
    created_equipment_id = serializers.SerializerMethodField()
    created_equipment_code = serializers.SerializerMethodField()
    equipment_image_url = serializers.SerializerMethodField()
    supporting_document_url = serializers.SerializerMethodField()
    supporting_document_name = serializers.SerializerMethodField()

    class Meta:
        model = EquipmentAdditionRequest
        fields = [
            "id",
            "status",
            "status_display",
            "name",
            "code",
            "description",
            "make",
            "model_information",
            "year_of_installation",
            "location",
            "specifications",
            "sample_requirements",
            "slots_per_day",
            "slot_duration_minutes",
            "slot_start_time",
            "slot_end_time",
            "charge_calculation_basis",
            "time_calculation_basis",
            "charge_iitr_student",
            "charge_iitr_faculty",
            "charge_external_educational_student",
            "charge_external_govt_rnd",
            "charge_industry",
            "charge_startup_incubated_iitr",
            "charge_external_startup_msme",
            "equipment_image_url",
            "supporting_document_url",
            "supporting_document_name",
            "internal_department",
            "internal_department_name",
            "internal_department_code",
            "proposed_oic_name",
            "proposed_oic_email",
            "proposed_operator_name",
            "proposed_operator_email",
            "submitter_name",
            "submitter_email",
            "submitter_phone",
            "notes",
            "reviewed_by",
            "reviewed_by_name",
            "reviewed_at",
            "review_notes",
            "created_equipment_id",
            "created_equipment_code",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_internal_department_name(self, obj):
        return obj.internal_department.name if obj.internal_department_id else None

    def get_internal_department_code(self, obj):
        return obj.internal_department.code if obj.internal_department_id else None

    def get_reviewed_by_name(self, obj):
        if not obj.reviewed_by_id:
            return None
        u = obj.reviewed_by
        return (u.name or u.email or "").strip() or None

    def get_created_equipment_id(self, obj):
        if not obj.created_equipment_id:
            return None
        return obj.created_equipment.equipment_id

    def get_created_equipment_code(self, obj):
        if not obj.created_equipment_id:
            return None
        return obj.created_equipment.code

    def _file_url(self, field_file):
        if not field_file:
            return None
        try:
            url = field_file.url
        except Exception:
            return None
        request = self.context.get("request")
        if request and url and not str(url).startswith("http"):
            return request.build_absolute_uri(url)
        return url

    def get_equipment_image_url(self, obj):
        return self._file_url(obj.equipment_image) if obj.equipment_image else None

    def get_supporting_document_url(self, obj):
        return self._file_url(obj.supporting_document) if obj.supporting_document else None

    def get_supporting_document_name(self, obj):
        if not obj.supporting_document:
            return None
        try:
            name = obj.supporting_document.name
            return name.rsplit("/", 1)[-1] if name else None
        except Exception:
            return None


def _notify_admins_new_request(req: EquipmentAdditionRequest) -> None:
    emails = list(
        User.objects.filter(user_type=UserType.ADMIN, is_active=True)
        .exclude(email="")
        .values_list("email", flat=True)
    )
    if not emails:
        return
    frontend = (getattr(settings, "FRONTEND_URL", "") or "").strip().rstrip("/")
    review_url = (
        f"{frontend}/admin/equipment-addition-requests"
        if frontend
        else "/admin/equipment-addition-requests"
    )
    subject = f"[IIC] New equipment addition request: {req.code}"
    message = (
        f"A new equipment addition request was submitted.\n\n"
        f"Name: {req.name}\n"
        f"Code: {req.code}\n"
        f"Submitter: {req.submitter_name} <{req.submitter_email}>\n\n"
        f"Review: {review_url}\n"
    )
    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=emails,
            fail_silently=True,
        )
    except Exception:
        logger.exception("Failed to email admins about equipment addition request %s", req.id)


def _build_setup_instruction(req: EquipmentAdditionRequest) -> str:
    lines = [
        "Created from a public equipment addition request. "
        "Configure slots, charges, Officer In-charge / Lab Operator, then set status to Operational.",
    ]
    if req.year_of_installation:
        lines.append(f"Year of installation: {req.year_of_installation}")
    if req.specifications:
        lines.append(f"Specifications:\n{req.specifications.strip()}")
    if req.sample_requirements:
        lines.append(f"Sample requirements:\n{req.sample_requirements.strip()}")
    slot_bits = []
    if req.slots_per_day is not None:
        slot_bits.append(f"slots/day={req.slots_per_day}")
    if req.slot_duration_minutes is not None:
        slot_bits.append(f"duration={req.slot_duration_minutes} min")
    if req.slot_start_time:
        slot_bits.append(f"start={req.slot_start_time.strftime('%H:%M')}")
    if req.slot_end_time:
        slot_bits.append(f"end={req.slot_end_time.strftime('%H:%M')}")
    if slot_bits:
        lines.append("Slot proposal: " + ", ".join(slot_bits))
    if req.charge_calculation_basis:
        lines.append(f"Charge calculation basis: {req.charge_calculation_basis.strip()}")
    if req.time_calculation_basis:
        lines.append(f"Time calculation basis: {req.time_calculation_basis.strip()}")
    charge_lines = [
        ("IITR Students", req.charge_iitr_student),
        ("IITR Faculty", req.charge_iitr_faculty),
        ("External Educational Student", req.charge_external_educational_student),
        ("External Government R&D", req.charge_external_govt_rnd),
        ("Industry", req.charge_industry),
        ("Startup Incubated at IIT Roorkee", req.charge_startup_incubated_iitr),
        ("External Startup/MSME", req.charge_external_startup_msme),
    ]
    filled = [f"  {label}: {val}" for label, val in charge_lines if (val or "").strip()]
    if filled:
        lines.append("Category charges:\n" + "\n".join(filled))
    if req.proposed_oic_name or req.proposed_oic_email:
        lines.append(
            f"Proposed OIC: {(req.proposed_oic_name or '').strip()} {(req.proposed_oic_email or '').strip()}".strip()
        )
    if req.proposed_operator_name or req.proposed_operator_email:
        lines.append(
            f"Proposed operator: {(req.proposed_operator_name or '').strip()} "
            f"{(req.proposed_operator_email or '').strip()}".strip()
        )
    if req.notes:
        lines.append(f"Submitter notes: {req.notes.strip()}")
    return "\n".join(lines)


@api_view(["GET"])
@authentication_classes([])
@permission_classes([permissions.AllowAny])
def equipment_addition_form_choices(request):
    depts = (
        Department.objects.filter(
            department_type=DepartmentType.INTERNAL,
            internal_subcategory=InternalDepartmentSubcategory.IIT_ROORKEE_DEPT_CENTRES,
        )
        .order_by("name")
        .values("id", "name", "code")
    )
    return Response({"internal_departments": list(depts)})


@api_view(["POST"])
@authentication_classes([])
@permission_classes([permissions.AllowAny])
@throttle_classes([EquipmentAdditionSubmitThrottle])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def equipment_addition_request_create(request):
    serializer = EquipmentAdditionRequestCreateSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    obj = serializer.save()
    try:
        _notify_admins_new_request(obj)
    except Exception:
        logger.exception("Notify admins failed for equipment addition request %s", obj.id)
    return Response(
        {
            "id": obj.id,
            "message": "Request submitted. An administrator will review it.",
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(["GET"])
@authentication_classes([TokenAuthenticationWithInactivity, SessionAuthentication])
@permission_classes([IsEquipmentAdditionReviewer])
def equipment_addition_request_list(request):
    qs = EquipmentAdditionRequest.objects.select_related(
        "internal_department", "reviewed_by", "created_equipment"
    ).all()
    if getattr(request.user, "user_type", None) == UserType.DEPT_ADMIN:
        qs = qs.filter(internal_department_id=request.user.department_id)
    status_filter = (request.query_params.get("status") or "").strip().upper()
    if status_filter and status_filter != "ALL":
        qs = qs.filter(status=status_filter)
    data = EquipmentAdditionRequestSerializer(
        qs[:500], many=True, context={"request": request}
    ).data
    return Response({"results": data, "count": len(data)})


@api_view(["GET"])
@authentication_classes([TokenAuthenticationWithInactivity, SessionAuthentication])
@permission_classes([IsEquipmentAdditionReviewer])
def equipment_addition_request_detail(request, pk: int):
    try:
        obj = EquipmentAdditionRequest.objects.select_related(
            "internal_department", "reviewed_by", "created_equipment"
        ).get(pk=pk)
    except EquipmentAdditionRequest.DoesNotExist:
        return Response({"error": "Request not found."}, status=status.HTTP_404_NOT_FOUND)
    if getattr(request.user, "user_type", None) == UserType.DEPT_ADMIN and obj.internal_department_id != request.user.department_id:
        return Response({"error": "Request not found."}, status=status.HTTP_404_NOT_FOUND)
    return Response(
        EquipmentAdditionRequestSerializer(obj, context={"request": request}).data
    )


@api_view(["POST"])
@authentication_classes([TokenAuthenticationWithInactivity, SessionAuthentication])
@permission_classes([IsAdminUserType])
def equipment_addition_request_approve(request, pk: int):
    try:
        obj = EquipmentAdditionRequest.objects.select_related("internal_department").get(pk=pk)
    except EquipmentAdditionRequest.DoesNotExist:
        return Response({"error": "Request not found."}, status=status.HTTP_404_NOT_FOUND)

    if obj.status != EquipmentAdditionRequestStatus.PENDING:
        return Response(
            {"error": f"Only pending requests can be approved (current: {obj.status})."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    code = (obj.code or "").strip()
    if Equipment.objects.filter(code__iexact=code).exists():
        return Response(
            {
                "error": (
                    f"Equipment code '{code}' is already in use. "
                    "Update the live equipment or reject this request."
                )
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    review_notes = (request.data.get("review_notes") or "").strip()

    with transaction.atomic():
        create_kwargs = {
            "name": obj.name.strip(),
            "code": code,
            "description": obj.description or "",
            "make": obj.make or "",
            "model_information": obj.model_information or "",
            "location": obj.location or "",
            "internal_department": obj.internal_department,
            "status": EquipmentStatus.INACTIVE,
            "important_instruction": _build_setup_instruction(obj),
        }
        if obj.slots_per_day is not None:
            create_kwargs["slots_per_day"] = obj.slots_per_day
        if obj.slot_duration_minutes is not None:
            create_kwargs["slot_duration_minutes"] = obj.slot_duration_minutes

        equipment = Equipment(**create_kwargs)
        if obj.equipment_image:
            try:
                obj.equipment_image.open("rb")
                equipment.image.save(
                    obj.equipment_image.name.rsplit("/", 1)[-1],
                    ContentFile(obj.equipment_image.read()),
                    save=False,
                )
            except Exception:
                logger.exception(
                    "Could not copy proposal image to equipment for request %s", obj.id
                )
        equipment.save()

        obj.status = EquipmentAdditionRequestStatus.APPROVED
        obj.reviewed_by = request.user
        obj.reviewed_at = timezone.now()
        obj.review_notes = review_notes
        obj.created_equipment = equipment
        obj.save(
            update_fields=[
                "status",
                "reviewed_by",
                "reviewed_at",
                "review_notes",
                "created_equipment",
                "updated_at",
            ]
        )

    return Response(
        {
            "message": (
                "Request approved. Equipment created (Under Maintenance). "
                "Finish slots/charges, then set Operational."
            ),
            "equipment_id": equipment.equipment_id,
            "equipment_code": equipment.code,
            "request": EquipmentAdditionRequestSerializer(
                obj, context={"request": request}
            ).data,
        }
    )


@api_view(["POST"])
@authentication_classes([TokenAuthenticationWithInactivity, SessionAuthentication])
@permission_classes([IsAdminUserType])
def equipment_addition_request_reject(request, pk: int):
    try:
        obj = EquipmentAdditionRequest.objects.get(pk=pk)
    except EquipmentAdditionRequest.DoesNotExist:
        return Response({"error": "Request not found."}, status=status.HTTP_404_NOT_FOUND)

    if obj.status != EquipmentAdditionRequestStatus.PENDING:
        return Response(
            {"error": f"Only pending requests can be rejected (current: {obj.status})."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    review_notes = (request.data.get("review_notes") or "").strip()
    obj.status = EquipmentAdditionRequestStatus.REJECTED
    obj.reviewed_by = request.user
    obj.reviewed_at = timezone.now()
    obj.review_notes = review_notes
    obj.save(
        update_fields=["status", "reviewed_by", "reviewed_at", "review_notes", "updated_at"]
    )
    return Response(
        {
            "message": "Request rejected.",
            "request": EquipmentAdditionRequestSerializer(
                obj, context={"request": request}
            ).data,
        }
    )

"""Serializers for Equipment models."""

import logging
from decimal import Decimal
from rest_framework import serializers
from django.core.files.storage import default_storage
from django.db.models import QuerySet
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from .calculators import ChargeCalculationEngine, build_safe_input_values_for_charge_calculation
from .models import (
    Equipment,
    EquipmentStatus,
    EquipmentCategory,
    EquipmentGroup,
    EquipmentGroupQuota,
    EquipmentSpecification,
    EquipmentAccessory,
    EquipmentAdditionalAccessory,
    DynamicInputField,
    DynamicInputFieldType,
    ChargeProfile,
    ChargeProfilePricingProfile,
    MultiParamDefinition,
    SlotMaster,
    DailySlot,
    SlotStatus,
    EquipmentOperator,
    EquipmentManager,
    Booking,
    BookingStatus,
    IstemFbrStatus,
    BookingEvent,
    BookingSampleTrace,
    BookingCancellationRequest,
    RepeatSampleRequest,
    TARewardConfig,
    TAAssignment,
    TADutyLog,
    TADutyLogStatus,
    TARewardLedger,
    TARewardLedgerEntryType,
    TARewardLedgerSourceType,
    BookingRewardRedemption,
    InventoryItem,
    EquipmentInventoryItem,
    EquipmentItemStock,
    InventoryRequest,
    InventoryRequestLine,
    InventoryTransaction,
    IssuedAsset,
    ProcurementRequest,
    ProcurementRequestLine,
    ProcurementAttachment,
    ProcurementActionLog,
    EquipmentAMCContract,
    EquipmentExpense,
    EquipmentWriteOffRequest,
    EquipmentWriteOffActionLog,
    EquipmentProfileType,
    PrintMaterial,
    PrintAnalysis,
    PrintAnalysisBatch,
)
from iic_booking.users.models.user import User
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.department import Department, DepartmentType
from iic_booking.communication.utils import booking_display_id_for_email

from .image_utils import get_equipment_image_storage_path, equipment_image_available

logger = logging.getLogger(__name__)


def _get_wallet_owner_display_name(user, cache: dict):
    """Supervisor / wallet owner label for UI. Read-only (no SubWallet/Department get_or_create).

    Used by booking list/detail serializers. Callers should pass the same ``cache`` dict (e.g.
    ``context['_wallet_owner_display_cache']``) so list endpoints do one lookup per distinct user.
    """
    if not user or not getattr(user, "pk", None):
        return None
    uid = user.pk
    if uid in cache:
        return cache[uid]
    from iic_booking.users.models.wallet import Wallet, WalletJoinRequest, WalletJoinRequestStatus

    result = None
    try:
        if user.user_type in {UserType.STUDENT, UserType.OTHER}:
            approved = (
                WalletJoinRequest.objects.filter(
                    student_id=uid,
                    status=WalletJoinRequestStatus.APPROVED,
                )
                .select_related("wallet__user")
                .first()
            )
            if approved and approved.wallet_id:
                w = approved.wallet
                owner = getattr(w, "user", None)
                if owner and owner.id != uid:
                    result = owner.name or owner.email
        # Faculty / individual student / external: wallet is always their own — no supervisor label.
    except Exception:
        result = None
    cache[uid] = result
    return result


def _equipment_image_url(obj, request=None, *, verify_storage=False):
    """
    Return the stable API proxy URL for the equipment image (streams from storage; does not expire).

    Clients should use this as img src (or getEquipmentImageUrl on the frontend) instead of raw storage URLs.

    By default we return the proxy whenever a DB path exists. Strict verify_storage used to hide
    images when S3 open() failed due to a media/ prefix mismatch even though the object existed —
    that looked like "images disappear after some time". The proxy still 404s if truly missing.
    """
    if not get_equipment_image_storage_path(obj):
        return None
    if verify_storage and not equipment_image_available(obj.image):
        return None
    try:
        try:
            path = reverse("equipment-image-proxy", kwargs={"pk": obj.equipment_id})
        except NoReverseMatch:
            # When URL patterns are namespaced under `app_name="api"`, reverse needs the namespace.
            path = reverse("api:equipment-image-proxy", kwargs={"pk": obj.equipment_id})
        if request:
            try:
                return request.build_absolute_uri(path)
            except Exception:
                # Relative path works with the Vite /api proxy and avoids None on host errors.
                return path
        return path
    except Exception as e:
        logger.warning(
            "Equipment image URL generation failed for equipment_id=%s: %s",
            getattr(obj, "equipment_id", None),
            e,
        )
        return None


class EquipmentCategorySerializer(serializers.ModelSerializer):
    """Serializer for EquipmentCategory (list/detail)."""

    class Meta:
        model = EquipmentCategory
        fields = ['id', 'name', 'code', 'description', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']


class EquipmentGroupSerializer(serializers.ModelSerializer):
    """Serializer for EquipmentGroup (list/detail)."""

    class Meta:
        model = EquipmentGroup
        fields = ['equipment_group_id', 'name', 'code', 'description', 'created_at', 'updated_at']
        read_only_fields = ['equipment_group_id', 'created_at', 'updated_at']


class EquipmentGroupQuotaSerializer(serializers.ModelSerializer):
    """Serializer for EquipmentGroupQuota (nested in group detail)."""
    quota_type_display = serializers.CharField(source='get_quota_type_display', read_only=True)

    class Meta:
        model = EquipmentGroupQuota
        fields = [
            'id', 'quota_type', 'quota_type_display',
            'internal_individual_quota_minutes', 'internal_faculty_quota_minutes',
            'external_individual_quota_minutes', 'external_faculty_quota_minutes',
            'is_enforced',
        ]
        read_only_fields = ['id']


class EquipmentGroupEquipmentSerializer(serializers.ModelSerializer):
    """Minimal equipment for display in group detail."""
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = Equipment
        fields = ['equipment_id', 'code', 'name', 'status', 'status_display']


class EquipmentGroupDetailSerializer(serializers.ModelSerializer):
    """EquipmentGroup with equipment list and quota configurations (Django admin change view)."""
    equipment = EquipmentGroupEquipmentSerializer(many=True, read_only=True)
    quotas = EquipmentGroupQuotaSerializer(many=True, read_only=True)

    class Meta:
        model = EquipmentGroup
        fields = [
            'equipment_group_id', 'name', 'code', 'description',
            'created_at', 'updated_at',
            'equipment', 'quotas',
        ]
        read_only_fields = ['equipment_group_id', 'created_at', 'updated_at', 'equipment', 'quotas']


class EquipmentOperatorSerializer(serializers.ModelSerializer):
    """Serializer for EquipmentOperator model."""
    
    operator_name = serializers.SerializerMethodField()
    operator_email = serializers.SerializerMethodField()
    operator_phone = serializers.SerializerMethodField()
    operator_profile_picture = serializers.SerializerMethodField()
    role_display = serializers.CharField(source="get_role_display", read_only=True)

    class Meta:
        model = EquipmentOperator
        fields = [
            'equipment_operator_id', 
            'operator', 
            'role',
            'role_display',
            'operator_name',
            'operator_email',
            'operator_phone',
            'operator_profile_picture',
            'created_at'
        ]
        read_only_fields = [
            'equipment_operator_id', 
            'operator_name',
            'operator_email',
            'operator_phone',
            'operator_profile_picture',
            'created_at'
        ]
    
    def get_operator_name(self, obj):
        """Return operator's name or email if name is not available."""
        if obj.operator:
            return obj.operator.name or obj.operator.email
        return None
    
    def get_operator_email(self, obj):
        """Return operator's email."""
        if obj.operator:
            return obj.operator.email
        return None
    
    def get_operator_phone(self, obj):
        """Return operator's phone number."""
        if obj.operator:
            return obj.operator.phone_number
        return None
    
    def get_operator_profile_picture(self, obj):
        """Return operator's stable profile-picture proxy URL (does not expire)."""
        if obj.operator:
            return obj.operator.get_profile_picture_url_or_none(request=self.context.get("request"))
        return None


class EquipmentManagerSerializer(serializers.ModelSerializer):
    """Serializer for EquipmentManager model."""
    
    manager_name = serializers.SerializerMethodField()
    manager_email = serializers.SerializerMethodField()
    manager_phone = serializers.SerializerMethodField()
    manager_profile_picture = serializers.SerializerMethodField()

    class Meta:
        model = EquipmentManager
        fields = [
            'equipment_manager_id', 
            'manager', 
            'manager_name',
            'manager_email',
            'manager_phone',
            'manager_profile_picture',
            'created_at'
        ]
        read_only_fields = [
            'equipment_manager_id', 
            'manager_name',
            'manager_email',
            'manager_phone',
            'manager_profile_picture',
            'created_at'
        ]
    
    def get_manager_name(self, obj):
        """Return manager's name or email if name is not available."""
        if obj.manager:
            return obj.manager.name or obj.manager.email
        return None
    
    def get_manager_email(self, obj):
        """Return manager's email."""
        if obj.manager:
            return obj.manager.email
        return None
    
    def get_manager_phone(self, obj):
        """Return manager's phone number."""
        if obj.manager:
            return obj.manager.phone_number
        return None
    
    def get_manager_profile_picture(self, obj):
        """Return manager's stable profile-picture proxy URL (does not expire)."""
        if obj.manager:
            return obj.manager.get_profile_picture_url_or_none(request=self.context.get("request"))
        return None


class EquipmentSpecificationSerializer(serializers.ModelSerializer):
    """Serializer for EquipmentSpecification model."""

    class Meta:
        model = EquipmentSpecification
        fields = ['equipment_specification_id', 'spec_key', 'spec_value', 'created_at']
        read_only_fields = ['equipment_specification_id', 'created_at']


class EquipmentAccessorySerializer(serializers.ModelSerializer):
    """Serializer for EquipmentAccessory model."""

    class Meta:
        model = EquipmentAccessory
        fields = [
            'equipment_accessory_id',
            'accessory_name',
            'is_optional',
            'quantity',
            'serial_number',
            'notes',
            'created_at',
        ]
        read_only_fields = ['equipment_accessory_id', 'created_at']


class EquipmentAdditionalAccessorySerializer(serializers.ModelSerializer):
    """Serializer for EquipmentAdditionalAccessory model."""

    class Meta:
        model = EquipmentAdditionalAccessory
        fields = [
            'equipment_additional_accessory_id',
            'additional_accessory_name',
            'additional_accessory_description',
            'is_optional',
            'created_at'
        ]
        read_only_fields = ['equipment_additional_accessory_id', 'created_at']


class DynamicInputFieldSerializer(serializers.ModelSerializer):
    """Serializer for DynamicInputField model."""

    class Meta:
        model = DynamicInputField
        fields = [
            'field_key',
            'field_label',
            'field_type',
            'is_required',
            'editing_required',
            'default_value',
            'options',
            'help_text',
            'source_element_field_key',
            'created_at',
            'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at']


class PrintMaterialSerializer(serializers.ModelSerializer):
    class Meta:
        model = PrintMaterial
        fields = [
            "id",
            "code",
            "name",
            "density_g_per_cm3",
            "price_per_gram",
            "user_type",
            "is_active",
            "display_order",
        ]


class PrintMaterialWriteSerializer(serializers.Serializer):
    id = serializers.IntegerField(required=False, allow_null=True)
    code = serializers.CharField(max_length=64)
    name = serializers.CharField(max_length=255)
    density_g_per_cm3 = serializers.DecimalField(max_digits=6, decimal_places=3, default=Decimal("1.240"))
    price_per_gram = serializers.DecimalField(max_digits=10, decimal_places=2)
    user_type = serializers.CharField(max_length=50, allow_blank=True, required=False, allow_null=True)
    is_active = serializers.BooleanField(default=True)
    display_order = serializers.IntegerField(default=0, required=False)


class PrintAnalysisSerializer(serializers.ModelSerializer):
    material_name = serializers.CharField(source="material.name", read_only=True, default="")
    stl_filename = serializers.CharField(source="original_filename", read_only=True)
    stl_download_url = serializers.SerializerMethodField()

    def get_stl_download_url(self, obj):
        """Return API download URL for the STL (served via Django)."""
        try:
            request = self.context.get("request") if hasattr(self, "context") else None
            if not request:
                # Fallback to relative URL; frontend will prefix API origin.
                return f"/api/print-analyses/{obj.id}/stl/"
            return request.build_absolute_uri(f"/api/print-analyses/{obj.id}/stl/")
        except Exception:
            return f"/api/print-analyses/{obj.id}/stl/"

    class Meta:
        model = PrintAnalysis
        fields = [
            "id",
            "batch_id",
            "sequence",
            "status",
            "analysis_method",
            "weight_grams",
            "actual_weight_grams",
            "volume_cm3",
            "estimated_time_minutes",
            "actual_time_minutes",
            "bounding_box",
            "warnings",
            "error_message",
            "slicer_settings",
            "material_code_snapshot",
            "price_per_gram_snapshot",
            "material_name",
            "stl_filename",
            "stl_download_url",
            "cancelled_at",
            "created_at",
            "updated_at",
        ]


class PrintAnalysisBatchSerializer(serializers.ModelSerializer):
    items = PrintAnalysisSerializer(many=True, read_only=True)
    total_weight_grams = serializers.SerializerMethodField()
    total_estimated_time_minutes = serializers.SerializerMethodField()
    material_code_snapshot = serializers.SerializerMethodField()

    def get_total_weight_grams(self, obj):
        from .print_3d_service import ceil_weight_grams

        total = 0
        for item in obj.items.filter(cancelled_at__isnull=True):
            if item.weight_grams is not None:
                total += int(ceil_weight_grams(item.weight_grams))
        return total

    def get_total_estimated_time_minutes(self, obj):
        total = 0
        for item in obj.items.filter(cancelled_at__isnull=True):
            if item.estimated_time_minutes is not None:
                total += int(item.estimated_time_minutes)
        return total

    def get_material_code_snapshot(self, obj):
        first = obj.items.filter(cancelled_at__isnull=True).order_by("sequence", "created_at").first()
        if not first:
            return ""
        return first.material_code_snapshot or (first.material.code if first.material else "")

    class Meta:
        model = PrintAnalysisBatch
        fields = [
            "id",
            "status",
            "original_filename",
            "slicer_settings",
            "error_message",
            "items",
            "total_weight_grams",
            "total_estimated_time_minutes",
            "material_code_snapshot",
            "created_at",
            "updated_at",
        ]


def _comments_input_field_schema():
    """Universal booking comments field shown after equipment-specific fields."""
    return {
        "field_key": "comments",
        "field_label": "Any Other Requirements:",
        "field_type": DynamicInputFieldType.TEXT,
        "is_required": False,
        "editing_required": True,
        "default_value": "",
        "options": [],
        "help_text": "Additional notes from user.",
        "source_element_field_key": None,
    }


class ChargeProfileSerializer(serializers.ModelSerializer):
    """Serializer for ChargeProfile model."""

    class Meta:
        model = ChargeProfile
        fields = [
            'equipment',
            'user_type',
            'is_active',
            'require_istem_fbr',
            'primary_unit_charge',
            'secondary_unit_charge',
            'breakpoint',
            'time_formula',
            'created_at',
            'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at']


class MultiParamDefinitionSerializer(serializers.ModelSerializer):
    """Serializer for MultiParamDefinition model."""

    class Meta:
        model = MultiParamDefinition
        fields = [
            'user_type',
            'param_name',
            'param_code',
            'unit_time_minutes',
            'unit_charge',
            'is_active',
            'created_at',
            'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at']


class SlotMasterSerializer(serializers.ModelSerializer):
    """Serializer for SlotMaster model."""

    class Meta:
        model = SlotMaster
        fields = [
            'slot_number',
            'slot_name',
            'open_time',
            'close_time',
            'is_active',
            'created_at',
            'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at']


class DailySlotSerializer(serializers.ModelSerializer):
    """Serializer for DailySlot model."""

    slot_number = serializers.SerializerMethodField()
    slot_name = serializers.SerializerMethodField()
    equipment_code = serializers.SerializerMethodField()
    booking_id = serializers.SerializerMethodField()
    real_booking_id = serializers.IntegerField(source='booking.booking_id', read_only=True, allow_null=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    booking_status = serializers.SerializerMethodField()
    booking_status_display = serializers.SerializerMethodField()
    booking_user_name = serializers.SerializerMethodField()
    booking_user_department_code = serializers.SerializerMethodField()
    booking_user_department_name = serializers.SerializerMethodField()
    booking_user_email = serializers.SerializerMethodField()
    booking_user_phone = serializers.SerializerMethodField()
    available_for_external = serializers.SerializerMethodField()
    # Wall-clock open time (HH:mm:ss) from SlotMaster — aligns weekly grid rows with slot_master_times (start_datetime is TZ-aware ISO).
    slot_open_time = serializers.SerializerMethodField()

    class Meta:
        model = DailySlot
        fields = [
            'id',
            'slot_master',
            'slot_number',
            'slot_name',
            'equipment_code',
            'date',
            'slot_open_time',
            'start_datetime',
            'end_datetime',
            'status',
            'status_display',
            'blocked_label',
            'reserved_for_external',
            'home_department_only',
            'booking',
            'booking_id',
            'real_booking_id',
            'booking_status',
            'booking_status_display',
            'booking_user_name',
            'booking_user_department_code',
            'booking_user_department_name',
            'booking_user_email',
            'booking_user_phone',
            'available_for_external',
            'created_at',
            'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_available_for_external(self, obj):
        """True only when slot is reserved for external and status is AVAILABLE."""
        return (
            getattr(obj, 'reserved_for_external', False)
            and obj.status == SlotStatus.AVAILABLE
        )

    def get_booking_id(self, obj):
        return booking_display_id_for_email(getattr(obj, "booking", None)) or None

    def get_slot_open_time(self, obj):
        sm = getattr(obj, "slot_master", None)
        if sm is not None and getattr(sm, "open_time", None) is not None:
            return sm.open_time.strftime("%H:%M:%S")
        return None

    def to_representation(self, instance):
        """External users: Sat/Sun/table holidays use the same status labels as internal (for calendar colours).
        Bookability for externals is only when reserved_for_external+AVAILABLE (available_for_external).
        Other weekdays: non-reserved slots show as Not Available.
        Internal users: on weekdays (not holiday/weekend), show 'Reserved for External User' when slot is AVAILABLE and reserved_for_external."""
        data = super().to_representation(instance)
        holidays_in_range = self.context.get('holidays_in_range') or set()
        if self.context.get('for_external_user'):
            ext_min = self.context.get('external_bookable_min_date')
            ext_max = self.context.get('external_bookable_max_date')
            # External view: never mask real non-AVAILABLE states (e.g. BOOKED) as NOT_AVAILABLE.
            # We only convert *AVAILABLE but not externally bookable* slots to NOT_AVAILABLE.
            if instance.status != SlotStatus.AVAILABLE or getattr(instance, 'booking_id', None):
                data['status'] = instance.status
                data['status_display'] = instance.get_status_display()
                data['available_for_external'] = False
                return data
            reserved_and_available = (
                getattr(instance, 'reserved_for_external', False)
                and instance.status == SlotStatus.AVAILABLE
            )
            # Rolling calendar window (today+15 … +21) greys out non-reserved days in a visible Mon–Sun week.
            # Admin-marked "Reserved for External" + AVAILABLE must still show as bookable on those dates.
            out_of_ext_rolling_window = (
                ext_min is not None and ext_max is not None and (
                    instance.date < ext_min or instance.date > ext_max
                )
            )
            if out_of_ext_rolling_window and not reserved_and_available:
                data['status'] = 'NOT_AVAILABLE'
                data['status_display'] = 'Not Available'
                data['available_for_external'] = False
            elif instance.date in holidays_in_range:
                # Same as weekdays: reserved+AVAILABLE must read as "Available" for external booking on Sat/Sun/holidays.
                ext_avail = self.get_available_for_external(instance)
                if ext_avail:
                    data['status'] = SlotStatus.AVAILABLE
                    data['status_display'] = 'Available'
                    data['available_for_external'] = True
                else:
                    data['status'] = instance.status
                    data['status_display'] = instance.get_status_display()
                    data['available_for_external'] = ext_avail
            elif getattr(instance, 'reserved_for_external', False) and instance.status == SlotStatus.AVAILABLE:
                data['status'] = SlotStatus.AVAILABLE
                data['status_display'] = 'Available'
                data['available_for_external'] = self.get_available_for_external(instance)
            else:
                data['status'] = 'NOT_AVAILABLE'
                data['status_display'] = 'Not Available'
                data['available_for_external'] = self.get_available_for_external(instance)
        else:
            # Internal user: on weekday (not in holidays_in_range), show "Reserved for External User" for AVAILABLE + reserved_for_external
            if (
                instance.status == SlotStatus.AVAILABLE
                and getattr(instance, 'reserved_for_external', False)
                and instance.date not in holidays_in_range
            ):
                data['status_display'] = 'Reserved for External User'
            elif (
                instance.status == SlotStatus.AVAILABLE
                and getattr(instance, 'home_department_only', False)
                and not getattr(instance, 'reserved_for_external', False)
            ):
                from .slot_department_access import non_home_reservation_released_to_all

                equipment = None
                try:
                    equipment = instance.slot_master.equipment if instance.slot_master_id else None
                except Exception:
                    equipment = None
                if equipment and non_home_reservation_released_to_all(instance, equipment):
                    data['status_display'] = 'Available (all departments)'
                else:
                    data['status_display'] = 'Reserved for other departments'
            elif (
                instance.status == SlotStatus.AVAILABLE
                and not getattr(instance, 'home_department_only', False)
                and not getattr(instance, 'reserved_for_external', False)
            ):
                from .slot_department_access import equipment_department_slot_policy_active

                equipment = None
                try:
                    equipment = instance.slot_master.equipment if instance.slot_master_id else None
                except Exception:
                    equipment = None
                policy_cache = self.context.setdefault("_dept_policy_active_by_eq", {})
                eid = getattr(equipment, "equipment_id", None) if equipment else None
                if eid is not None and eid not in policy_cache:
                    policy_cache[eid] = equipment_department_slot_policy_active(equipment)
                if eid is not None and policy_cache.get(eid):
                    data['status_display'] = 'Home department only'
        return data

    def _show_completed_as_booked_for_weekly(self, obj):
        """True if this slot should display as BOOKED in the weekly window (display only)."""
        if not self.context.get('for_weekly_display'):
            return False
        if not obj.booking_id or not obj.booking:
            return False
        if obj.booking.status != BookingStatus.COMPLETED:
            return False
        today = timezone.localdate()
        return obj.date > today

    def get_booking_status(self, obj):
        """Return booking status; for weekly display, show COMPLETED + future date as BOOKED."""
        if not obj.booking_id or not obj.booking:
            return None
        if self._show_completed_as_booked_for_weekly(obj):
            return BookingStatus.BOOKED
        return obj.booking.status

    def get_slot_number(self, obj):
        """Get slot number from slot master."""
        if obj.slot_master:
            return obj.slot_master.slot_number
        return None
    
    def get_slot_name(self, obj):
        """Get slot name from slot master."""
        if obj.slot_master:
            return obj.slot_master.slot_name
        return None
    
    def get_equipment_code(self, obj):
        """Get equipment code from slot master."""
        if obj.slot_master and obj.slot_master.equipment:
            return obj.slot_master.equipment.code
        return None
    
    def get_booking_status_display(self, obj):
        """Get human-readable booking status when slot has a booking. For weekly display, show COMPLETED + future date as 'Booked'."""
        if obj.booking_id and obj.booking:
            if self._show_completed_as_booked_for_weekly(obj):
                return "Booked"
            return obj.booking.get_status_display()
        return None

    def _booking_user(self, obj):
        booking = getattr(obj, "booking", None)
        if not booking:
            return None
        return getattr(booking, "user", None)

    def get_booking_user_name(self, obj):
        """
        Display name for the user who booked this slot.
        Used by the Lab operator/OIC dashboard weekly calendar to label BOOKED cells.
        """
        user = self._booking_user(obj)
        if not user:
            return None
        return getattr(user, "name", None) or getattr(user, "email", None) or None

    def get_booking_user_department_code(self, obj):
        """Department code of the user who booked this slot (second line on dashboard, when available)."""
        user = self._booking_user(obj)
        if not user:
            return None
        dept = getattr(user, "department", None)
        if not dept:
            return None
        code = getattr(dept, "code", None)
        code = str(code).strip() if code is not None else ""
        return code or None

    def get_booking_user_department_name(self, obj):
        """Department name of the user who booked this slot (staff hover details)."""
        user = self._booking_user(obj)
        if not user:
            return None
        dept = getattr(user, "department", None)
        if not dept:
            return None
        name = getattr(dept, "name", None)
        name = str(name).strip() if name is not None else ""
        return name or None

    def get_booking_user_email(self, obj):
        """Email of the booker — only for admin-panel staff (change-slot / OIC hover)."""
        if not self.context.get("include_booking_user_contact"):
            return None
        user = self._booking_user(obj)
        if not user:
            return None
        email = getattr(user, "email", None)
        email = str(email).strip() if email is not None else ""
        return email or None

    def get_booking_user_phone(self, obj):
        """Mobile/phone of the booker — only for admin-panel staff."""
        if not self.context.get("include_booking_user_contact"):
            return None
        user = self._booking_user(obj)
        if not user:
            return None
        phone = getattr(user, "phone_number", None)
        phone = str(phone).strip() if phone is not None else ""
        return phone or None


class EquipmentListSerializer(serializers.ModelSerializer):
    """Simplified serializer for listing equipment."""

    profile_type_display = serializers.CharField(source='get_profile_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    image_url = serializers.SerializerMethodField()
    video_url = serializers.SerializerMethodField()
    avg_rating = serializers.FloatField(read_only=True, allow_null=True)
    rating_count = serializers.IntegerField(read_only=True)
    rating_dist = serializers.SerializerMethodField()
    category_name = serializers.CharField(source='category.name', read_only=True, allow_null=True)
    category_code = serializers.CharField(source='category.code', read_only=True, allow_null=True)
    internal_department_name = serializers.CharField(source='internal_department.name', read_only=True, allow_null=True)
    internal_department_code = serializers.CharField(source='internal_department.code', read_only=True, allow_null=True)
    visibility_group_name = serializers.CharField(source='visibility_group.name', read_only=True, allow_null=True)
    equipment_group_id = serializers.IntegerField(source='equipment_group.equipment_group_id', read_only=True, allow_null=True)
    equipment_group_name = serializers.CharField(source='equipment_group.name', read_only=True, allow_null=True)
    equipment_group_code = serializers.CharField(source='equipment_group.code', read_only=True, allow_null=True)

    class Meta:
        model = Equipment
        fields = [
            'equipment_id',
            'code',
            'name',
            'description',
            'profile_type',
            'profile_type_display',
            'status',
            'status_display',
            'location',
            'image_url',
            'video_file',
            'video_url',
            'avg_rating',
            'rating_count',
            'rating_dist',
            'category',
            'category_name',
            'category_code',
            'internal_department',
            'internal_department_name',
            'internal_department_code',
            'visibility_group',
            'visibility_group_name',
            'equipment_group',
            'equipment_group_id',
            'equipment_group_name',
            'equipment_group_code',
            'created_at',
            'updated_at',
            'enable_charge_recalculation',
            'user_rating_enabled',
            'weekly_view_display',
            'weekly_view_time_from',
            'weekly_view_time_to',
            'weekly_view_max_rows',
            'weekly_view_default_days',
            'slot_window_reference_weekday',
            'slot_window_reference_time',
            'urgent_peak_window_minutes',
            'max_urgent_requests',
            'booking_not_utilize_window_hours',
            'operator_unavailable_after_booking_end_hours',
            'operator_absent_disruption_after_booking_end_hours',
            'make',
            'show_make_on_card',
            'model_information',
            'show_model_on_card',
        ]
        read_only_fields = ['equipment_id', 'created_at', 'updated_at']

    def get_image_url(self, obj):
        """Return stable proxy URL whenever a DB image path exists (no false-negative S3 hide)."""
        return _equipment_image_url(
            obj,
            request=self.context.get("request"),
            verify_storage=False,
        )
    
    def get_video_url(self, obj):
        """Get video URL from storage if available."""
        if obj.video_file:
            try:
                return default_storage.url(obj.video_file.name)
            except Exception:
                return None
        return None

    def get_rating_dist(self, obj):
        """
        Rating distribution counts for 1..5.
        Values are provided via queryset annotations (rating_1_count .. rating_5_count).
        """
        return {
            "1": int(getattr(obj, "rating_1_count", 0) or 0),
            "2": int(getattr(obj, "rating_2_count", 0) or 0),
            "3": int(getattr(obj, "rating_3_count", 0) or 0),
            "4": int(getattr(obj, "rating_4_count", 0) or 0),
            "5": int(getattr(obj, "rating_5_count", 0) or 0),
        }


class EquipmentListLiteSerializer(serializers.ModelSerializer):
    """
    Lightweight equipment list serializer for fast equipment catalog pages.

    Excludes rating aggregations (which require expensive joins/aggregations).
    """

    profile_type_display = serializers.CharField(source='get_profile_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    image_url = serializers.SerializerMethodField()
    video_url = serializers.SerializerMethodField()
    category_name = serializers.CharField(source='category.name', read_only=True, allow_null=True)
    category_code = serializers.CharField(source='category.code', read_only=True, allow_null=True)
    internal_department_name = serializers.CharField(source='internal_department.name', read_only=True, allow_null=True)
    internal_department_code = serializers.CharField(source='internal_department.code', read_only=True, allow_null=True)

    class Meta:
        model = Equipment
        fields = [
            'equipment_id',
            'code',
            'name',
            'profile_type',
            'profile_type_display',
            'status',
            'status_display',
            'location',
            'image_url',
            'video_file',
            'video_url',
            'category',
            'category_name',
            'category_code',
            'internal_department',
            'internal_department_name',
            'internal_department_code',
            'make',
            'show_make_on_card',
            'model_information',
            'show_model_on_card',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['equipment_id', 'created_at', 'updated_at']

    def get_image_url(self, obj):
        return _equipment_image_url(
            obj,
            request=self.context.get("request"),
            verify_storage=False,
        )

    def get_video_url(self, obj):
        if obj.video_file:
            try:
                return default_storage.url(obj.video_file.name)
            except Exception:
                return None
        return None


class EquipmentDetailSerializer(serializers.ModelSerializer):
    """Detailed serializer for Equipment model."""

    profile_type_display = serializers.CharField(source='get_profile_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    image_url = serializers.SerializerMethodField()
    video_url = serializers.SerializerMethodField()
    category_name = serializers.CharField(source='category.name', read_only=True, allow_null=True)
    category_code = serializers.CharField(source='category.code', read_only=True, allow_null=True)
    internal_department_name = serializers.CharField(source='internal_department.name', read_only=True, allow_null=True)
    internal_department_code = serializers.CharField(source='internal_department.code', read_only=True, allow_null=True)
    equipment_group_id = serializers.IntegerField(source='equipment_group.equipment_group_id', read_only=True, allow_null=True)
    equipment_group_name = serializers.CharField(source='equipment_group.name', read_only=True, allow_null=True)
    equipment_group_code = serializers.CharField(source='equipment_group.code', read_only=True, allow_null=True)
    visibility_group_name = serializers.CharField(source='visibility_group.name', read_only=True, allow_null=True)
    specifications = EquipmentSpecificationSerializer(many=True, read_only=True, source='equipment_specifications')
    accessories = EquipmentAccessorySerializer(many=True, read_only=True, source='equipment_accessories')
    additional_accessories = EquipmentAdditionalAccessorySerializer(
        many=True, read_only=True, source='equipment_additional_accessories'
    )
    input_fields = serializers.SerializerMethodField()
    charge_profiles = serializers.SerializerMethodField()
    slot_masters = SlotMasterSerializer(many=True, read_only=True)
    slot_options = MultiParamDefinitionSerializer(many=True, read_only=True, source='param_definitions')
    operators = EquipmentOperatorSerializer(many=True, read_only=True, source='equipment_operators')
    managers = EquipmentManagerSerializer(many=True, read_only=True, source='equipment_managers')
    base_charges_by_user_type = serializers.SerializerMethodField()
    print_materials = PrintMaterialSerializer(many=True, read_only=True)

    class Meta:
        model = Equipment
        fields = [
            'equipment_id',
            'code',
            'name',
            'description',
            'profile_type',
            'profile_type_display',
            'status',
            'status_display',
            'location',
            'important_instruction',
            'make',
            'show_make_on_card',
            'model_information',
            'show_model_on_card',
            'booking_email_extra_text',
            'completion_email_extra_text',
            'print_3d_stl_notification_email',
            'istem_portal_url',
            'istem_fbr_status_url',
            'image_url',
            'video_file',
            'video_url',
            'category',
            'category_name',
            'category_code',
            'internal_department',
            'internal_department_name',
            'internal_department_code',
            'equipment_group',
            'equipment_group_id',
            'equipment_group_name',
            'equipment_group_code',
            'visibility_group',
            'visibility_group_name',
            'reschedule_hours_threshold',
            'results_base_location',
            'specifications',
            'accessories',
            'charge_profiles',
            'slot_masters',
            'additional_accessories',
            'input_fields',
            'created_at',
            'updated_at',
            'slot_options',
            'operators',
            'managers',
            'base_charges_by_user_type',
            'slot_duration_minutes',
            'slots_per_day',
            'split_booking_enabled',
            'auto_slot_selection_default',
            'repeat_sample_request_days',
            'repeat_sample_disclaimer',
            'enable_charge_recalculation',
            'user_rating_enabled',
            'weekly_view_display',
            'weekly_view_time_from',
            'weekly_view_time_to',
            'weekly_view_max_rows',
            'weekly_view_default_days',
            'slot_window_reference_weekday',
            'slot_window_reference_time',
            'urgent_peak_window_minutes',
            'max_urgent_requests',
            'waitlist_queue_depth',
            'booking_not_utilize_window_hours',
            'operator_unavailable_after_booking_end_hours',
            'operator_absent_disruption_after_booking_end_hours',
            'print_materials',
        ]
        read_only_fields = ['equipment_id']

    def get_image_url(self, obj):
        """Return stable proxy URL for equipment image when the file exists in storage."""
        return _equipment_image_url(
            obj,
            request=self.context.get("request"),
            verify_storage=False,
        )
    
    def get_video_url(self, obj):
        """Get video URL from storage if available."""
        if obj.video_file:
            try:
                return default_storage.url(obj.video_file.name)
            except Exception:
                return None
        return None

    def get_input_fields(self, obj):
        fields = DynamicInputFieldSerializer(obj.input_fields.all().order_by("field_key"), many=True).data
        # Always include Any Other Requirements at the end for all equipment booking forms.
        fields.append(_comments_input_field_schema())
        return fields

    def get_base_charges_by_user_type(self, obj):
        """Return base charges per user type from active charge profiles.
        Exclude student and faculty only; show all other user types (e.g. R&D, Institute, External, Other).
        """
        choices_dict = dict(UserType.get_choices())
        profiles = obj.charge_profiles.filter(is_active=True).order_by('user_type')
        # Do not show charges for student and faculty
        profiles = profiles.exclude(user_type__in=[UserType.STUDENT, UserType.FACULTY])

        result = []
        for cp in profiles:
            result.append({
                'user_type': cp.user_type,
                'user_type_display': str(choices_dict.get(cp.user_type, cp.user_type)),
                'profile_type_display': obj.get_profile_type_display() if obj.profile_type else None,
                'primary_unit_charge': str(cp.primary_unit_charge),
            })
        return result

    def get_charge_profiles(self, obj):
        """
        Admin/booking UI should only edit/view the STANDARD charge profiles.
        Discounted profiles are applied via the user flag, and must not appear
        here to avoid accidental overriding of standard prices.
        """
        qs = obj.charge_profiles.filter(pricing_profile=ChargeProfilePricingProfile.STANDARD).order_by("user_type")
        serializer = ChargeProfileSerializer(qs, many=True, context=self.context)
        return serializer.data


# --- Admin write serializers (nested inlines for equipment create/update) ---

class EquipmentManagerWriteSerializer(serializers.Serializer):
    manager = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(
            user_type=UserType.MANAGER,
            is_active=True,
            department__department_type=DepartmentType.INTERNAL,
        ).order_by('name', 'email'),
        required=True,
    )


class EquipmentOperatorWriteSerializer(serializers.Serializer):
    operator = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(
            user_type=UserType.OPERATOR,
            is_active=True,
            department__department_type=DepartmentType.INTERNAL,
        ).order_by('name', 'email'),
        required=True,
    )
    role = serializers.ChoiceField(
        choices=getattr(EquipmentOperator, "Role").choices,
        required=False,
        default=getattr(EquipmentOperator, "Role").PRIMARY,
    )


class EquipmentSpecificationWriteSerializer(serializers.Serializer):
    spec_key = serializers.CharField(max_length=255)
    spec_value = serializers.CharField(allow_blank=True, required=False, default='')


class EquipmentAccessoryWriteSerializer(serializers.Serializer):
    accessory_name = serializers.CharField(max_length=255)
    is_optional = serializers.BooleanField(default=False)
    quantity = serializers.IntegerField(required=False, default=1, min_value=1)
    serial_number = serializers.CharField(max_length=120, allow_blank=True, required=False, default='')
    notes = serializers.CharField(allow_blank=True, required=False, default='')


class EquipmentAdditionalAccessoryWriteSerializer(serializers.Serializer):
    additional_accessory_name = serializers.CharField(max_length=255)
    additional_accessory_description = serializers.CharField(allow_blank=True, required=False, default='')
    is_optional = serializers.BooleanField(default=False)


class DynamicInputFieldWriteSerializer(serializers.Serializer):
    field_key = serializers.CharField(max_length=1)
    field_label = serializers.CharField(max_length=255)
    field_type = serializers.CharField(max_length=20)
    is_required = serializers.BooleanField(default=False)
    default_value = serializers.CharField(max_length=500, allow_blank=True, required=False, default='')
    options = serializers.ListField(child=serializers.CharField(), allow_empty=True, required=False, default=list)
    help_text = serializers.CharField(allow_blank=True, required=False, default='')
    source_element_field_key = serializers.CharField(max_length=1, allow_blank=True, required=False, default=None)


class ChargeProfileWriteSerializer(serializers.Serializer):
    user_type = serializers.CharField(max_length=50)
    is_active = serializers.BooleanField(default=True)
    require_istem_fbr = serializers.BooleanField(default=False, required=False)
    primary_unit_charge = serializers.DecimalField(max_digits=10, decimal_places=2)
    secondary_unit_charge = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, default=0)
    breakpoint = serializers.DecimalField(max_digits=10, decimal_places=2, allow_null=True, required=False)
    time_formula = serializers.CharField(max_length=500, allow_blank=True, allow_null=True, required=False)


class SlotMasterWriteSerializer(serializers.Serializer):
    slot_number = serializers.IntegerField()
    slot_name = serializers.CharField(max_length=100, allow_blank=True, required=False, default='')
    open_time = serializers.TimeField()
    close_time = serializers.TimeField()
    is_active = serializers.BooleanField(default=True)


class EquipmentAdminWriteSerializer(serializers.ModelSerializer):
    equipment_managers = EquipmentManagerWriteSerializer(many=True, required=False, default=list)
    equipment_operators = EquipmentOperatorWriteSerializer(many=True, required=False, default=list)
    equipment_specifications = EquipmentSpecificationWriteSerializer(many=True, required=False, default=list)
    equipment_accessories = EquipmentAccessoryWriteSerializer(many=True, required=False, default=list)
    equipment_additional_accessories = EquipmentAdditionalAccessoryWriteSerializer(many=True, required=False, default=list)
    input_fields = DynamicInputFieldWriteSerializer(many=True, required=False, default=list)
    charge_profiles = ChargeProfileWriteSerializer(many=True, required=False, default=list)
    slot_masters = SlotMasterWriteSerializer(many=True, required=False, default=list)
    print_materials = PrintMaterialWriteSerializer(many=True, required=False, default=list)
    internal_department = serializers.PrimaryKeyRelatedField(
        queryset=Department.objects.filter(department_type=DepartmentType.INTERNAL).order_by("name"),
        allow_null=True,
        required=False,
    )

    class Meta:
        model = Equipment
        fields = [
            'name', 'code', 'description', 'status', 'location', 'important_instruction',
            'make', 'show_make_on_card', 'model_information', 'show_model_on_card',
            'booking_email_extra_text', 'completion_email_extra_text', 'print_3d_stl_notification_email',
            'istem_portal_url', 'istem_fbr_status_url',
            'profile_type', 'category', 'internal_department', 'visibility_group',
            'equipment_group', 'slot_duration_minutes', 'slots_per_day',
            'reschedule_hours_threshold', 'results_base_location', 'split_booking_enabled', 'auto_slot_selection_default', 'weekly_view_display',
            'weekly_view_time_from', 'weekly_view_time_to', 'weekly_view_max_rows', 'weekly_view_default_days',
            'slot_window_reference_weekday', 'slot_window_reference_time',
            'urgent_peak_window_minutes',
            'max_urgent_requests',
            'waitlist_queue_depth',
            'booking_not_utilize_window_hours',
            'operator_unavailable_after_booking_end_hours',
            'operator_absent_disruption_after_booking_end_hours',
            'repeat_sample_request_days', 'repeat_sample_disclaimer',
            'equipment_managers', 'equipment_operators',
            'equipment_specifications', 'equipment_accessories',
            'equipment_additional_accessories', 'input_fields',
            'charge_profiles', 'slot_masters', 'print_materials',
        ]

    def validate_internal_department(self, value):
        from iic_booking.users.models.department import DepartmentType

        if value is None:
            return value
        if getattr(value, "department_type", None) != DepartmentType.INTERNAL:
            raise serializers.ValidationError(
                "Internal Department must be a department with type Internal."
            )
        return value

    def validate(self, attrs):
        from iic_booking.users.models.department import DepartmentType
        from iic_booking.users.models.user import User

        attrs = super().validate(attrs)
        managers = attrs.get("equipment_managers")
        operators = attrs.get("equipment_operators")
        if managers is not None:
            for item in managers:
                mgr = item.get("manager") if isinstance(item, dict) else None
                if mgr is None:
                    continue
                user = mgr if isinstance(mgr, User) else User.objects.filter(pk=mgr).select_related("department").first()
                if not user or getattr(getattr(user, "department", None), "department_type", None) != DepartmentType.INTERNAL:
                    raise serializers.ValidationError({
                        "equipment_managers": (
                            "Only Officer In Charge users belonging to an Internal department can be assigned."
                        )
                    })
        if operators is not None:
            for item in operators:
                op = item.get("operator") if isinstance(item, dict) else None
                if op is None:
                    continue
                user = op if isinstance(op, User) else User.objects.filter(pk=op).select_related("department").first()
                if not user or getattr(getattr(user, "department", None), "department_type", None) != DepartmentType.INTERNAL:
                    raise serializers.ValidationError({
                        "equipment_operators": (
                            "Only Lab Incharge users belonging to an Internal department can be assigned."
                        )
                    })
        return attrs

    def create(self, validated_data):
        from django.db import transaction
        inlines = {k: validated_data.pop(k, []) for k in [
            'equipment_managers', 'equipment_operators', 'equipment_specifications',
            'equipment_accessories', 'equipment_additional_accessories',
            'input_fields', 'charge_profiles', 'slot_masters', 'print_materials',
        ]}
        with transaction.atomic():
            equipment = Equipment.objects.create(**validated_data)
            _create_related(equipment, inlines)
        return equipment

    def update(self, instance, validated_data):
        from django.db import transaction
        inlines = {k: validated_data.pop(k, None) for k in [
            'equipment_managers', 'equipment_operators', 'equipment_specifications',
            'equipment_accessories', 'equipment_additional_accessories',
            'input_fields', 'charge_profiles', 'slot_masters', 'print_materials',
        ]}
        with transaction.atomic():
            for attr, value in validated_data.items():
                setattr(instance, attr, value)
            instance.save()
            _sync_related(instance, inlines)
        return instance


def _create_related(equipment, inlines):
    from .models import (
        EquipmentManager, EquipmentOperator, EquipmentSpecification,
        EquipmentAccessory, EquipmentAdditionalAccessory, DynamicInputField,
        ChargeProfile, SlotMaster, PrintMaterial,
    )
    for item in inlines.get('equipment_managers', []):
        EquipmentManager.objects.create(equipment=equipment, manager=item['manager'])
    for item in inlines.get('equipment_operators', []):
        EquipmentOperator.objects.create(
            equipment=equipment,
            operator=item['operator'],
            role=item.get("role") or EquipmentOperator.Role.PRIMARY,
        )
    for item in inlines.get('equipment_specifications', []):
        EquipmentSpecification.objects.create(equipment=equipment, spec_key=item['spec_key'], spec_value=item.get('spec_value', ''))
    for item in inlines.get('equipment_accessories', []):
        EquipmentAccessory.objects.create(
            equipment=equipment,
            accessory_name=item['accessory_name'],
            is_optional=item.get('is_optional', False),
            quantity=item.get('quantity', 1) or 1,
            serial_number=(item.get('serial_number') or '').strip(),
            notes=(item.get('notes') or '').strip(),
        )
    for item in inlines.get('equipment_additional_accessories', []):
        EquipmentAdditionalAccessory.objects.create(
            equipment=equipment,
            additional_accessory_name=item['additional_accessory_name'],
            additional_accessory_description=item.get('additional_accessory_description', '') or '',
            is_optional=item.get('is_optional', False),
        )
    for item in inlines.get('input_fields', []):
        DynamicInputField.objects.create(
            equipment=equipment, field_key=item['field_key'], field_label=item['field_label'],
            field_type=item['field_type'], is_required=item.get('is_required', False),
            default_value=item.get('default_value') or '', options=item.get('options', []) or [],
            help_text=item.get('help_text') or '',
            source_element_field_key=(item.get('source_element_field_key') or '').strip() or None,
        )
    for item in inlines.get('charge_profiles', []):
        ChargeProfile.objects.create(
            equipment=equipment,
            user_type=item['user_type'],
            pricing_profile=ChargeProfilePricingProfile.STANDARD,
            is_active=item.get('is_active', True),
            require_istem_fbr=item.get('require_istem_fbr', False),
            primary_unit_charge=item['primary_unit_charge'], secondary_unit_charge=item.get('secondary_unit_charge', 0),
            breakpoint=item.get('breakpoint'), time_formula=item.get('time_formula') or '',
        )
        # Create the discounted variant (always zero charges) for every equipment/user type.
        ChargeProfile.objects.create(
            equipment=equipment,
            user_type=item['user_type'],
            pricing_profile=ChargeProfilePricingProfile.DISCOUNTED,
            is_active=True,
            require_istem_fbr=item.get('require_istem_fbr', False),
            primary_unit_charge=0,
            secondary_unit_charge=0,
            breakpoint=item.get('breakpoint'),
            time_formula=item.get('time_formula') or '',
        )
    for item in inlines.get('slot_masters', []):
        SlotMaster.objects.create(
            equipment=equipment, slot_number=item['slot_number'], slot_name=item.get('slot_name') or '',
            open_time=item['open_time'], close_time=item['close_time'], is_active=item.get('is_active', True),
        )
    for item in inlines.get('print_materials', []):
        PrintMaterial.objects.create(
            equipment=equipment,
            code=item['code'],
            name=item['name'],
            density_g_per_cm3=item.get('density_g_per_cm3', Decimal('1.240')),
            price_per_gram=item['price_per_gram'],
            user_type=(item.get('user_type') or '').strip() or None,
            is_active=item.get('is_active', True),
            display_order=item.get('display_order', 0) or 0,
        )


def _sync_related(equipment, inlines):
    from .models import (
        EquipmentManager, EquipmentOperator, EquipmentSpecification,
        EquipmentAccessory, EquipmentAdditionalAccessory, DynamicInputField,
        ChargeProfile, SlotMaster, PrintMaterial,
    )
    if inlines.get('equipment_managers') is not None:
        EquipmentManager.objects.filter(equipment=equipment).delete()
        for item in inlines['equipment_managers']:
            EquipmentManager.objects.create(equipment=equipment, manager=item['manager'])
    if inlines.get('equipment_operators') is not None:
        EquipmentOperator.objects.filter(equipment=equipment).delete()
        for item in inlines['equipment_operators']:
            EquipmentOperator.objects.create(
                equipment=equipment,
                operator=item['operator'],
                role=item.get("role") or EquipmentOperator.Role.PRIMARY,
            )
    if inlines.get('equipment_specifications') is not None:
        EquipmentSpecification.objects.filter(equipment=equipment).delete()
        for item in inlines['equipment_specifications']:
            EquipmentSpecification.objects.create(equipment=equipment, spec_key=item['spec_key'], spec_value=item.get('spec_value', ''))
    if inlines.get('equipment_accessories') is not None:
        EquipmentAccessory.objects.filter(equipment=equipment).delete()
        for item in inlines['equipment_accessories']:
            EquipmentAccessory.objects.create(
                equipment=equipment,
                accessory_name=item['accessory_name'],
                is_optional=item.get('is_optional', False),
                quantity=item.get('quantity', 1) or 1,
                serial_number=(item.get('serial_number') or '').strip(),
                notes=(item.get('notes') or '').strip(),
            )
    if inlines.get('equipment_additional_accessories') is not None:
        EquipmentAdditionalAccessory.objects.filter(equipment=equipment).delete()
        for item in inlines['equipment_additional_accessories']:
            EquipmentAdditionalAccessory.objects.create(
                equipment=equipment,
                additional_accessory_name=item['additional_accessory_name'],
                additional_accessory_description=item.get('additional_accessory_description', '') or '',
                is_optional=item.get('is_optional', False),
            )
    if inlines.get('input_fields') is not None:
        DynamicInputField.objects.filter(equipment=equipment).delete()
        for item in inlines['input_fields']:
            DynamicInputField.objects.create(
                equipment=equipment, field_key=item['field_key'], field_label=item['field_label'],
                field_type=item['field_type'], is_required=item.get('is_required', False),
                default_value=item.get('default_value') or '', options=item.get('options', []) or [],
                help_text=item.get('help_text') or '',
                source_element_field_key=(item.get('source_element_field_key') or '').strip() or None,
            )
    if inlines.get('charge_profiles') is not None:
        from django.db.models import Count
        payload_user_types = {item['user_type'] for item in inlines['charge_profiles']}
        existing = ChargeProfile.objects.filter(equipment=equipment).annotate(
            booking_count=Count('bookings'),
        )
        for item in inlines['charge_profiles']:
            user_type = item['user_type']
            # STANDARD (editable by equipment admin UI)
            profile, created = ChargeProfile.objects.get_or_create(
                equipment=equipment,
                user_type=user_type,
                pricing_profile=ChargeProfilePricingProfile.STANDARD,
                defaults={
                    'is_active': item.get('is_active', True),
                    'require_istem_fbr': item.get('require_istem_fbr', False),
                    'primary_unit_charge': item['primary_unit_charge'],
                    'secondary_unit_charge': item.get('secondary_unit_charge', 0),
                    'breakpoint': item.get('breakpoint'),
                    'time_formula': item.get('time_formula') or '',
                },
            )
            if not created:
                profile.is_active = item.get('is_active', True)
                profile.require_istem_fbr = item.get('require_istem_fbr', False)
                profile.primary_unit_charge = item['primary_unit_charge']
                profile.secondary_unit_charge = item.get('secondary_unit_charge', 0)
                profile.breakpoint = item.get('breakpoint')
                profile.time_formula = item.get('time_formula') or ''
                profile.save()

            # DISCOUNTED (always zero charges; time formula/breakpoint synced from STANDARD)
            discounted_profile, discounted_created = ChargeProfile.objects.get_or_create(
                equipment=equipment,
                user_type=user_type,
                pricing_profile=ChargeProfilePricingProfile.DISCOUNTED,
                defaults={
                    'is_active': True,
                    'require_istem_fbr': item.get('require_istem_fbr', False),
                    'primary_unit_charge': 0,
                    'secondary_unit_charge': 0,
                    'breakpoint': item.get('breakpoint'),
                    'time_formula': item.get('time_formula') or '',
                },
            )
            if not discounted_created:
                discounted_profile.is_active = True
                discounted_profile.require_istem_fbr = item.get('require_istem_fbr', False)
                discounted_profile.primary_unit_charge = 0
                discounted_profile.secondary_unit_charge = 0
                discounted_profile.breakpoint = item.get('breakpoint')
                discounted_profile.time_formula = item.get('time_formula') or ''
                discounted_profile.save()
        for profile in existing:
            if profile.user_type not in payload_user_types and profile.booking_count == 0:
                profile.delete()
    if inlines.get('slot_masters') is not None:
        SlotMaster.objects.filter(equipment=equipment).delete()
        for item in inlines['slot_masters']:
            SlotMaster.objects.create(
                equipment=equipment, slot_number=item['slot_number'], slot_name=item.get('slot_name') or '',
                open_time=item['open_time'], close_time=item['close_time'], is_active=item.get('is_active', True),
            )
    if inlines.get('print_materials') is not None:
        PrintMaterial.objects.filter(equipment=equipment).delete()
        for item in inlines['print_materials']:
            PrintMaterial.objects.create(
                equipment=equipment,
                code=item['code'],
                name=item['name'],
                density_g_per_cm3=item.get('density_g_per_cm3', Decimal('1.240')),
                price_per_gram=item['price_per_gram'],
                user_type=(item.get('user_type') or '').strip() or None,
                is_active=item.get('is_active', True),
                display_order=item.get('display_order', 0) or 0,
            )


def _booking_breakdown_suffix_start_index(stored: list) -> int:
    """
    Index of the first line that must follow the engine-calculated core (GST, discount, repeat discount).
    """
    for i, line in enumerate(stored):
        desc = (line.get("description") or "").strip()
        if desc.startswith("GST"):
            return i
        if desc == "Coupon":  # Backward compatibility with historical bookings.
            return i
        if desc.startswith("Repeat of"):
            return i
        if desc.lower().startswith("repeat sample"):
            return i
    return len(stored)


class BookingSerializer(serializers.ModelSerializer):
    """Serializer for Booking model. Shows 'Booked' for PENDING (user-facing)."""
    
    booking_id = serializers.SerializerMethodField()
    real_booking_id = serializers.IntegerField(source='booking_id', read_only=True)
    equipment_code = serializers.CharField(source='equipment.code', read_only=True)
    equipment_name = serializers.CharField(source='equipment.name', read_only=True)
    equipment_reschedule_hours_threshold = serializers.IntegerField(source='equipment.reschedule_hours_threshold', read_only=True)
    equipment_repeat_sample_request_days = serializers.IntegerField(source='equipment.repeat_sample_request_days', read_only=True, allow_null=True)
    equipment_repeat_sample_disclaimer = serializers.CharField(source='equipment.repeat_sample_disclaimer', read_only=True, allow_blank=True)
    equipment_enable_charge_recalculation = serializers.BooleanField(source='equipment.enable_charge_recalculation', read_only=True, default=False)
    equipment_user_rating_enabled = serializers.BooleanField(source='equipment.user_rating_enabled', read_only=True, default=True)
    equipment_booking_not_utilize_window_hours = serializers.IntegerField(
        source='equipment.booking_not_utilize_window_hours', read_only=True, default=24
    )
    equipment_operator_unavailable_after_booking_end_hours = serializers.IntegerField(
        source='equipment.operator_unavailable_after_booking_end_hours', read_only=True, default=24
    )
    equipment_profile_type = serializers.CharField(source='equipment.profile_type', read_only=True)
    equipment_profile_type_display = serializers.CharField(
        source='equipment.get_profile_type_display', read_only=True
    )
    user_email = serializers.CharField(source='user.email', read_only=True)
    user_name = serializers.SerializerMethodField()
    user_phone = serializers.SerializerMethodField()
    user_department = serializers.SerializerMethodField()
    user_profile_picture = serializers.SerializerMethodField()
    status_display = serializers.SerializerMethodField()
    total_hours = serializers.SerializerMethodField()
    start_time = serializers.SerializerMethodField()
    end_time = serializers.SerializerMethodField()
    daily_slots = DailySlotSerializer(many=True, read_only=True)
    equipment_weekly_view_display = serializers.SerializerMethodField()
    input_fields = serializers.SerializerMethodField()
    editable_input_fields = serializers.SerializerMethodField()
    all_input_fields = serializers.SerializerMethodField()
    user_type_snapshot_display = serializers.SerializerMethodField()
    wallet_owner_name = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    sample_trace = serializers.SerializerMethodField()
    repeat_sample_request_status = serializers.SerializerMethodField()
    repeat_booking_already_created = serializers.SerializerMethodField()
    accounts_in_charge = serializers.SerializerMethodField()
    lab_in_charge = serializers.SerializerMethodField()
    oic_contacts = serializers.SerializerMethodField()
    charge_breakdown = serializers.SerializerMethodField()
    istem_fbr_status_display = serializers.SerializerMethodField()
    istem_portal_url = serializers.SerializerMethodField()
    istem_fbr_status_url = serializers.SerializerMethodField()
    require_istem_fbr = serializers.SerializerMethodField()
    settlement_department_name = serializers.SerializerMethodField()
    print_analysis = PrintAnalysisSerializer(read_only=True)
    print_analysis_batch = PrintAnalysisBatchSerializer(read_only=True)
    print_analyses = serializers.SerializerMethodField()

    def get_print_analyses(self, obj):
        qs = PrintAnalysis.objects.filter(booking=obj, cancelled_at__isnull=True).order_by(
            "sequence", "created_at"
        )
        if not qs.exists() and getattr(obj, "print_analysis_batch_id", None):
            qs = PrintAnalysis.objects.filter(
                batch_id=obj.print_analysis_batch_id,
                cancelled_at__isnull=True,
            ).order_by("sequence", "created_at")
        if not qs.exists() and getattr(obj, "print_analysis_id", None):
            qs = PrintAnalysis.objects.filter(pk=obj.print_analysis_id)
        return PrintAnalysisSerializer(qs, many=True, context=self.context).data

    def get_settlement_department_name(self, obj):
        dept = getattr(obj, "settlement_department", None)
        return dept.name if dept else None

    def _get_input_fields_cache(self):
        return self.context.setdefault("_booking_input_fields_cache", {})

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        instances = self.instance
        if instances is None:
            return
        if isinstance(instances, QuerySet):
            booking_ids = list(instances.values_list("booking_id", flat=True))
        elif isinstance(instances, (list, tuple)):
            booking_ids = [getattr(b, "booking_id", None) for b in instances if getattr(b, "booking_id", None) is not None]
        else:
            booking_id = getattr(instances, "booking_id", None)
            booking_ids = [booking_id] if booking_id is not None else []
        if not booking_ids:
            return
        repeated_source_ids = set(
            Booking.objects.filter(source_booking_id__in=booking_ids)
            .values_list("source_booking_id", flat=True)
        )
        self.context["_repeat_booking_source_id_set"] = repeated_source_ids

    def _build_input_field_item(self, f):
        item = {
            'field_key': f.field_key,
            'field_label': f.field_label,
            'field_type': f.field_type or '',
            'editing_required': getattr(f, 'editing_required', False),
            'help_text': (f.help_text or '').strip() or None,
            'source_element_field_key': (f.source_element_field_key or '').strip() or None,
        }
        if f.options:
            item['options'] = f.options
        return item

    class Meta:
        model = Booking
        fields = [
            'booking_id',
            'real_booking_id',
            'virtual_booking_id',
            'user',
            'user_email',
            'user_name',
            'user_phone',
            'user_department',
            'user_profile_picture',
            'equipment',
            'equipment_code',
            'equipment_name',
            'equipment_reschedule_hours_threshold',
            'equipment_repeat_sample_request_days',
            'equipment_repeat_sample_disclaimer',
            'equipment_enable_charge_recalculation',
            'equipment_user_rating_enabled',
            'equipment_booking_not_utilize_window_hours',
            'equipment_operator_unavailable_after_booking_end_hours',
            'equipment_profile_type',
            'equipment_profile_type_display',
            'charge_profile',
            'user_type_snapshot',
            'total_time_minutes',
            'total_hours',
            'total_charge',
            'wallet_amount_applied',
            'amount_due',
            'payment_settled_at',
            'settlement_department',
            'settlement_department_name',
            'input_values',
            'input_fields',
            'editable_input_fields',
            'all_input_fields',
            'selected_parameters',
            'charge_breakdown',
            'status',
            'status_display',
            'notes',
            'sample_return_after_analysis',
            'return_shipping_fee_amount',
            'return_shipping_company',
            'return_shipping_tracking_id',
            'return_shipping_tracking_updated_at',
            'start_time',
            'end_time',
            'daily_slots',
            'equipment_weekly_view_display',
            'user_type_snapshot_display',
            'wallet_owner_name',
            'created_by_name',
            'sample_trace',
            'repeat_sample_request_status',
            'charge_recalculation_pending_amount',
            'created_at',
            'updated_at',
            'completed_at',
            'rating_on_time_operator_availability',
            'rating_laboratory_cleanliness_organization',
            'rating_sample_handling_care',
            'rating_operator_behaviour_professionalism',
            'rating_compliance_booking_request_parameters',
            'rating',
            'rating_feedback',
            'rated_at',
            'repeat_sample_enabled',
            'source_booking_id',
            'repeat_booking_already_created',
            'accounts_in_charge',
            'lab_in_charge',
            'oic_contacts',
            'istem_fbr_number',
            'istem_fbr_status',
            'istem_fbr_status_display',
            'istem_fbr_invalid_reason',
            'istem_fbr_executed_at',
            'istem_portal_url',
            'istem_fbr_status_url',
            'require_istem_fbr',
            'print_analysis',
            'print_analysis_batch',
            'print_analyses',
        ]
        read_only_fields = [
            'booking_id',
            'real_booking_id',
            'virtual_booking_id',
            'created_at',
            'updated_at',
            'completed_at',
            'rating_on_time_operator_availability',
            'rating_laboratory_cleanliness_organization',
            'rating_sample_handling_care',
            'rating_operator_behaviour_professionalism',
            'rating_compliance_booking_request_parameters',
            'rating',
            'rating_feedback',
            'rated_at',
            'istem_fbr_number',
            'istem_fbr_status',
            'istem_fbr_status_display',
            'istem_fbr_invalid_reason',
            'istem_fbr_executed_at',
        ]

    def get_istem_fbr_status_display(self, obj):
        st = getattr(obj, "istem_fbr_status", None)
        if not st:
            return None
        return dict(IstemFbrStatus.choices).get(st, st)

    def get_istem_portal_url(self, obj):
        from .models import get_equipment_istem_portal_url
        equipment = getattr(obj, "equipment", None)
        return get_equipment_istem_portal_url(equipment) if equipment else "https://www.istem.gov.in/"

    def get_istem_fbr_status_url(self, obj):
        from .models import get_equipment_istem_fbr_status_url
        equipment = getattr(obj, "equipment", None)
        return get_equipment_istem_fbr_status_url(equipment) if equipment else ""

    def get_require_istem_fbr(self, obj):
        from .models import charge_profile_requires_istem_fbr
        return charge_profile_requires_istem_fbr(getattr(obj, "charge_profile", None))

    def get_booking_id(self, obj):
        return booking_display_id_for_email(obj)
    
    def get_charge_breakdown(self, obj):
        """
        Re-run the charge engine for display so breakdown text stays in sync with calculators
        (stored JSON is a snapshot at booking time). GST / discount / repeat lines are kept from storage.
        """
        stored = obj.charge_breakdown or []
        if not stored:
            return stored
        idx = _booking_breakdown_suffix_start_index(stored)
        suffix = stored[idx:]
        try:
            cp = obj.charge_profile
            eq = obj.equipment
            if not cp or not eq:
                return stored

            class _ChargeProfileProxy:
                def __init__(self, profile, equipment):
                    self.equipment = profile.equipment
                    self.user_type = profile.user_type
                    self.is_active = profile.is_active
                    self.primary_unit_charge = profile.primary_unit_charge
                    self.secondary_unit_charge = profile.secondary_unit_charge
                    self.breakpoint = profile.breakpoint
                    self.time_formula = profile.time_formula
                    self.pricing_profile = getattr(profile, "pricing_profile", ChargeProfilePricingProfile.STANDARD)
                    self.profile_type = getattr(equipment, "profile_type", None)

            safe_inputs = build_safe_input_values_for_charge_calculation(obj.input_values)
            from .print_3d_views import apply_print_analysis_to_input_values

            if getattr(eq, "profile_type", None) == EquipmentProfileType.PRINT_3D:
                safe_inputs = apply_print_analysis_to_input_values(obj, safe_inputs)
            proxy = _ChargeProfileProxy(cp, eq)
            _, fresh_core = ChargeCalculationEngine.calculate_charge(
                proxy,
                safe_inputs,
                int(obj.total_time_minutes or 0),
                selected_parameters=obj.selected_parameters,
            )
            refreshed = [
                {"description": line["description"], "amount": float(line["amount"])}
                for line in fresh_core
            ]
            return refreshed + suffix
        except Exception:
            logger.exception("Failed to refresh charge_breakdown for booking %s", getattr(obj, "booking_id", obj))
            return stored

    def get_repeat_sample_request_status(self, obj):
        """Latest repeat sample request status for this booking, if any."""
        prefetched = getattr(obj, "_prefetched_objects_cache", {}).get("repeat_sample_requests")
        if prefetched is not None:
            latest = max(prefetched, key=lambda r: r.requested_at) if prefetched else None
        else:
            latest = obj.repeat_sample_requests.order_by('-requested_at').first()
        return latest.status if latest else None

    def get_repeat_booking_already_created(self, obj):
        """True if a repeat booking has already been created from this booking (enable repeat sample is then permanently disabled)."""
        prefetched_set = self.context.get("_repeat_booking_source_id_set")
        if prefetched_set is not None:
            return obj.booking_id in prefetched_set
        cache = self.context.setdefault("_repeat_booking_exists_cache", {})
        if obj.booking_id in cache:
            return cache[obj.booking_id]
        exists = Booking.objects.filter(source_booking_id=obj.booking_id).exists()
        cache[obj.booking_id] = exists
        return exists

    def get_accounts_in_charge(self, obj):
        """
        Return Accounts In Charge contact details.
        Heuristic: finance user(s) in the equipment's internal_department; fallback to any finance user.
        """
        try:
            from iic_booking.users.models.user_type import UserType
            equipment = getattr(obj, "equipment", None)
            dept_id = getattr(getattr(equipment, "internal_department", None), "id", None)
            cache = self.context.setdefault("_accounts_in_charge_cache", {})
            if dept_id in cache:
                return cache[dept_id]
            qs = User.objects.filter(user_type=UserType.FINANCE)
            if dept_id:
                qs = qs.filter(department_id=dept_id)
            u = qs.order_by("id").first() or User.objects.filter(user_type=UserType.FINANCE).order_by("id").first()
            if not u:
                cache[dept_id] = None
                return None
            payload = {
                "user_id": u.id,
                "name": u.name or u.email,
                "email": u.email,
                "phone": u.phone_number,
                "user_type": "finance",
            }
            cache[dept_id] = payload
            return payload
        except Exception:
            return None

    def get_lab_in_charge(self, obj):
        """
        Return Lab Incharge contact details (EquipmentOperator preferred).
        """
        try:
            equipment = getattr(obj, "equipment", None)
            if not equipment:
                return None
            cache = self.context.setdefault("_lab_in_charge_cache", {})
            eq_id = getattr(equipment, "equipment_id", None)
            if eq_id in cache:
                return cache[eq_id]
            prefetched_ops = getattr(equipment, "_prefetched_objects_cache", {}).get("equipment_operators")
            if prefetched_ops is not None:
                first_link = min(prefetched_ops, key=lambda l: l.equipment_operator_id) if prefetched_ops else None
                operator = getattr(first_link, "operator", None) if first_link is not None else None
            else:
                op_link = getattr(equipment, "equipment_operators", None)
                operator = op_link.select_related("operator").order_by("equipment_operator_id").first().operator if op_link is not None else None
            if operator:
                payload = {
                    "user_id": operator.id,
                    "name": operator.name or operator.email,
                    "email": operator.email,
                    "phone": operator.phone_number,
                    "user_type": "operator",
                }
                cache[eq_id] = payload
                return payload
            cache[eq_id] = None
            return None
        except Exception:
            return None

    def get_oic_contacts(self, obj):
        """Return OIC contact details (equipment managers)."""
        try:
            equipment = getattr(obj, "equipment", None)
            if not equipment:
                return []
            cache = self.context.setdefault("_oic_contacts_cache", {})
            eq_id = getattr(equipment, "equipment_id", None)
            if eq_id in cache:
                return cache[eq_id]
            prefetched_mgrs = getattr(equipment, "_prefetched_objects_cache", {}).get("equipment_managers")
            if prefetched_mgrs is not None:
                managers = sorted(prefetched_mgrs, key=lambda l: l.equipment_manager_id)
            else:
                mgr_link = getattr(equipment, "equipment_managers", None)
                if mgr_link is None:
                    cache[eq_id] = []
                    return []
                managers = list(mgr_link.select_related("manager").order_by("equipment_manager_id"))
            out = []
            for link in managers:
                m = getattr(link, "manager", None)
                if not m:
                    continue
                out.append(
                    {
                        "user_id": m.id,
                        "name": m.name or m.email,
                        "email": m.email,
                        "phone": m.phone_number,
                        "user_type": "manager",
                    }
                )
            cache[eq_id] = out
            return out
        except Exception:
            return []

    def get_user_name(self, obj):
        """Return user's name or email if name is not available."""
        if obj.user:
            return obj.user.name or obj.user.email
        return None
    
    def get_user_phone(self, obj):
        """Return user's phone number."""
        if obj.user:
            return obj.user.phone_number
        return None

    def get_user_department(self, obj):
        """Return user's department name."""
        if obj.user and obj.user.department:
            return obj.user.department.name
        return None

    def get_user_profile_picture(self, obj):
        """Return user's stable profile-picture proxy URL (does not expire)."""
        if obj.user:
            return obj.user.get_profile_picture_url_or_none(request=self.context.get("request"))
        return None

    def get_created_by_name(self, obj):
        """Return the name of the user who created this booking (admin, OIC, or booking user). Fallback to booking user for older records."""
        if obj.created_by:
            return obj.created_by.name or obj.created_by.email
        return self.get_user_name(obj)
    
    def get_status_display(self, obj):
        """Return model's status display."""
        return obj.get_status_display()
    
    def get_total_hours(self, obj):
        """Convert total_time_minutes to hours."""
        if obj.total_time_minutes:
            return round(obj.total_time_minutes / 60, 2)
        return None
    
    def get_input_fields(self, obj):
        """Return all equipment user input fields for booking user-input display section."""
        if not obj.equipment_id:
            return [_comments_input_field_schema()]
        cache = self._get_input_fields_cache()
        if obj.equipment_id not in cache:
            from .models import DynamicInputField
            fields = DynamicInputField.objects.filter(equipment_id=obj.equipment_id).order_by('field_key')
            cache[obj.equipment_id] = [self._build_input_field_item(f) for f in fields]
        result = list(cache[obj.equipment_id])
        # Universal free-text comments field must be shown as the last input.
        result.append(_comments_input_field_schema())
        return result

    def get_all_input_fields(self, obj):
        """Backward-compatible alias of all input fields."""
        return self.get_input_fields(obj)

    def get_editable_input_fields(self, obj):
        """Return only editable input fields for Edit User Inputs popup."""
        if not obj.equipment_id:
            return [_comments_input_field_schema()]
        all_fields = self.get_input_fields(obj)[:-1]
        result = [f for f in all_fields if f.get("editing_required")]
        result.append(_comments_input_field_schema())
        return result

    def get_start_time(self, obj):
        """Get start time from the earliest daily slot."""
        prefetched = getattr(obj, "_prefetched_objects_cache", {}).get("daily_slots")
        if prefetched is not None:
            if not prefetched:
                return None
            return min(prefetched, key=lambda s: s.start_datetime).start_datetime
        daily_slots = obj.daily_slots.all().order_by('start_datetime')
        if daily_slots.exists():
            return daily_slots.first().start_datetime
        return None
    
    def get_end_time(self, obj):
        """Get end time from the latest daily slot."""
        prefetched = getattr(obj, "_prefetched_objects_cache", {}).get("daily_slots")
        if prefetched is not None:
            if not prefetched:
                return None
            return max(prefetched, key=lambda s: s.end_datetime).end_datetime
        daily_slots = obj.daily_slots.all().order_by('-end_datetime')
        if daily_slots.exists():
            return daily_slots.first().end_datetime
        return None

    def get_equipment_weekly_view_display(self, obj):
        """Equipment's weekly view display: TIME or SLOT_ID (Hide time). Used by frontend to show date-only when SLOT_ID."""
        if obj.equipment_id and getattr(obj, 'equipment', None):
            return getattr(obj.equipment, 'weekly_view_display', None) or 'TIME'
        return 'TIME'

    def get_user_type_snapshot_display(self, obj):
        """Human-readable user type label (e.g. IITR Student, IITR Faculty) from snapshot code."""
        code = (obj.user_type_snapshot or '').strip()
        if not code:
            return None
        choices_dict = dict(UserType.get_choices())
        return choices_dict.get(code, code)

    def get_wallet_owner_name(self, obj):
        """Return Supervisor name when the booking user is not the Supervisor (e.g. student using faculty wallet)."""
        if not obj.user_id or not getattr(obj, "user", None):
            return None
        cache = self.context.setdefault("_wallet_owner_display_cache", {})
        try:
            return _get_wallet_owner_display_name(obj.user, cache)
        except Exception:
            return None

    def get_sample_trace(self, obj):
        """Return sample/slot tracing events for arrow-style timeline."""
        events = getattr(obj, "_prefetched_objects_cache", {}).get("sample_trace_events")
        if events is None:
            events_qs = getattr(obj, 'sample_trace_events', None)
            if events_qs is None:
                return []
            events = list(events_qs.order_by('created_at'))
        if not events:
            return []
        events = sorted(events, key=lambda e: e.created_at)
        return BookingSampleTraceSerializer(events, many=True, context=self.context).data


class BookingListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for list/table view. Excludes heavy fields (daily_slots, charge_breakdown, input_fields, sample_trace)."""
    booking_id = serializers.SerializerMethodField()
    real_booking_id = serializers.IntegerField(source='booking_id', read_only=True)
    equipment_code = serializers.CharField(source='equipment.code', read_only=True)
    equipment_name = serializers.CharField(source='equipment.name', read_only=True)
    equipment_reschedule_hours_threshold = serializers.IntegerField(source='equipment.reschedule_hours_threshold', read_only=True, allow_null=True)
    equipment_user_rating_enabled = serializers.BooleanField(source='equipment.user_rating_enabled', read_only=True, default=True)
    user_email = serializers.CharField(source='user.email', read_only=True)
    user_name = serializers.SerializerMethodField()
    user_phone = serializers.SerializerMethodField()
    user_department = serializers.SerializerMethodField()
    status_display = serializers.SerializerMethodField()
    total_hours = serializers.SerializerMethodField()
    start_time = serializers.SerializerMethodField()
    end_time = serializers.SerializerMethodField()
    user_type_snapshot_display = serializers.SerializerMethodField()
    wallet_owner_name = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    equipment_weekly_view_display = serializers.SerializerMethodField()
    repeat_sample_request_status = serializers.SerializerMethodField()
    has_results = serializers.BooleanField(read_only=True, default=False)
    equipment_status = serializers.CharField(source='equipment.status', read_only=True, allow_null=True)
    equipment_is_operational = serializers.SerializerMethodField()
    maintenance_disruption_flag = serializers.BooleanField(read_only=True)
    maintenance_decision_deadline_at = serializers.DateTimeField(read_only=True, allow_null=True)
    maintenance_reschedule_extra_week = serializers.BooleanField(read_only=True)
    maintenance_operational_marked_at = serializers.DateTimeField(read_only=True, allow_null=True)
    disruption_kind = serializers.CharField(read_only=True, allow_null=True)
    disruption_release_slot_status = serializers.CharField(read_only=True, allow_null=True)
    istem_fbr_status_display = serializers.SerializerMethodField()
    istem_portal_url = serializers.SerializerMethodField()
    istem_fbr_status_url = serializers.SerializerMethodField()
    require_istem_fbr = serializers.SerializerMethodField()
    oic_contacts = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = [
            'booking_id', 'real_booking_id', 'virtual_booking_id', 'user', 'user_email', 'user_name', 'user_phone', 'user_department',
            'equipment', 'equipment_code', 'equipment_name', 'equipment_reschedule_hours_threshold',
            'equipment_user_rating_enabled', 'equipment_status', 'equipment_is_operational',
            'maintenance_disruption_flag', 'maintenance_decision_deadline_at', 'maintenance_reschedule_extra_week',
            'maintenance_operational_marked_at', 'disruption_kind', 'disruption_release_slot_status',
            'charge_profile', 'user_type_snapshot', 'total_time_minutes', 'total_hours',
            'total_charge', 'status', 'status_display', 'start_time', 'end_time', 'equipment_weekly_view_display',
            'user_type_snapshot_display', 'wallet_owner_name', 'created_by_name', 'repeat_sample_request_status',
            'has_results',
            'charge_recalculation_pending_amount', 'created_at', 'updated_at', 'completed_at',
            'rating_on_time_operator_availability',
            'rating_laboratory_cleanliness_organization',
            'rating_sample_handling_care',
            'rating_operator_behaviour_professionalism',
            'rating_compliance_booking_request_parameters',
            'rating', 'rating_feedback',
            'rated_at', 'repeat_sample_enabled', 'source_booking_id',
            'istem_fbr_number', 'istem_fbr_status', 'istem_fbr_status_display', 'istem_fbr_invalid_reason', 'istem_fbr_executed_at',
            'istem_portal_url', 'istem_fbr_status_url', 'require_istem_fbr',
            'oic_contacts',
        ]
        read_only_fields = [
            'booking_id',
            'real_booking_id',
            'virtual_booking_id',
            'created_at',
            'updated_at',
            'completed_at',
            'rating_on_time_operator_availability',
            'rating_laboratory_cleanliness_organization',
            'rating_sample_handling_care',
            'rating_operator_behaviour_professionalism',
            'rating_compliance_booking_request_parameters',
            'rating',
            'rating_feedback',
            'rated_at',
            'istem_fbr_number',
            'istem_fbr_status',
            'istem_fbr_status_display',
            'istem_fbr_invalid_reason',
            'istem_fbr_executed_at',
            'oic_contacts',
        ]

    def get_istem_fbr_status_display(self, obj):
        st = getattr(obj, "istem_fbr_status", None)
        if not st:
            return None
        return dict(IstemFbrStatus.choices).get(st, st)

    def get_istem_portal_url(self, obj):
        from .models import get_equipment_istem_portal_url
        equipment = getattr(obj, "equipment", None)
        return get_equipment_istem_portal_url(equipment) if equipment else "https://www.istem.gov.in/"

    def get_istem_fbr_status_url(self, obj):
        from .models import get_equipment_istem_fbr_status_url
        equipment = getattr(obj, "equipment", None)
        return get_equipment_istem_fbr_status_url(equipment) if equipment else ""

    def get_require_istem_fbr(self, obj):
        from .models import charge_profile_requires_istem_fbr
        return charge_profile_requires_istem_fbr(getattr(obj, "charge_profile", None))

    def get_oic_contacts(self, obj):
        return BookingSerializer.get_oic_contacts(self, obj)

    def get_booking_id(self, obj):
        return booking_display_id_for_email(obj)

    def get_repeat_sample_request_status(self, obj):
        prefetched = getattr(obj, "_prefetched_objects_cache", None)
        if prefetched and "repeat_sample_requests" in prefetched:
            reqs = prefetched["repeat_sample_requests"]
            if not reqs:
                return None
            latest = max(reqs, key=lambda r: r.requested_at)
            return latest.status
        latest = obj.repeat_sample_requests.order_by("-requested_at").first()
        return latest.status if latest else None

    def get_user_name(self, obj):
        if obj.user:
            return obj.user.name or obj.user.email
        return None

    def get_user_phone(self, obj):
        return getattr(obj.user, 'phone_number', None) if obj.user else None

    def get_user_department(self, obj):
        if obj.user and getattr(obj.user, 'department', None):
            return obj.user.department.name
        return None

    def get_status_display(self, obj):
        return obj.get_status_display() if hasattr(obj, 'get_status_display') else obj.status

    def get_total_hours(self, obj):
        if obj.total_time_minutes is not None:
            return round(obj.total_time_minutes / 60.0, 2)
        return None

    def get_start_time(self, obj):
        daily_slots = getattr(obj, 'daily_slots', None)
        if daily_slots is not None:
            slots = list(daily_slots.all()) if hasattr(daily_slots, 'all') else list(daily_slots)
            with_start = [s for s in slots if getattr(s, 'start_datetime', None)]
            if with_start:
                return min(with_start, key=lambda s: s.start_datetime).start_datetime
        return None

    def get_end_time(self, obj):
        daily_slots = getattr(obj, 'daily_slots', None)
        if daily_slots is not None:
            slots = list(daily_slots.all()) if hasattr(daily_slots, 'all') else list(daily_slots)
            with_end = [s for s in slots if getattr(s, 'end_datetime', None)]
            if with_end:
                return max(with_end, key=lambda s: s.end_datetime).end_datetime
        return None

    def get_equipment_weekly_view_display(self, obj):
        if obj.equipment_id and getattr(obj, 'equipment', None):
            return getattr(obj.equipment, 'weekly_view_display', None) or 'TIME'
        return 'TIME'

    def get_user_type_snapshot_display(self, obj):
        code = (obj.user_type_snapshot or '').strip()
        if not code:
            return None
        choices_dict = dict(UserType.get_choices())
        return choices_dict.get(code, code)

    def get_wallet_owner_name(self, obj):
        if not obj.user_id or not getattr(obj, "user", None):
            return None
        cache = self.context.setdefault("_wallet_owner_display_cache", {})
        try:
            return _get_wallet_owner_display_name(obj.user, cache)
        except Exception:
            return None

    def get_created_by_name(self, obj):
        if getattr(obj, 'created_by', None):
            return obj.created_by.name or obj.created_by.email
        return None

    def get_equipment_is_operational(self, obj):
        """Operational = ACTIVE (same rule as booking eligibility)."""
        eq = getattr(obj, 'equipment', None)
        if not eq:
            return True
        st = (getattr(eq, 'status', None) or '').strip()
        return st == EquipmentStatus.ACTIVE


class BookingSampleTraceSerializer(serializers.ModelSerializer):
    """Serializer for sample/slot tracing events (arrow timeline)."""
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    created_by_name = serializers.SerializerMethodField()
    reply_attachments = serializers.SerializerMethodField()

    class Meta:
        model = BookingSampleTrace
        fields = ['id', 'status', 'status_display', 'sample_identifiers', 'tracking_id', 'reason', 'user_reply', 'reply_attachments', 'created_at', 'created_by', 'created_by_name']
        read_only_fields = ['id', 'created_at']

    def get_created_by_name(self, obj):
        if obj.created_by:
            return obj.created_by.name or obj.created_by.email
        return None

    def get_reply_attachments(self, obj):
        attachments = getattr(obj, 'reply_attachments', None)
        if not attachments:
            return []
        request = self.context.get('request')
        result = []
        for a in attachments.all():
            url = a.file.url if a.file else None
            if url and request:
                url = request.build_absolute_uri(url)
            result.append({
                'id': a.id,
                'file_url': url,
                'name': a.original_name or (a.file.name if a.file else ''),
            })
        return result


class BookingEventSerializer(serializers.ModelSerializer):
    """Serializer for BookingEvent model."""
    
    event_type_display = serializers.CharField(source='get_event_type_display', read_only=True)
    booking_id = serializers.SerializerMethodField()
    real_booking_id = serializers.IntegerField(source='booking.booking_id', read_only=True)
    created_by_name = serializers.SerializerMethodField()
    previous_status_display = serializers.CharField(source='get_previous_status_display', read_only=True) if hasattr(Booking, 'get_status_display') else serializers.SerializerMethodField()
    new_status_display = serializers.CharField(source='get_new_status_display', read_only=True) if hasattr(Booking, 'get_status_display') else serializers.SerializerMethodField()
    
    class Meta:
        model = BookingEvent
        fields = [
            'event_id',
            'booking',
            'booking_id',
            'real_booking_id',
            'event_type',
            'event_type_display',
            'previous_status',
            'previous_status_display',
            'new_status',
            'new_status_display',
            'comment',
            'created_by',
            'created_by_name',
            'metadata',
            'notification_sent',
            'created_at',
        ]
        read_only_fields = ['event_id', 'created_at', 'notification_sent']

    def get_booking_id(self, obj):
        return booking_display_id_for_email(getattr(obj, "booking", None))
    
    def get_created_by_name(self, obj):
        """Return creator's name or email if name is not available."""
        if obj.created_by:
            return obj.created_by.name or obj.created_by.email
        return None
    
    def get_previous_status_display(self, obj):
        """Return display name for previous status."""
        if obj.previous_status:
            return dict(Booking.BookingStatus.choices).get(obj.previous_status, obj.previous_status)
        return None
    
    def get_new_status_display(self, obj):
        """Return display name for new status."""
        if obj.new_status:
            return dict(Booking.BookingStatus.choices).get(obj.new_status, obj.new_status)
        return None


class BookingCancellationRequestSerializer(serializers.ModelSerializer):
    """Serializer for BookingCancellationRequest."""
    
    booking_id = serializers.SerializerMethodField()
    real_booking_id = serializers.IntegerField(source='booking.booking_id', read_only=True)
    equipment_name = serializers.CharField(source='booking.equipment.name', read_only=True)
    equipment_code = serializers.CharField(source='booking.equipment.code', read_only=True)
    total_charge = serializers.DecimalField(source='booking.total_charge', max_digits=10, decimal_places=2, read_only=True)
    user_email = serializers.EmailField(source='user.email', read_only=True)
    user_name = serializers.CharField(source='user.name', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    
    class Meta:
        model = BookingCancellationRequest
        fields = [
            'id', 'booking', 'booking_id', 'real_booking_id', 'user', 'user_email', 'user_name',
            'equipment_name', 'equipment_code', 'total_charge',
            'status', 'status_display', 'notes', 'response_message',
            'approved_by_email', 'requested_at', 'responded_at'
        ]
        read_only_fields = ['id', 'user', 'status', 'response_message', 'approved_by_email', 'requested_at', 'responded_at']

    def get_booking_id(self, obj):
        return booking_display_id_for_email(getattr(obj, "booking", None))


class RepeatSampleRequestSerializer(serializers.ModelSerializer):
    """Serializer for RepeatSampleRequest (admin/OIC list and detail)."""
    booking_id = serializers.SerializerMethodField()
    real_booking_id = serializers.IntegerField(source='booking.booking_id', read_only=True)
    virtual_booking_id = serializers.CharField(source='booking.virtual_booking_id', read_only=True)
    equipment_name = serializers.CharField(source='booking.equipment.name', read_only=True)
    equipment_code = serializers.CharField(source='booking.equipment.code', read_only=True)
    user_email = serializers.EmailField(source='booking.user.email', read_only=True)
    user_name = serializers.CharField(source='booking.user.name', read_only=True)
    completed_at = serializers.DateTimeField(source='booking.completed_at', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    new_booking_id = serializers.SerializerMethodField()
    new_real_booking_id = serializers.IntegerField(source='new_booking.booking_id', read_only=True, allow_null=True)
    new_virtual_booking_id = serializers.CharField(source='new_booking.virtual_booking_id', read_only=True, allow_null=True)

    class Meta:
        model = RepeatSampleRequest
        fields = [
            'id', 'booking', 'booking_id', 'real_booking_id', 'virtual_booking_id',
            'equipment_name', 'equipment_code', 'user_email', 'user_name',
            'completed_at', 'status', 'status_display',
            'user_notes', 'admin_notes', 'requested_at', 'responded_at', 'responded_by',
            'new_booking', 'new_booking_id', 'new_real_booking_id', 'new_virtual_booking_id',
        ]
        read_only_fields = ['id', 'booking', 'requested_at', 'responded_at', 'responded_by', 'new_booking']

    def get_booking_id(self, obj):
        return booking_display_id_for_email(getattr(obj, "booking", None))

    def get_new_booking_id(self, obj):
        return booking_display_id_for_email(getattr(obj, "new_booking", None)) or None


class TARewardConfigSerializer(serializers.ModelSerializer):
    equipment_code = serializers.CharField(source='equipment.code', read_only=True)
    equipment_name = serializers.CharField(source='equipment.name', read_only=True)

    class Meta:
        model = TARewardConfig
        fields = [
            'id',
            'equipment',
            'equipment_code',
            'equipment_name',
            'is_enabled',
            'points_per_duty_hour',
            'points_per_sample',
            'currency_per_point',
            'max_redeem_percent_per_booking',
            'max_redeem_points_per_booking',
            'min_booking_amount_for_redeem',
            'expiry_days',
            'allow_stack_with_other_discounts',
            'created_at',
            'updated_at',
        ]


class TADutyLogSerializer(serializers.ModelSerializer):
    student_name = serializers.CharField(source='student.name', read_only=True)
    student_email = serializers.CharField(source='student.email', read_only=True)
    equipment_code = serializers.CharField(source='equipment.code', read_only=True)
    equipment_name = serializers.CharField(source='equipment.name', read_only=True)
    assignment_status = serializers.CharField(source='assignment.status', read_only=True)
    verified_by_name = serializers.CharField(source='verified_by.name', read_only=True)
    reward_points_earned = serializers.SerializerMethodField()

    class Meta:
        model = TADutyLog
        fields = [
            'id',
            'nomination',
            'student',
            'student_name',
            'student_email',
            'equipment',
            'equipment_code',
            'equipment_name',
            'booking',
            'assignment',
            'assignment_status',
            'duty_date',
            'hours_spent',
            'samples_processed',
            'remarks',
            'status',
            'reward_points_earned',
            'verified_by',
            'verified_by_name',
            'verified_at',
            'created_by',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'nomination',
            'student',
            'student_name',
            'student_email',
            'equipment',
            'equipment_code',
            'equipment_name',
            'booking',
            'assignment',
            'assignment_status',
            'status',
            'verified_by',
            'verified_by_name',
            'verified_at',
            'created_by',
            'created_at',
            'updated_at',
        ]

    def get_reward_points_earned(self, obj):
        """Points credited when this duty log was verified (EARN ledger row)."""
        if obj.status != TADutyLogStatus.VERIFIED:
            return None
        annotated = getattr(obj, "_reward_points_earned", None)
        if annotated is not None:
            return str(annotated)
        row = (
            TARewardLedger.objects.filter(
                source_type=TARewardLedgerSourceType.DUTY_LOG,
                entry_type=TARewardLedgerEntryType.EARN,
                source_id=obj.pk,
            )
            .order_by("-created_at")
            .first()
        )
        return str(row.points) if row else None

    def validate(self, attrs):
        hours = attrs.get('hours_spent') or 0
        samples = attrs.get('samples_processed') or 0
        if float(hours) <= 0 and int(samples) <= 0:
            raise serializers.ValidationError("Provide hours_spent or samples_processed (> 0).")
        return attrs


class TAAssignmentSerializer(serializers.ModelSerializer):
    ta_student_name = serializers.CharField(source='ta_student.name', read_only=True)
    ta_student_email = serializers.CharField(source='ta_student.email', read_only=True)
    equipment_code = serializers.CharField(source='equipment.code', read_only=True)
    equipment_name = serializers.CharField(source='equipment.name', read_only=True)
    semester_name = serializers.CharField(source='semester.name', read_only=True)
    allocated_by_name = serializers.CharField(source='allocated_by.name', read_only=True)
    booking_display_id = serializers.SerializerMethodField()
    booking_slot_summary = serializers.SerializerMethodField()

    class Meta:
        model = TAAssignment
        fields = [
            'id',
            'nomination',
            'booking',
            'booking_display_id',
            'booking_slot_summary',
            'ta_student',
            'ta_student_name',
            'ta_student_email',
            'equipment',
            'equipment_code',
            'equipment_name',
            'semester',
            'semester_name',
            'status',
            'allocation_notes',
            'expected_hours',
            'allocated_by',
            'allocated_by_name',
            'allocated_at',
            'responded_at',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'ta_student_name',
            'ta_student_email',
            'equipment_code',
            'equipment_name',
            'semester_name',
            'allocated_by_name',
            'booking_display_id',
            'booking_slot_summary',
            'allocated_at',
            'responded_at',
            'created_at',
            'updated_at',
        ]

    def get_booking_display_id(self, obj):
        return booking_display_id_for_email(getattr(obj, 'booking', None)) or ''

    def get_booking_slot_summary(self, obj):
        booking = getattr(obj, 'booking', None)
        if not booking:
            return ''
        slots = booking.daily_slots.all()
        if not slots:
            return ''
        first = slots.first()
        last = slots.last()
        if not first or not first.start_datetime:
            return ''

        def local_parts(dt):
            if not dt:
                return None, None
            try:
                local = timezone.localtime(dt) if timezone.is_aware(dt) else dt
            except Exception:
                local = dt
            return local.date().isoformat(), local.strftime('%H:%M')

        d0, t0 = local_parts(first.start_datetime)
        d1, t1 = local_parts(last.end_datetime) if last and last.end_datetime else (None, None)
        if not d0:
            return ''
        if d1 and d0 == d1 and t0 and t1:
            return f'{d0} {t0}–{t1}'
        if d1 and t0 and t1:
            return f'{d0} {t0} → {d1} {t1}'
        return f'{d0} {t0}' if t0 else d0


class TARewardLedgerSerializer(serializers.ModelSerializer):
    student_name = serializers.CharField(source='student.name', read_only=True)
    student_email = serializers.CharField(source='student.email', read_only=True)
    created_by_name = serializers.CharField(source='created_by.name', read_only=True)

    class Meta:
        model = TARewardLedger
        fields = [
            'id',
            'student',
            'student_name',
            'student_email',
            'entry_type',
            'points',
            'currency_value',
            'source_type',
            'source_id',
            'description',
            'expires_at',
            'is_expired',
            'created_by',
            'created_by_name',
            'created_at',
        ]


class BookingRewardRedemptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = BookingRewardRedemption
        fields = [
            'id',
            'booking',
            'student',
            'points_used',
            'discount_amount',
            'ledger_entry',
            'created_at',
        ]


class AdminRewardAdjustSerializer(serializers.Serializer):
    student_id = serializers.IntegerField()
    points = serializers.DecimalField(max_digits=10, decimal_places=2)
    reason = serializers.CharField()
    reference = serializers.CharField(required=False, allow_blank=True)


class InventoryItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = InventoryItem
        fields = [
            'item_id',
            'item_code',
            'name',
            'category',
            'uom',
            'specification',
            'active',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['item_id', 'item_code', 'created_at', 'updated_at']


class EquipmentInventoryItemSerializer(serializers.ModelSerializer):
    item = InventoryItemSerializer(read_only=True)

    class Meta:
        model = EquipmentInventoryItem
        fields = [
            'id',
            'equipment',
            'item',
            'min_level',
            'max_level',
            'reorder_level',
            'critical_level',
            'default_store_location',
            'is_enabled',
            'created_at',
            'updated_at',
        ]


class EquipmentItemStockSerializer(serializers.ModelSerializer):
    item = InventoryItemSerializer(read_only=True)

    class Meta:
        model = EquipmentItemStock
        fields = [
            'id',
            'equipment',
            'item',
            'current_qty',
            'updated_at',
        ]


class InventoryRequestLineSerializer(serializers.ModelSerializer):
    item_detail = InventoryItemSerializer(source='item', read_only=True)

    class Meta:
        model = InventoryRequestLine
        fields = [
            'id',
            'item',
            'item_detail',
            'requested_qty',
            'approved_qty',
            'issued_qty',
            'estimated_unit_cost',
            'remarks',
            'created_at',
            'updated_at',
        ]


class InventoryRequestSerializer(serializers.ModelSerializer):
    lines = InventoryRequestLineSerializer(many=True, required=False)

    class Meta:
        model = InventoryRequest
        fields = [
            'request_id',
            'request_no',
            'equipment',
            'requested_by',
            'request_type',
            'status',
            'justification',
            'required_by_date',
            'submitted_at',
            'decision_by',
            'decision_at',
            'decision_note',
            'created_at',
            'updated_at',
            'lines',
        ]
        read_only_fields = [
            'request_id',
            'request_no',
            'decision_by',
            'decision_at',
            'created_at',
            'updated_at',
        ]


class InventoryTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = InventoryTransaction
        fields = [
            'transaction_id',
            'equipment',
            'item',
            'tx_type',
            'quantity',
            'unit_cost',
            'batch_no',
            'expiry_date',
            'reference_type',
            'reference_id',
            'performed_by',
            'performed_at',
            'remarks',
            'created_at',
        ]
        read_only_fields = ['transaction_id', 'created_at']


class IssuedAssetSerializer(serializers.ModelSerializer):
    class Meta:
        model = IssuedAsset
        fields = [
            'id',
            'equipment',
            'item',
            'serial_no',
            'issued_to',
            'issued_on',
            'expected_return_on',
            'returned_on',
            'condition_on_issue',
            'condition_on_return',
            'status',
            'created_at',
            'updated_at',
        ]


class ProcurementRequestLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProcurementRequestLine
        fields = [
            'id',
            'item_master',
            'finalized_item_master',
            'manual_item_name',
            'classification',
            'office_corrected_name',
            'office_corrected_classification',
            'quantity',
            'tentative_unit_cost',
            'tentative_total_cost',
            'created_at',
            'updated_at',
        ]


class ProcurementAttachmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProcurementAttachment
        fields = ['id', 'line', 'attachment_type', 'file', 'uploaded_by', 'uploaded_at']


class ProcurementActionLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProcurementActionLog
        fields = ['id', 'action', 'by_user', 'comments', 'metadata', 'created_at']


class ProcurementRequestSerializer(serializers.ModelSerializer):
    lines = ProcurementRequestLineSerializer(many=True, required=False)
    attachments = ProcurementAttachmentSerializer(many=True, read_only=True)
    action_logs = ProcurementActionLogSerializer(many=True, read_only=True)

    class Meta:
        model = ProcurementRequest
        fields = [
            'request_id',
            'request_no',
            'equipment',
            'initiated_by',
            'status',
            'head_approval_required',
            'head_approval_mode',
            'total_estimated_cost',
            'remarks',
            'oic_endorsed_by',
            'oic_endorsed_at',
            'office_verified_by',
            'office_verified_at',
            'store_approved_by',
            'store_approved_at',
            'head_approved_by',
            'head_approved_at',
            'purchase_completed_by',
            'purchase_completed_at',
            'office_seen_by',
            'office_seen_at',
            'created_at',
            'updated_at',
            'lines',
            'attachments',
            'action_logs',
        ]


class EquipmentLifecycleFieldsSerializer(serializers.ModelSerializer):
    """Editable procurement / warranty fields on Equipment."""

    class Meta:
        model = Equipment
        fields = [
            'supplier_name',
            'supplier_contact',
            'purchase_order_ref',
            'purchase_invoice_ref',
            'purchase_date',
            'warranty_start',
            'warranty_end',
            'commissioning_date',
            'asset_serial_number',
            'lifecycle_notes',
        ]


class EquipmentAMCContractSerializer(serializers.ModelSerializer):
    class Meta:
        model = EquipmentAMCContract
        fields = [
            'id',
            'equipment',
            'vendor_name',
            'contract_reference',
            'start_date',
            'end_date',
            'contract_value',
            'coverage_notes',
            'contract_document',
            'is_active',
            'created_by',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_by', 'created_at', 'updated_at']


class EquipmentExpenseSerializer(serializers.ModelSerializer):
    class Meta:
        model = EquipmentExpense
        fields = [
            'id',
            'equipment',
            'expense_type',
            'classification',
            'amount',
            'expense_date',
            'description',
            'procurement_request',
            'amc_contract',
            'created_by',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_by', 'created_at', 'updated_at']


class EquipmentWriteOffActionLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = EquipmentWriteOffActionLog
        fields = ['id', 'action', 'by_user', 'comments', 'created_at']


class EquipmentWriteOffRequestSerializer(serializers.ModelSerializer):
    action_logs = EquipmentWriteOffActionLogSerializer(many=True, read_only=True)

    class Meta:
        model = EquipmentWriteOffRequest
        fields = [
            'id',
            'request_no',
            'equipment',
            'initiated_by',
            'reason',
            'asset_classification',
            'estimated_residual_value',
            'status',
            'office_reviewed_by',
            'office_reviewed_at',
            'store_reviewed_by',
            'store_reviewed_at',
            'head_reviewed_by',
            'head_reviewed_at',
            'executed_by',
            'executed_at',
            'rejection_comments',
            'created_at',
            'updated_at',
            'action_logs',
        ]
        read_only_fields = [
            'id',
            'request_no',
            'initiated_by',
            'office_reviewed_by',
            'office_reviewed_at',
            'store_reviewed_by',
            'store_reviewed_at',
            'head_reviewed_by',
            'head_reviewed_at',
            'executed_by',
            'executed_at',
            'created_at',
            'updated_at',
            'action_logs',
        ]

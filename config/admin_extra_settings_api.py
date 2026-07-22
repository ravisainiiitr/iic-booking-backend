"""Extra admin CRUD for equipment/wallet settings (frontend Admin Settings parity)."""

from __future__ import annotations

from rest_framework import permissions, serializers, status
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet, ViewSet

from iic_booking.users.models.user_type import UserType
from iic_booking.users.rbac import is_department_admin, user_has_permission


class IsMainAdmin(permissions.BasePermission):
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if getattr(request.user, "is_staff", False):
            return True
        return getattr(request.user, "user_type", None) == UserType.ADMIN


class IsMainAdminOrDeptEquipmentAdminReadOnly(permissions.BasePermission):
    """Main Admin (full access), or Department Administrator granted admin_settings.equipment
    (read-only: e.g. semester list used to filter equipment-linked nominations). Writes remain
    Main Admin only since semesters are an institute-wide resource."""

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if getattr(request.user, "is_staff", False):
            return True
        if getattr(request.user, "user_type", None) == UserType.ADMIN:
            return True
        if request.method not in permissions.SAFE_METHODS:
            return False
        if is_department_admin(request.user) and (
            user_has_permission(request.user, "admin_settings.equipment", department_id=request.user.department_id)
            or user_has_permission(request.user, "equipment.manage", department_id=request.user.department_id)
        ):
            return True
        return False


class IsMainAdminOrDeptEquipmentAdmin(permissions.BasePermission):
    """Main Admin (full access), or Department Administrator granted admin_settings.equipment
    (equipment-linked resources only, scoped to their own department in get_queryset)."""

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if getattr(request.user, "is_staff", False):
            return True
        if getattr(request.user, "user_type", None) == UserType.ADMIN:
            return True
        if is_department_admin(request.user) and (
            user_has_permission(request.user, "admin_settings.equipment", department_id=request.user.department_id)
            or user_has_permission(request.user, "equipment.manage", department_id=request.user.department_id)
        ):
            return True
        return False


def register_extra_admin_routes(router):
    from iic_booking.equipment.models import (
        BookingBufferConfig,
        BookingChargeSetting,
        EquipmentModeSchedule,
        ICPMSStandardSample,
        Semester,
        StudentEquipmentNomination,
    )
    from iic_booking.users.models.wallet import WalletWithdrawalRequest
    from iic_booking.users.models.wallet_credit_facility_settings import WalletCreditFacilitySettings
    from iic_booking.users.models.wallet_sric_settings import WalletSricSettings
    from iic_booking.users.models.wallet_student_recharge_settings import WalletStudentRechargeSettings

    class SemesterSerializer(serializers.ModelSerializer):
        class Meta:
            model = Semester
            fields = [
                "id",
                "name",
                "code",
                "start_date",
                "end_date",
                "is_active",
                "created_at",
                "updated_at",
            ]
            read_only_fields = ["id", "created_at", "updated_at"]

    class SemesterViewSet(ModelViewSet):
        permission_classes = [IsMainAdminOrDeptEquipmentAdminReadOnly]
        queryset = Semester.objects.all().order_by("-start_date")
        serializer_class = SemesterSerializer

    class ICPMSStandardSampleSerializer(serializers.ModelSerializer):
        class Meta:
            model = ICPMSStandardSample
            fields = [
                "id",
                "s_no",
                "part_no",
                "name_of_std",
                "list_of_elements",
                "concentration",
                "status",
                "created_at",
                "updated_at",
            ]
            read_only_fields = ["id", "created_at", "updated_at"]

    class ICPMSStandardSampleViewSet(ModelViewSet):
        permission_classes = [IsMainAdmin]
        queryset = ICPMSStandardSample.objects.all().order_by("id")
        serializer_class = ICPMSStandardSampleSerializer

    class EquipmentModeScheduleSerializer(serializers.ModelSerializer):
        parent_equipment_code = serializers.CharField(source="parent_equipment.code", read_only=True)
        mode_equipment_code = serializers.CharField(source="mode_equipment.code", read_only=True)

        class Meta:
            model = EquipmentModeSchedule
            fields = [
                "id",
                "parent_equipment",
                "parent_equipment_code",
                "mode_equipment",
                "mode_equipment_code",
                "start_date",
                "end_date",
                "start_time",
                "end_time",
                "behavior",
                "unavailable_label",
                "unavailable_color",
                "exclusive_blocked_label",
                "exclusive_blocked_color",
                "created_at",
                "updated_at",
            ]
            read_only_fields = ["id", "created_at", "updated_at", "parent_equipment_code", "mode_equipment_code"]

    class EquipmentModeScheduleViewSet(ModelViewSet):
        permission_classes = [IsMainAdminOrDeptEquipmentAdmin]
        queryset = EquipmentModeSchedule.objects.select_related(
            "parent_equipment", "mode_equipment"
        ).order_by("-start_date", "-id")
        serializer_class = EquipmentModeScheduleSerializer

        def get_queryset(self):
            qs = super().get_queryset()
            user = self.request.user
            if is_department_admin(user):
                qs = qs.filter(parent_equipment__internal_department_id=user.department_id)
            return qs

        def _assert_department_scope(self, payload):
            user = self.request.user
            if not is_department_admin(user):
                return
            from iic_booking.equipment.models import Equipment

            for key in ("parent_equipment", "mode_equipment"):
                equipment_id = payload.get(key)
                if equipment_id in (None, ""):
                    continue
                if not Equipment.objects.filter(
                    pk=equipment_id, internal_department_id=user.department_id
                ).exists():
                    raise PermissionDenied(
                        "Department Administrators can only manage mode schedules for equipment in their own department."
                    )

        def create(self, request, *args, **kwargs):
            self._assert_department_scope(request.data)
            return super().create(request, *args, **kwargs)

        def update(self, request, *args, **kwargs):
            self._assert_department_scope(request.data)
            return super().update(request, *args, **kwargs)

        def partial_update(self, request, *args, **kwargs):
            self._assert_department_scope(request.data)
            return super().partial_update(request, *args, **kwargs)

    class BookingChargeSettingSerializer(serializers.ModelSerializer):
        class Meta:
            model = BookingChargeSetting
            fields = ["id", "key", "value"]
            read_only_fields = ["id"]

    class BookingChargeSettingViewSet(ModelViewSet):
        permission_classes = [IsMainAdmin]
        queryset = BookingChargeSetting.objects.all().order_by("key")
        serializer_class = BookingChargeSettingSerializer
        http_method_names = ["get", "head", "options", "patch", "put", "post"]

    class BookingBufferConfigSerializer(serializers.ModelSerializer):
        class Meta:
            model = BookingBufferConfig
            fields = [
                "id",
                "buffer_days",
                "enabled",
                "sample_retention_days",
                "auto_archive_enabled",
                "created_at",
                "updated_at",
            ]
            read_only_fields = ["id", "created_at", "updated_at"]

    class BookingBufferConfigViewSet(ViewSet):
        permission_classes = [IsMainAdmin]

        def _get(self):
            obj = BookingBufferConfig.objects.order_by("pk").first()
            if obj is None:
                obj = BookingBufferConfig.objects.create()
            return obj

        def list(self, request):
            return Response(BookingBufferConfigSerializer(self._get()).data)

        def retrieve(self, request, pk=None):
            return Response(BookingBufferConfigSerializer(self._get()).data)

        def partial_update(self, request, pk=None):
            obj = self._get()
            ser = BookingBufferConfigSerializer(obj, data=request.data, partial=True)
            ser.is_valid(raise_exception=True)
            ser.save()
            return Response(ser.data)

        def update(self, request, pk=None):
            return self.partial_update(request, pk=pk)

    class StudentEquipmentNominationSerializer(serializers.ModelSerializer):
        equipment_code = serializers.CharField(source="equipment.code", read_only=True)
        equipment_name = serializers.CharField(source="equipment.name", read_only=True)
        semester_name = serializers.CharField(source="semester.name", read_only=True)
        student_email = serializers.CharField(source="student.email", read_only=True)
        student_name = serializers.CharField(source="student.name", read_only=True)
        supervisor_email = serializers.CharField(source="supervisor.email", read_only=True)
        supervisor_name = serializers.CharField(source="supervisor.name", read_only=True)
        status_display = serializers.CharField(source="get_status_display", read_only=True)
        has_resume = serializers.SerializerMethodField()

        class Meta:
            model = StudentEquipmentNomination
            fields = [
                "id",
                "equipment",
                "equipment_code",
                "equipment_name",
                "semester",
                "semester_name",
                "student",
                "student_email",
                "student_name",
                "supervisor",
                "supervisor_email",
                "supervisor_name",
                "status",
                "status_display",
                "remarks",
                "nominated_at",
                "approved_at",
                "has_resume",
            ]
            read_only_fields = fields

        def get_has_resume(self, obj):
            return bool(getattr(obj, "resume", None))

    class StudentEquipmentNominationViewSet(ModelViewSet):
        permission_classes = [IsMainAdminOrDeptEquipmentAdmin]
        queryset = StudentEquipmentNomination.objects.select_related(
            "equipment", "semester", "student", "supervisor"
        ).order_by("-nominated_at")
        serializer_class = StudentEquipmentNominationSerializer
        http_method_names = ["get", "head", "options"]

        def get_queryset(self):
            qs = super().get_queryset()
            user = self.request.user
            if is_department_admin(user):
                qs = qs.filter(equipment__internal_department_id=user.department_id)
            status_filter = (self.request.query_params.get("status") or "").strip()
            if status_filter:
                qs = qs.filter(status=status_filter)
            semester_id = self.request.query_params.get("semester") or self.request.query_params.get("semester_id")
            if semester_id:
                qs = qs.filter(semester_id=semester_id)
            return qs

    class WalletSricSettingsSerializer(serializers.ModelSerializer):
        class Meta:
            model = WalletSricSettings
            fields = ["id", "recipient_emails", "bill_section_emails", "grant_code_for_credit"]
            read_only_fields = ["id"]

    class IsMainAdminOrDeptAdmin(permissions.BasePermission):
        """Main Admin or Department Administrator may manage SRIC / Bill Section email settings."""

        def has_permission(self, request, view):
            user = request.user
            if not user or not user.is_authenticated:
                return False
            if getattr(user, "user_type", None) == UserType.ADMIN:
                return True
            return is_department_admin(user)

    class WalletSricSettingsViewSet(ViewSet):
        permission_classes = [IsMainAdminOrDeptAdmin]

        def list(self, request):
            return Response(WalletSricSettingsSerializer(WalletSricSettings.get_singleton()).data)

        def retrieve(self, request, pk=None):
            return self.list(request)

        def partial_update(self, request, pk=None):
            obj = WalletSricSettings.get_singleton()
            data = dict(request.data)
            # Department Administrators may only update Bill Section emails
            if is_department_admin(request.user) and getattr(request.user, "user_type", None) != UserType.ADMIN:
                data = {"bill_section_emails": request.data.get("bill_section_emails", obj.bill_section_emails)}
            ser = WalletSricSettingsSerializer(obj, data=data, partial=True)
            ser.is_valid(raise_exception=True)
            ser.save()
            return Response(ser.data)

        def update(self, request, pk=None):
            return self.partial_update(request, pk=pk)

    class WalletWithdrawalRequestSerializer(serializers.ModelSerializer):
        user_email = serializers.CharField(source="user.email", read_only=True)
        user_name = serializers.CharField(source="user.name", read_only=True)
        status_display = serializers.CharField(source="get_status_display", read_only=True)

        class Meta:
            model = WalletWithdrawalRequest
            fields = [
                "id",
                "user",
                "user_email",
                "user_name",
                "wallet",
                "amount",
                "status",
                "status_display",
                "allocations",
                "user_note",
                "approved_by_email",
                "response_message",
                "utr_reference",
                "bank_snapshot",
                "created_at",
                "updated_at",
                "responded_at",
                "completed_at",
            ]
            read_only_fields = fields

    class WalletWithdrawalRequestViewSet(ModelViewSet):
        permission_classes = [IsMainAdmin]
        queryset = WalletWithdrawalRequest.objects.select_related("user").order_by("-created_at")
        serializer_class = WalletWithdrawalRequestSerializer
        http_method_names = ["get", "head", "options"]

    class WalletCreditFacilitySettingsSerializer(serializers.ModelSerializer):
        class Meta:
            model = WalletCreditFacilitySettings
            fields = [
                "id",
                "balance_threshold_inr",
                "credit_window_days",
                "max_credit_inr",
            ]
            read_only_fields = ["id"]

    class WalletCreditFacilitySettingsViewSet(ViewSet):
        permission_classes = [IsMainAdmin]

        def list(self, request):
            return Response(
                WalletCreditFacilitySettingsSerializer(WalletCreditFacilitySettings.get_singleton()).data
            )

        def retrieve(self, request, pk=None):
            return self.list(request)

        def partial_update(self, request, pk=None):
            obj = WalletCreditFacilitySettings.get_singleton()
            ser = WalletCreditFacilitySettingsSerializer(obj, data=request.data, partial=True)
            ser.is_valid(raise_exception=True)
            ser.save()
            return Response(ser.data)

        def update(self, request, pk=None):
            return self.partial_update(request, pk=pk)

    class WalletStudentRechargeSettingsSerializer(serializers.ModelSerializer):
        class Meta:
            model = WalletStudentRechargeSettings
            fields = ["id", "enable_iitr_student_wallet_recharge"]
            read_only_fields = ["id"]

    class WalletStudentRechargeSettingsViewSet(ViewSet):
        permission_classes = [IsMainAdmin]

        def list(self, request):
            return Response(
                WalletStudentRechargeSettingsSerializer(WalletStudentRechargeSettings.get_singleton()).data
            )

        def retrieve(self, request, pk=None):
            return self.list(request)

        def partial_update(self, request, pk=None):
            obj = WalletStudentRechargeSettings.get_singleton()
            ser = WalletStudentRechargeSettingsSerializer(obj, data=request.data, partial=True)
            ser.is_valid(raise_exception=True)
            ser.save()
            return Response(ser.data)

        def update(self, request, pk=None):
            return self.partial_update(request, pk=pk)

    router.register(r"semesters", SemesterViewSet, basename="admin-semesters")
    router.register(r"icpms-standards", ICPMSStandardSampleViewSet, basename="admin-icpms-standards")
    router.register(
        r"equipment-mode-schedules",
        EquipmentModeScheduleViewSet,
        basename="admin-equipment-mode-schedules",
    )
    router.register(
        r"booking-charge-settings",
        BookingChargeSettingViewSet,
        basename="admin-booking-charge-settings",
    )
    router.register(
        r"booking-buffer-config",
        BookingBufferConfigViewSet,
        basename="admin-booking-buffer-config",
    )
    router.register(
        r"student-equipment-nominations",
        StudentEquipmentNominationViewSet,
        basename="admin-student-equipment-nominations",
    )
    router.register(r"wallet-sric-settings", WalletSricSettingsViewSet, basename="admin-wallet-sric-settings")
    router.register(
        r"wallet-withdrawal-requests",
        WalletWithdrawalRequestViewSet,
        basename="admin-wallet-withdrawal-requests",
    )
    router.register(
        r"wallet-credit-facility-settings",
        WalletCreditFacilitySettingsViewSet,
        basename="admin-wallet-credit-facility-settings",
    )
    router.register(
        r"wallet-student-recharge-settings",
        WalletStudentRechargeSettingsViewSet,
        basename="admin-wallet-student-recharge-settings",
    )
    return router

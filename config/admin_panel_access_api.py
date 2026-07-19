"""Main Admin APIs for Admin Panel access by User Type + Department."""

from __future__ import annotations

from rest_framework import permissions, serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import ViewSet

from iic_booking.users.admin_settings_registry import (
    ADMIN_SECTION_MODULE_KEYS,
    all_module_keys,
    expand_module_keys,
    flatten_admin_settings_modules,
    get_admin_settings_module_tree,
)
from iic_booking.users.models import Department, DepartmentType, UserType
from iic_booking.users.models.admin_panel_access import AdminPanelRoleConfig
from iic_booking.users.rbac import (
    list_effective_admin_module_keys,
    user_can_access_admin_module,
    user_has_admin_panel_access,
)


class IsMainAdmin(permissions.BasePermission):
    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        if getattr(u, "is_staff", False):
            return True
        return getattr(u, "user_type", None) == UserType.ADMIN


class AdminPanelAccessMeView(APIView):
    """Current user's effective Admin Panel access (for frontend gating)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        enabled = user_has_admin_panel_access(user)
        modules = list_effective_admin_module_keys(user) if enabled else []
        return Response(
            {
                "admin_panel_enabled": enabled,
                "module_keys": modules,
                "module_tree": get_admin_settings_module_tree(
                    include_main_admin_only=getattr(user, "user_type", None) == UserType.ADMIN
                    or getattr(user, "is_staff", False)
                ),
            }
        )


class AdminPanelAccessRegistryView(APIView):
    """Full module tree for Main Admin configuration UI."""

    permission_classes = [IsMainAdmin]

    def get(self, request):
        return Response(
            {
                "tree": get_admin_settings_module_tree(include_main_admin_only=True),
                "flat": flatten_admin_settings_modules(),
                "configurable_user_types": [
                    {"value": code, "label": str(label)}
                    for code, label in UserType.get_choices()
                    if code != UserType.ADMIN
                ],
            }
        )


class AdminPanelRoleConfigSerializer(serializers.ModelSerializer):
    department_name = serializers.CharField(source="department.name", read_only=True)
    department_code = serializers.CharField(source="department.code", read_only=True)
    user_type_label = serializers.SerializerMethodField()
    expanded_module_keys = serializers.SerializerMethodField()

    class Meta:
        model = AdminPanelRoleConfig
        fields = [
            "id",
            "user_type",
            "user_type_label",
            "department",
            "department_name",
            "department_code",
            "admin_panel_enabled",
            "module_keys",
            "expanded_module_keys",
            "updated_at",
        ]
        read_only_fields = ["id", "updated_at", "department_name", "department_code"]

    def get_user_type_label(self, obj):
        mapping = dict(UserType.get_choices())
        return str(mapping.get(obj.user_type, obj.user_type))

    def get_expanded_module_keys(self, obj):
        return sorted(expand_module_keys(obj.module_keys or []))

    def validate_user_type(self, value):
        if value == UserType.ADMIN:
            raise serializers.ValidationError("Main Administrator always has full Admin Panel access.")
        valid = {c for c, _ in UserType.get_choices()}
        if value not in valid:
            raise serializers.ValidationError("Invalid user type.")
        return value

    def validate_module_keys(self, value):
        if value is None:
            return []
        if not isinstance(value, list):
            raise serializers.ValidationError("module_keys must be a list of strings.")
        known = all_module_keys()
        cleaned = []
        for item in value:
            key = str(item).strip()
            if not key:
                continue
            if key not in known:
                raise serializers.ValidationError(f"Unknown module key: {key}")
            cleaned.append(key)
        from iic_booking.users.admin_settings_registry import normalize_module_keys

        return sorted(normalize_module_keys(cleaned))

    def validate_department(self, value):
        if value is None:
            raise serializers.ValidationError("Department is required.")
        if getattr(value, "department_type", None) != DepartmentType.INTERNAL:
            raise serializers.ValidationError("Only internal departments can be configured.")
        return value


class AdminPanelRoleConfigViewSet(ViewSet):
    """CRUD-ish API for AdminPanelRoleConfig (Main Admin only)."""

    permission_classes = [IsMainAdmin]

    def list(self, request):
        qs = AdminPanelRoleConfig.objects.select_related("department").order_by(
            "department__name", "user_type"
        )
        user_type = (request.query_params.get("user_type") or "").strip()
        department_id = (request.query_params.get("department") or "").strip()
        if user_type:
            qs = qs.filter(user_type=user_type)
        if department_id:
            try:
                qs = qs.filter(department_id=int(department_id))
            except ValueError:
                return Response({"error": "Invalid department id."}, status=status.HTTP_400_BAD_REQUEST)
        return Response(AdminPanelRoleConfigSerializer(qs, many=True).data)

    def retrieve(self, request, pk=None):
        try:
            obj = AdminPanelRoleConfig.objects.select_related("department").get(pk=pk)
        except AdminPanelRoleConfig.DoesNotExist:
            return Response({"error": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(AdminPanelRoleConfigSerializer(obj).data)

    def create(self, request):
        # Prefer upsert semantics so UniqueTogetherValidator does not fire on existing rows.
        return self.upsert(request)

    def partial_update(self, request, pk=None):
        try:
            obj = AdminPanelRoleConfig.objects.get(pk=pk)
        except AdminPanelRoleConfig.DoesNotExist:
            return Response({"error": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        ser = AdminPanelRoleConfigSerializer(obj, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        obj = ser.save(updated_by=request.user)
        return Response(AdminPanelRoleConfigSerializer(obj).data)

    def destroy(self, request, pk=None):
        try:
            obj = AdminPanelRoleConfig.objects.get(pk=pk)
        except AdminPanelRoleConfig.DoesNotExist:
            return Response({"error": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    def upsert(self, request):
        """
        POST body: { user_type, department, admin_panel_enabled, module_keys }
        Always updates the existing (user_type, department) row when present.
        Avoids UniqueTogetherValidator errors on disable/re-enable.
        """
        raw_ut = (request.data.get("user_type") or "").strip()
        raw_dept = request.data.get("department")
        try:
            dept_id = int(raw_dept)
        except (TypeError, ValueError):
            return Response({"error": "department is required."}, status=status.HTTP_400_BAD_REQUEST)

        existing = AdminPanelRoleConfig.objects.filter(
            user_type=raw_ut, department_id=dept_id
        ).first()
        if existing:
            ser = AdminPanelRoleConfigSerializer(existing, data=request.data, partial=True)
        else:
            ser = AdminPanelRoleConfigSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        obj = ser.save(updated_by=request.user)
        return Response(AdminPanelRoleConfigSerializer(obj).data)


def require_admin_module(module_key: str):
    """DRF permission class factory for a specific Admin Settings module key."""

    class _Perm(permissions.BasePermission):
        def has_permission(self, request, view):
            if not request.user or not request.user.is_authenticated:
                return False
            return user_can_access_admin_module(request.user, module_key)

    _Perm.__name__ = f"RequireAdminModule_{module_key.replace('.', '_')}"
    return _Perm


def assert_admin_section_module(user, section_key: str) -> None:
    """Raise PermissionDenied if user cannot access the admin section module."""
    from rest_framework.exceptions import PermissionDenied

    module_key = ADMIN_SECTION_MODULE_KEYS.get(section_key)
    if not module_key:
        # Unknown section: require general admin panel access
        if not user_has_admin_panel_access(user):
            raise PermissionDenied("Admin Panel access is disabled for your user type.")
        return
    if not user_can_access_admin_module(user, module_key):
        raise PermissionDenied(
            f"You do not have permission to access this Admin Settings module ({module_key})."
        )

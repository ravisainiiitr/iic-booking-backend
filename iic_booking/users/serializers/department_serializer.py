"""Serializers for Department model."""

from rest_framework import serializers

from ..models import Department


class DepartmentSerializer(serializers.ModelSerializer[Department]):
    """Serializer for Department model."""

    user_count = serializers.SerializerMethodField()
    equipment_count = serializers.SerializerMethodField()
    department_type_display = serializers.CharField(source="get_department_type_display", read_only=True)

    class Meta:
        model = Department
        fields = [
            "id",
            "name",
            "code",
            "internal_grant_code",
            "access_enabled",
            "department_type",
            "department_type_display",
            "external_subcategory",
            "state",
            "description",
            "created_at",
            "updated_at",
            "user_count",
            "equipment_count",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_user_count(self, obj: Department) -> int:
        """Get count of users in this department."""
        return obj.users.count()

    def get_equipment_count(self, obj: Department) -> int:
        """Count of equipment mapped to this (internal) department."""
        from iic_booking.equipment.models import Equipment
        return Equipment.objects.filter(internal_department=obj).count()


class DepartmentListSerializer(serializers.ModelSerializer[Department]):
    """Simplified serializer for listing departments."""

    department_type_display = serializers.CharField(source="get_department_type_display", read_only=True)

    class Meta:
        model = Department
        fields = ["id", "name", "code", "department_type", "department_type_display", "access_enabled"]
        read_only_fields = ["id"]


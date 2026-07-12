"""Serializers package for users app."""

from .department_serializer import (
    DepartmentSerializer,
    DepartmentListSerializer,
)
from .user_serializer import UserSerializer, AdminUserCreateSerializer, AdminUserUpdateSerializer, AdminUserSetPasswordSerializer
from .billing_serializer import ExternalBillingProfileSerializer
from rest_framework import serializers
from ..models.organization_request import OrganizationRequest
from ..models.department import ExternalDepartmentSubcategory


class OrganizationRequestCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating organization requests from public signup."""

    class Meta:
        model = OrganizationRequest
        fields = ("name", "state", "external_subcategory", "email", "requester_name", "web_page", "notes")

    def validate(self, attrs):
        # Ensure a supported organization type.
        sub = attrs.get("external_subcategory")
        allowed = {c[0] for c in ExternalDepartmentSubcategory.get_choices()}
        if sub is not None and sub not in allowed:
            raise serializers.ValidationError({"external_subcategory": "Invalid organization type."})
        return attrs

__all__ = [
    "DepartmentSerializer",
    "DepartmentListSerializer",
    "UserSerializer",
    "AdminUserCreateSerializer",
    "AdminUserUpdateSerializer",
    "AdminUserSetPasswordSerializer",
    "ExternalBillingProfileSerializer",
    "OrganizationRequestCreateSerializer",
]


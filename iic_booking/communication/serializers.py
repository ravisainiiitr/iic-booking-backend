"""Serializers for communication models."""

from rest_framework import serializers
from .models import Notice


class NoticeSerializer(serializers.ModelSerializer):
    """Serializer for Notice model (public + full request details)."""

    created_by_name = serializers.CharField(source="created_by.name", read_only=True, allow_null=True)
    notice_type_display = serializers.CharField(source="get_notice_type_display", read_only=True)
    approval_status_display = serializers.CharField(
        source="get_approval_status_display", read_only=True
    )
    source_display = serializers.CharField(source="get_source_display", read_only=True)
    requested_by_name = serializers.CharField(
        source="requested_by.name", read_only=True, allow_null=True
    )
    requested_by_email = serializers.EmailField(
        source="requested_by.email", read_only=True, allow_null=True
    )
    reviewed_by_name = serializers.CharField(
        source="reviewed_by.name", read_only=True, allow_null=True
    )
    equipment_code = serializers.CharField(source="equipment.code", read_only=True, allow_null=True)
    equipment_name = serializers.CharField(source="equipment.name", read_only=True, allow_null=True)

    class Meta:
        model = Notice
        fields = [
            "notice_id",
            "title",
            "description",
            "content",
            "notice_type",
            "notice_type_display",
            "is_active",
            "priority",
            "created_by",
            "created_by_name",
            "expiry_date",
            "expiry_unlimited",
            "needs_oic_expiry",
            "approval_status",
            "approval_status_display",
            "source",
            "source_display",
            "equipment",
            "equipment_code",
            "equipment_name",
            "requested_by",
            "requested_by_name",
            "requested_by_email",
            "reviewed_by",
            "reviewed_by_name",
            "reviewed_at",
            "review_comment",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "notice_id",
            "created_at",
            "updated_at",
            "created_by_name",
            "notice_type_display",
            "approval_status_display",
            "source_display",
            "requested_by_name",
            "requested_by_email",
            "reviewed_by_name",
            "equipment_code",
            "equipment_name",
            "reviewed_by",
            "reviewed_at",
        ]

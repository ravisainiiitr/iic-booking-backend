"""Serializers for support ticket models."""

from rest_framework import serializers
from .models import Ticket, TicketComment, TicketTypeCode


def _validate_attachment(file):
    if not file:
        return file
    max_bytes = 10 * 1024 * 1024  # 10 MB
    if getattr(file, "size", 0) > max_bytes:
        raise serializers.ValidationError("Attachment must be 10MB or smaller.")
    name = (getattr(file, "name", "") or "").lower()
    allowed_ext = (
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".gif",
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".txt",
    )
    if name and not name.endswith(allowed_ext):
        raise serializers.ValidationError(
            "Unsupported attachment type. Please upload an image or document (pdf/doc/docx/xls/xlsx/ppt/pptx/txt)."
        )
    return file


class TicketCommentSerializer(serializers.ModelSerializer):
    """Serializer for TicketComment model."""
    
    user_name = serializers.CharField(source='user.name', read_only=True)
    user_email = serializers.CharField(source='user.email', read_only=True)
    
    class Meta:
        model = TicketComment
        fields = [
            'comment_id',
            'ticket',
            'user',
            'user_name',
            'user_email',
            'comment',
            'is_internal',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['comment_id', 'created_at', 'updated_at', 'user_name', 'user_email']


class TicketSerializer(serializers.ModelSerializer):
    """Serializer for Ticket model."""
    
    user_name = serializers.CharField(source='user.name', read_only=True)
    user_email = serializers.CharField(source='user.email', read_only=True)
    ticket_type_display = serializers.CharField(source='get_ticket_type_display', read_only=True)
    ticket_type_name = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    priority_display = serializers.CharField(source='get_priority_display', read_only=True)
    assigned_to_name = serializers.CharField(source='assigned_to.name', read_only=True)
    assigned_to_email = serializers.CharField(source='assigned_to.email', read_only=True)
    related_equipment_name = serializers.CharField(source='related_equipment.name', read_only=True)
    related_equipment_code = serializers.CharField(source='related_equipment.code', read_only=True)
    related_booking_id = serializers.IntegerField(source='related_booking.booking_id', read_only=True)
    comments = TicketCommentSerializer(many=True, read_only=True)
    comments_count = serializers.IntegerField(source='comments.count', read_only=True)
    attachment_url = serializers.SerializerMethodField()

    def get_attachment_url(self, obj):
        if not getattr(obj, "attachment", None):
            return None
        try:
            return obj.attachment.url
        except Exception:
            return None
    
    def get_ticket_type_name(self, obj):
        """Get the display name for the ticket type."""
        return TicketTypeCode.get_display_name(obj.ticket_type)
    
    class Meta:
        model = Ticket
        fields = [
            'ticket_id',
            'user',
            'user_name',
            'user_email',
            'public_name',
            'public_email',
            'public_phone',
            'ticket_type',
            'ticket_type_display',
            'ticket_type_name',
            'subject',
            'description',
            'attachment_url',
            'related_equipment',
            'related_equipment_name',
            'related_equipment_code',
            'related_booking',
            'related_booking_id',
            'status',
            'status_display',
            'priority',
            'priority_display',
            'assigned_to',
            'assigned_to_name',
            'assigned_to_email',
            'resolution_notes',
            'created_at',
            'updated_at',
            'resolved_at',
            'closed_at',
            'comments',
            'comments_count',
        ]
        read_only_fields = [
            'ticket_id',
            'created_at',
            'updated_at',
            'resolved_at',
            'closed_at',
            'user_name',
            'user_email',
            'ticket_type_display',
            'ticket_type_name',
            'status_display',
            'priority_display',
            'assigned_to_name',
            'assigned_to_email',
            'related_equipment_name',
            'related_equipment_code',
            'related_booking_id',
            'comments',
            'comments_count',
        ]


class TicketCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating tickets (allows public users)."""

    attachment = serializers.FileField(required=False, allow_null=True)

    def validate_attachment(self, value):
        return _validate_attachment(value)
    
    class Meta:
        model = Ticket
        fields = [
            'public_name',
            'public_email',
            'public_phone',
            'ticket_type',
            'subject',
            'description',
            'attachment',
            'related_equipment',
            'related_booking',
        ]

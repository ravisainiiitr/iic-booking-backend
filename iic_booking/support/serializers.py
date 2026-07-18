"""Serializers for support ticket models."""

from rest_framework import serializers
from .models import Ticket, TicketComment, TicketEvent, TicketTypeCode, PortalFeedback


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

    user_name = serializers.CharField(source="user.name", read_only=True)
    user_email = serializers.CharField(source="user.email", read_only=True)

    class Meta:
        model = TicketComment
        fields = [
            "comment_id",
            "ticket",
            "user",
            "user_name",
            "user_email",
            "comment",
            "is_internal",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["comment_id", "created_at", "updated_at", "user_name", "user_email"]


class TicketEventSerializer(serializers.ModelSerializer):
    actor_name = serializers.SerializerMethodField()
    actor_email = serializers.CharField(source="actor.email", read_only=True, allow_null=True)
    event_type_display = serializers.CharField(source="get_event_type_display", read_only=True)

    class Meta:
        model = TicketEvent
        fields = [
            "event_id",
            "ticket",
            "event_type",
            "event_type_display",
            "actor",
            "actor_name",
            "actor_email",
            "message",
            "from_value",
            "to_value",
            "metadata",
            "is_internal",
            "created_at",
        ]
        read_only_fields = fields

    def get_actor_name(self, obj):
        if not obj.actor_id:
            return None
        return obj.actor.get_display_name()


class TicketSerializer(serializers.ModelSerializer):
    """Serializer for Ticket model."""

    user_name = serializers.SerializerMethodField()
    user_email = serializers.CharField(source="user.email", read_only=True)
    ticket_type_display = serializers.CharField(source="get_ticket_type_display", read_only=True)
    ticket_type_name = serializers.SerializerMethodField()
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    priority_display = serializers.CharField(source="get_priority_display", read_only=True)
    assigned_to_name = serializers.SerializerMethodField()
    assigned_to_email = serializers.CharField(source="assigned_to.email", read_only=True)
    related_equipment_name = serializers.CharField(source="related_equipment.name", read_only=True)
    related_equipment_code = serializers.CharField(source="related_equipment.code", read_only=True)
    related_booking_id = serializers.IntegerField(source="related_booking.booking_id", read_only=True)
    comments = TicketCommentSerializer(many=True, read_only=True)
    comments_count = serializers.IntegerField(source="comments.count", read_only=True)
    attachment_url = serializers.SerializerMethodField()
    attachment_name = serializers.SerializerMethodField()
    requester_name = serializers.SerializerMethodField()
    requester_email = serializers.SerializerMethodField()

    def get_attachment_url(self, obj):
        if not getattr(obj, "attachment", None) or not getattr(obj.attachment, "name", None):
            return None
        try:
            from django.urls import NoReverseMatch, reverse

            try:
                path = reverse("ticket-attachment", kwargs={"ticket_id": obj.ticket_id})
            except NoReverseMatch:
                path = reverse("api:ticket-attachment", kwargs={"ticket_id": obj.ticket_id})
            request = self.context.get("request")
            if request is not None:
                try:
                    return request.build_absolute_uri(path)
                except Exception:
                    return path
            return path
        except Exception:
            try:
                return obj.attachment.url
            except Exception:
                return None

    def get_attachment_name(self, obj):
        if not getattr(obj, "attachment", None) or not getattr(obj.attachment, "name", None):
            return None
        name = (obj.attachment.name or "").rsplit("/", 1)[-1]
        return name or None

    def get_ticket_type_name(self, obj):
        return TicketTypeCode.get_display_name(obj.ticket_type)

    def get_user_name(self, obj):
        return obj.get_user_name()

    def get_assigned_to_name(self, obj):
        if obj.assigned_to_id:
            return obj.assigned_to.get_display_name()
        return None

    def get_requester_name(self, obj):
        return obj.get_user_name()

    def get_requester_email(self, obj):
        return obj.get_user_email()

    class Meta:
        model = Ticket
        fields = [
            "ticket_id",
            "user",
            "user_name",
            "user_email",
            "public_name",
            "public_email",
            "public_phone",
            "requester_name",
            "requester_email",
            "ticket_type",
            "ticket_type_display",
            "ticket_type_name",
            "subject",
            "description",
            "attachment_url",
            "attachment_name",
            "related_equipment",
            "related_equipment_name",
            "related_equipment_code",
            "related_booking",
            "related_booking_id",
            "status",
            "status_display",
            "priority",
            "priority_display",
            "assigned_to",
            "assigned_to_name",
            "assigned_to_email",
            "resolution_notes",
            "created_at",
            "updated_at",
            "resolved_at",
            "closed_at",
            "comments",
            "comments_count",
        ]
        read_only_fields = [
            "ticket_id",
            "created_at",
            "updated_at",
            "resolved_at",
            "closed_at",
            "user_name",
            "user_email",
            "requester_name",
            "requester_email",
            "ticket_type_display",
            "ticket_type_name",
            "status_display",
            "priority_display",
            "assigned_to_name",
            "assigned_to_email",
            "related_equipment_name",
            "related_equipment_code",
            "related_booking_id",
            "comments",
            "comments_count",
            "attachment_url",
            "attachment_name",
        ]


class TicketCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating tickets (allows public users)."""

    attachment = serializers.FileField(required=False, allow_null=True)

    def validate_attachment(self, value):
        return _validate_attachment(value)

    class Meta:
        model = Ticket
        fields = [
            "public_name",
            "public_email",
            "public_phone",
            "ticket_type",
            "subject",
            "description",
            "priority",
            "attachment",
            "related_equipment",
            "related_booking",
        ]


class PortalFeedbackSerializer(serializers.ModelSerializer):
    user_name = serializers.SerializerMethodField()
    user_email = serializers.EmailField(source="user.email", read_only=True)
    user_type = serializers.CharField(source="user.user_type", read_only=True)
    user_type_display = serializers.SerializerMethodField()
    department_name = serializers.CharField(
        source="user.department.name", read_only=True, allow_null=True
    )
    average_rating = serializers.FloatField(read_only=True)

    class Meta:
        model = PortalFeedback
        fields = [
            "feedback_id",
            "user",
            "user_name",
            "user_email",
            "user_type",
            "user_type_display",
            "department_name",
            "overall_rating",
            "ease_of_booking",
            "website_usability",
            "equipment_booking_experience",
            "average_rating",
            "suggestions",
            "comments",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "feedback_id",
            "user",
            "user_name",
            "user_email",
            "user_type",
            "user_type_display",
            "department_name",
            "average_rating",
            "created_at",
            "updated_at",
        ]

    def get_user_name(self, obj):
        return obj.user.get_display_name() if obj.user else ""

    def get_user_type_display(self, obj):
        if obj.user:
            return obj.user.get_user_type_display_label()
        return None

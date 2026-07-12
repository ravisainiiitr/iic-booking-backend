"""Admin configuration for communication models."""

from django.contrib import admin
from django.contrib.admin import SimpleListFilter
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from .models import (
    CommunicationLog,
    CommunicationTemplate,
    CommunicationWebhookEventLog,
    Notice,
)
from .utils import set_current_user, clear_current_user


class IsActiveFilter(SimpleListFilter):
    """Filter for active/inactive items."""
    title = _("Is Active")
    parameter_name = "is_active"

    def lookups(self, request, model_admin):
        return (
            ("yes", _("Yes")),
            ("no", _("No")),
        )

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(is_active=True)
        if self.value() == "no":
            return queryset.filter(is_active=False)
        return queryset


@admin.register(CommunicationTemplate)
class CommunicationTemplateAdmin(admin.ModelAdmin):
    """Admin interface for CommunicationTemplate model."""

    list_display = [
        "name",
        "code",
        "communication_type",
        "subject_preview",
        "is_active",
        "created_at",
        "updated_at",
    ]
    list_filter = [
        "communication_type",
        IsActiveFilter,
        "created_at",
        "updated_at",
    ]
    search_fields = [
        "name",
        "code",
        "subject",
        "description",
    ]
    readonly_fields = [
        "created_at",
        "updated_at",
        "created_by",
        "updated_by",
    ]
    fieldsets = (
        (
            _("Basic Information"),
            {
                "fields": (
                    "name",
                    "code",
                    "communication_type",
                    "description",
                ),
            },
        ),
        (
            _("Content"),
            {
                "fields": (
                    "subject",
                    "body_text",
                    "body_html",
                    "sms_body",
                    "push_data",
                ),
                "description": _(
                    "Content fields vary by communication type:\n"
                    "- Email: subject, body_text, body_html\n"
                    "- Push Notification: subject (title), body_text (message), push_data\n"
                    "- SMS: sms_body (or body_text if sms_body is empty)"
                ),
            },
        ),
        (
            _("Template Variables"),
            {
                "fields": ("variable_help",),
                "description": _(
                    "Document the available template variables here. "
                    "Variables should be in the format {{ variable_name }}. "
                    "Example: {{ user_name }}, {{ verification_url }}, {{ booking_id }}"
                ),
            },
        ),
        (
            _("Status & Metadata"),
            {
                "fields": (
                    "is_active",
                    "created_by",
                    "updated_by",
                    "created_at",
                    "updated_at",
                ),
            },
        ),
    )
    ordering = ["communication_type", "name"]
    raw_id_fields = ["created_by", "updated_by"]

    def subject_preview(self, obj):
        """Display a preview of the subject."""
        if obj.subject:
            preview = obj.subject[:50] + "..." if len(obj.subject) > 50 else obj.subject
            return format_html('<span title="{}">{}</span>', obj.subject, preview)
        return "-"

    subject_preview.short_description = _("Subject Preview")

    def save_model(self, request, obj, form, change):
        """Set current user for automatic created_by/updated_by management.
        
        Ensures created_by and updated_by are always set to the logged-in admin user.
        """
        # Ensure only admin users can create/update templates
        if not request.user.is_staff:
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied("Only admin users can create or update communication templates.")
        
        # Set current user for automatic field management
        set_current_user(request.user)
        try:
            # Explicitly set created_by and updated_by to ensure they're always set
            if not change:  # New object
                obj.created_by = request.user
            obj.updated_by = request.user
            super().save_model(request, obj, form, change)
        finally:
            clear_current_user()
    
    def has_add_permission(self, request):
        """Only allow admin users to add templates."""
        return request.user.is_staff
    
    def has_change_permission(self, request, obj=None):
        """Only allow admin users to change templates."""
        return request.user.is_staff
    
    def has_delete_permission(self, request, obj=None):
        """Only allow admin users to delete templates."""
        return request.user.is_staff


class CommunicationTypeFilter(SimpleListFilter):
    """Filter for communication types."""
    title = _("Communication Type")
    parameter_name = "communication_type"

    def lookups(self, request, model_admin):
        return CommunicationLog.CommunicationType.choices

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(communication_type=self.value())
        return queryset


class CommunicationStatusFilter(SimpleListFilter):
    """Filter for communication status."""
    title = _("Status")
    parameter_name = "status"

    def lookups(self, request, model_admin):
        return CommunicationLog.CommunicationStatus.choices

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(status=self.value())
        return queryset


class CommunicationWebhookEventLogInline(admin.TabularInline):
    """Inline admin for webhook events under CommunicationLog."""
    
    model = CommunicationWebhookEventLog
    extra = 0
    can_delete = False
    readonly_fields = [
        "webhook_event_type",
        "provider_type",
        "provider_message_id",
        "recipient_email",
        "recipient_phone",
        "recipient_user",
        "recipient_display",
        "processed",
        "processed_at",
        "processing_error",
        "event_timestamp",
        "received_at",
        "webhook_payload",
    ]
    fields = [
        "webhook_event_type",
        "provider_type",
        "provider_message_id",
        "recipient_display",
        "processed",
        "processed_at",
        "received_at",
    ]
    
    def has_add_permission(self, request, obj=None):
        """Webhook events can only be created via webhooks, not manually."""
        return False
    
    def has_change_permission(self, request, obj=None):
        """Webhook events are read-only."""
        return False
    
    def has_delete_permission(self, request, obj=None):
        """Webhook events cannot be deleted."""
        return False
    
    def recipient_display(self, obj):
        """Display recipient information."""
        if obj.recipient_user:
            return format_html(
                '<a href="{}">{}</a>',
                f"/admin/users/user/{obj.recipient_user.id}/change/",
                obj.recipient_user.email,
            )
        if obj.recipient_email:
            return obj.recipient_email
        if obj.recipient_phone:
            return obj.recipient_phone
        return "-"
    
    recipient_display.short_description = _("Recipient")


@admin.register(CommunicationLog)
class CommunicationLogAdmin(admin.ModelAdmin):
    """Admin interface for CommunicationLog model."""

    list_display = [
        "communication_type",
        "recipient",
        "subject_preview",
        "status",
        "sent_at",
        "delivered_at",
        "read_at",
        "created_at",
    ]
    list_filter = [
        CommunicationTypeFilter,
        CommunicationStatusFilter,
        "created_at",
        "sent_at",
        "delivered_at",
    ]
    search_fields = [
        "recipient__email",
        "recipient__name",
        "subject",
        "message",
        "error_message",
        "provider_message_id",
    ]
    readonly_fields = [
        "communication_type",
        "recipient",
        "template",
        "subject",
        "message",
        "status",
        "sent_at",
        "delivered_at",
        "read_at",
        "provider_message_id",
        "metadata",
        "error_message",
        "created_by",
        "created_at",
        "updated_at",
    ]
    fieldsets = (
        (
            _("Communication Information"),
            {
                "fields": (
                    "communication_type",
                    "recipient",
                    "template",
                ),
            },
        ),
        (
            _("Content"),
            {
                "fields": (
                    "subject",
                    "message",
                ),
            },
        ),
        (
            _("Status & Tracking"),
            {
                "fields": (
                    "status",
                    "sent_at",
                    "delivered_at",
                    "read_at",
                ),
            },
        ),
        (
            _("Provider Information"),
            {
                "fields": (
                    "provider_message_id",
                    "metadata",
                ),
            },
        ),
        (
            _("Error Information"),
            {
                "fields": ("error_message",),
                "classes": ("collapse",),
            },
        ),
        (
            _("Metadata"),
            {
                "fields": (
                    "created_by",
                    "created_at",
                    "updated_at",
                ),
            },
        ),
    )
    ordering = ["-created_at"]
    raw_id_fields = ["recipient", "template", "created_by"]
    inlines = [CommunicationWebhookEventLogInline]

    def subject_preview(self, obj):
        """Display a preview of the subject."""
        if obj.subject:
            preview = obj.subject[:50] + "..." if len(obj.subject) > 50 else obj.subject
            return format_html('<span title="{}">{}</span>', obj.subject, preview)
        return "-"

    subject_preview.short_description = _("Subject Preview")

    def has_add_permission(self, request):
        """Communication logs can only be created programmatically, not manually."""
        return False
    
    def has_change_permission(self, request, obj=None):
        """Allow viewing but not editing communication logs."""
        # Return True to allow viewing, but all fields are readonly so editing is disabled
        return True
    
    def has_delete_permission(self, request, obj=None):
        """Communication logs cannot be deleted."""
        return False


class NoticeTypeFilter(SimpleListFilter):
    """Filter for notice types."""
    title = _("Notice Type")
    parameter_name = "notice_type"

    def lookups(self, request, model_admin):
        return Notice.NoticeType.choices

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(notice_type=self.value())
        return queryset


@admin.register(Notice)
class NoticeAdmin(admin.ModelAdmin):
    """Admin interface for Notice model."""

    list_display = [
        "title",
        "notice_type",
        "is_active",
        "priority",
        "expiry_date",
        "created_by",
        "created_at",
        "updated_at",
    ]
    list_filter = [
        NoticeTypeFilter,
        IsActiveFilter,
        "expiry_date",
        "created_at",
        "updated_at",
    ]
    search_fields = [
        "title",
        "description",
        "content",
    ]
    readonly_fields = [
        "created_at",
        "updated_at",
        "created_by",
    ]
    fieldsets = (
        (
            _("Basic Information"),
            {
                "fields": (
                    "title",
                    "description",
                    "content",
                ),
            },
        ),
        (
            _("Settings"),
            {
                "fields": (
                    "notice_type",
                    "is_active",
                    "priority",
                    "expiry_date",
                ),
            },
        ),
        (
            _("Metadata"),
            {
                "fields": (
                    "created_by",
                    "created_at",
                    "updated_at",
                ),
            },
        ),
    )
    ordering = ["-priority", "-created_at"]
    raw_id_fields = ["created_by"]

    def save_model(self, request, obj, form, change):
        """Set created_by for new notices."""
        if not change:  # New object
            obj.created_by = request.user
        super().save_model(request, obj, form, change)



"""Admin configuration for support app."""

from django.contrib import admin
from .models import SupportNotificationSettings, Ticket, TicketComment


@admin.register(SupportNotificationSettings)
class SupportNotificationSettingsAdmin(admin.ModelAdmin):
    list_display = ["__str__", "ticket_alert_enabled", "ticket_alert_emails", "updated_at", "updated_by"]
    readonly_fields = ["updated_at", "updated_by"]

    def has_add_permission(self, request):
        return not SupportNotificationSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


class TicketCommentInline(admin.TabularInline):
    """Inline admin for Ticket Comments."""
    model = TicketComment
    extra = 0
    fields = ['user', 'comment', 'is_internal', 'created_at']
    readonly_fields = ['created_at']
    ordering = ['-created_at']
    verbose_name = "Comment"
    verbose_name_plural = "Comments"


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    """Admin interface for Ticket model."""
    
    def get_user_display(self, obj):
        """Display user name or email, or public name/email."""
        if obj.user:
            return obj.user.name or obj.user.email
        elif obj.public_name:
            return f"{obj.public_name} ({obj.public_email})"
        elif obj.public_email:
            return obj.public_email
        return "-"
    get_user_display.short_description = "User"
    
    def get_assigned_to_display(self, obj):
        """Display assigned user name or email."""
        if obj.assigned_to:
            return obj.assigned_to.name or obj.assigned_to.email
        return "-"
    get_assigned_to_display.short_description = "Assigned To"

    def has_attachment(self, obj):
        return bool(getattr(obj, "attachment", None) and obj.attachment.name)

    has_attachment.boolean = True
    has_attachment.short_description = "Attachment"

    list_display = [
        'ticket_id',
        'subject',
        'ticket_type',
        'status',
        'priority',
        'get_user_display',
        'get_assigned_to_display',
        'has_attachment',
        'created_at',
    ]
    list_filter = [
        ('ticket_type', admin.ChoicesFieldListFilter),
        'status',
        'priority',
        'created_at',
    ]
    search_fields = [
        'ticket_id',
        'subject',
        'description',
        'user__email',
        'public_email',
        'public_name',
    ]
    readonly_fields = [
        'ticket_id',
        'created_at',
        'updated_at',
        'resolved_at',
        'closed_at',
    ]
    fieldsets = (
        ('Basic Information', {
            'fields': ('ticket_id', 'user', 'public_name', 'public_email', 'public_phone')
        }),
        ('Ticket Details', {
            'fields': ('ticket_type', 'subject', 'description', 'attachment', 'priority')
        }),
        ('Related Items', {
            'fields': ('related_equipment', 'related_booking')
        }),
        ('Status & Assignment', {
            'fields': ('status', 'assigned_to', 'resolution_notes')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at', 'resolved_at', 'closed_at'),
            'classes': ('collapse',)
        }),
    )
    inlines = [
        TicketCommentInline,
    ]


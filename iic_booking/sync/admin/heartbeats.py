"""Agent Heartbeat admin — readonly diagnostics."""

from __future__ import annotations

from datetime import timedelta

from django.contrib import admin
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from iic_booking.sync.models import AgentHeartbeat

from .constants import heartbeat_timeout_seconds
from .filters import AgentDepartmentFilter, HeartbeatTimeRangeFilter
from .helpers import color_badge
from .scoping import resolve_sync_admin_scope, scope_by_agent_department


@admin.register(AgentHeartbeat)
class AgentHeartbeatAdmin(admin.ModelAdmin):
    list_display = (
        "sync_agent",
        "reported_at",
        "stale_badge",
        "cpu_percent",
        "memory_percent",
        "disk_percent",
        "active_workers",
        "queue_size",
        "reported_configuration_version",
        "reported_schema_version",
        "hostname",
        "service_version",
    )
    list_filter = (AgentDepartmentFilter, "sync_agent", HeartbeatTimeRangeFilter, "reported_at")
    search_fields = (
        "sync_agent__agent_name",
        "hostname",
        "service_version",
        "windows_build",
        "sqlite_schema_version",
        "status_message",
    )
    readonly_fields = (
        "sync_agent",
        "reported_at",
        "cpu_percent",
        "memory_percent",
        "disk_percent",
        "queue_size",
        "active_workers",
        "last_upload_at",
        "agent_uptime_seconds",
        "service_version",
        "sqlite_schema_version",
        "windows_build",
        "hostname",
        "reported_configuration_version",
        "reported_schema_version",
        "status_message",
        "details",
        "created_at",
        "stale_badge",
    )
    ordering = ("-reported_at",)
    list_select_related = ("sync_agent", "sync_agent__department")
    raw_id_fields = ("sync_agent",)
    date_hierarchy = "reported_at"

    fieldsets = (
        (_("Agent"), {"fields": ("sync_agent", "reported_at", "stale_badge", "status_message")}),
        (
            _("Resources"),
            {"fields": ("cpu_percent", "memory_percent", "disk_percent", "queue_size", "active_workers")},
        ),
        (
            _("Deployment"),
            {
                "fields": (
                    "hostname",
                    "service_version",
                    "sqlite_schema_version",
                    "windows_build",
                    "agent_uptime_seconds",
                )
            },
        ),
        (
            _("Versions"),
            {"fields": ("reported_configuration_version", "reported_schema_version", "last_upload_at")},
        ),
        (_("Payload"), {"fields": ("details", "created_at"), "classes": ("collapse",)}),
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related("sync_agent", "sync_agent__department")
        return scope_by_agent_department(qs, resolve_sync_admin_scope(request))

    @admin.display(description=_("Freshness"))
    def stale_badge(self, obj):
        cutoff = timezone.now() - timedelta(seconds=heartbeat_timeout_seconds())
        if obj.reported_at >= cutoff:
            return color_badge(_("Fresh"), "#28a745")
        return color_badge(_("Stale"), "#dc3545")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return True  # view-only change form

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

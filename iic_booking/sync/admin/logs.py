"""Sync Log admin — enterprise event viewer."""

from __future__ import annotations

from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from iic_booking.sync.models import SyncLog

from .filters import AgentDepartmentFilter
from .helpers import severity_badge
from .scoping import resolve_sync_admin_scope, scope_by_agent_department


class SyncLogSeverityFilter(admin.SimpleListFilter):
    title = _("Severity")
    parameter_name = "severity"

    def lookups(self, request, model_admin):
        from iic_booking.sync.models import SyncLogSeverity

        return SyncLogSeverity.choices

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(severity=self.value())
        return queryset


class SyncLogCategoryFilter(admin.SimpleListFilter):
    title = _("Category")
    parameter_name = "category"

    def lookups(self, request, model_admin):
        from iic_booking.sync.models import SyncLogCategory

        return SyncLogCategory.choices

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(category=self.value())
        return queryset


@admin.register(SyncLog)
class SyncLogAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "event_code",
        "severity_badge_col",
        "category",
        "sync_agent",
        "equipment",
        "message_preview",
        "correlation_id",
    )
    list_filter = (
        SyncLogSeverityFilter,
        SyncLogCategoryFilter,
        AgentDepartmentFilter,
        "sync_agent",
        "equipment",
        "created_at",
    )
    search_fields = (
        "event_code",
        "message",
        "correlation_id",
        "sync_agent__agent_name",
        "equipment__code",
        "equipment__name",
    )
    readonly_fields = (
        "sync_agent",
        "equipment",
        "event_code",
        "severity",
        "category",
        "message",
        "json_payload",
        "correlation_id",
        "created_at",
        "severity_badge_col",
    )
    ordering = ("-created_at",)
    list_select_related = ("sync_agent", "equipment", "sync_agent__department")
    raw_id_fields = ("sync_agent", "equipment")
    date_hierarchy = "created_at"

    fieldsets = (
        (
            _("Event"),
            {
                "fields": (
                    "created_at",
                    "event_code",
                    "severity",
                    "severity_badge_col",
                    "category",
                    "correlation_id",
                )
            },
        ),
        (_("Scope"), {"fields": ("sync_agent", "equipment")}),
        (_("Message"), {"fields": ("message", "json_payload")}),
    )

    def get_queryset(self, request):
        qs = (
            super()
            .get_queryset(request)
            .select_related("sync_agent", "equipment", "sync_agent__department")
        )
        return scope_by_agent_department(qs, resolve_sync_admin_scope(request))

    @admin.display(description=_("Severity"), ordering="severity")
    def severity_badge_col(self, obj):
        return severity_badge(obj.severity, obj.get_severity_display())

    @admin.display(description=_("Message"))
    def message_preview(self, obj):
        text = obj.message or ""
        return text if len(text) <= 80 else text[:77] + "…"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return True

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

"""Laboratory admin."""

from __future__ import annotations

from django.contrib import admin
from django.db.models import Count
from django.utils.translation import gettext_lazy as _

from iic_booking.sync.models import Laboratory

from .filters import DepartmentFilter
from .scoping import resolve_sync_admin_scope


@admin.register(Laboratory)
class LaboratoryAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "department", "location", "is_active", "agent_count", "updated_at")
    list_filter = (DepartmentFilter, "is_active")
    search_fields = ("name", "code", "location", "department__name")
    autocomplete_fields = ("department",)
    readonly_fields = ("id", "created_at", "updated_at")
    ordering = ("department__name", "name")

    fieldsets = (
        (_("Laboratory"), {"fields": ("name", "code", "department", "location", "description", "is_active")}),
        (_("System"), {"fields": ("id", "created_at", "updated_at"), "classes": ("collapse",)}),
    )

    def get_queryset(self, request):
        qs = (
            super()
            .get_queryset(request)
            .select_related("department")
            .annotate(_agent_count=Count("sync_agents"))
        )
        scope = resolve_sync_admin_scope(request)
        if scope.is_full_access:
            return qs
        if scope.department_id is not None:
            return qs.filter(department_id=scope.department_id)
        return qs.none()

    @admin.display(description=_("Agents"), ordering="_agent_count")
    def agent_count(self, obj):
        return getattr(obj, "_agent_count", 0)

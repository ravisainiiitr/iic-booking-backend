"""List filters for the Department Sync Operations Console."""

from __future__ import annotations

from datetime import timedelta

from django.contrib import admin
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from iic_booking.sync.models import AgentLifecycleStatus, Laboratory
from iic_booking.users.models.department import Department, DepartmentType

from .constants import heartbeat_timeout_seconds


class DepartmentFilter(admin.SimpleListFilter):
    title = _("Department")
    parameter_name = "department"

    def lookups(self, request, model_admin):
        return list(
            Department.objects.filter(department_type=DepartmentType.INTERNAL)
            .order_by("name")
            .values_list("id", "name")
        )

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(department_id=self.value())
        return queryset


class AgentDepartmentFilter(admin.SimpleListFilter):
    title = _("Department")
    parameter_name = "agent_department"

    def lookups(self, request, model_admin):
        return list(
            Department.objects.filter(department_type=DepartmentType.INTERNAL)
            .order_by("name")
            .values_list("id", "name")
        )

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(sync_agent__department_id=self.value())
        return queryset


class ProfileDepartmentFilter(admin.SimpleListFilter):
    title = _("Department")
    parameter_name = "profile_department"

    def lookups(self, request, model_admin):
        return list(
            Department.objects.filter(department_type=DepartmentType.INTERNAL)
            .order_by("name")
            .values_list("id", "name")
        )

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(equipment__internal_department_id=self.value())
        return queryset


class LaboratoryFilter(admin.SimpleListFilter):
    title = _("Laboratory")
    parameter_name = "laboratory"

    def lookups(self, request, model_admin):
        return list(Laboratory.objects.order_by("name").values_list("id", "name"))

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(laboratory_id=self.value())
        return queryset


class LifecycleFilter(admin.SimpleListFilter):
    title = _("Lifecycle")
    parameter_name = "lifecycle"

    def lookups(self, request, model_admin):
        return AgentLifecycleStatus.choices

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(status=self.value())
        return queryset


class OnlineStatusFilter(admin.SimpleListFilter):
    title = _("Online")
    parameter_name = "online"

    def lookups(self, request, model_admin):
        return [("1", _("Online")), ("0", _("Offline"))]

    def queryset(self, request, queryset):
        cutoff = timezone.now() - timedelta(seconds=heartbeat_timeout_seconds())
        if self.value() == "1":
            return queryset.filter(last_heartbeat_at__gte=cutoff)
        if self.value() == "0":
            return queryset.filter(
                models_q_offline(cutoff)
            )
        return queryset


def models_q_offline(cutoff):
    from django.db.models import Q

    return Q(last_heartbeat_at__isnull=True) | Q(last_heartbeat_at__lt=cutoff)


class ProfileAgentFilter(admin.SimpleListFilter):
    title = _("Assigned agent")
    parameter_name = "assigned_agent"

    def lookups(self, request, model_admin):
        from iic_booking.sync.models import DepartmentSyncAgent

        return list(
            DepartmentSyncAgent.objects.order_by("agent_name").values_list("id", "agent_name")[:500]
        )

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(
                assignments__is_active=True,
                assignments__sync_agent_id=self.value(),
            ).distinct()
        return queryset


class HeartbeatTimeRangeFilter(admin.SimpleListFilter):
    title = _("Time range")
    parameter_name = "time_range"

    def lookups(self, request, model_admin):
        return [
            ("1h", _("Last hour")),
            ("24h", _("Last 24 hours")),
            ("7d", _("Last 7 days")),
            ("stale", _("Stale only")),
        ]

    def queryset(self, request, queryset):
        now = timezone.now()
        value = self.value()
        if value == "1h":
            return queryset.filter(reported_at__gte=now - timedelta(hours=1))
        if value == "24h":
            return queryset.filter(reported_at__gte=now - timedelta(hours=24))
        if value == "7d":
            return queryset.filter(reported_at__gte=now - timedelta(days=7))
        if value == "stale":
            cutoff = now - timedelta(seconds=heartbeat_timeout_seconds())
            return queryset.filter(reported_at__lt=cutoff)
        return queryset

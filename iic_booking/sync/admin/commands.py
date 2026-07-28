"""Minimal admin for Agent Command Framework and workspaces."""

from __future__ import annotations

from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from iic_booking.sync.models import AgentCommand, BookingWorkspace


@admin.register(AgentCommand)
class AgentCommandAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "command_type",
        "sync_agent",
        "priority",
        "status",
        "equipment",
        "booking",
        "created_at",
        "updated_at",
    )
    list_filter = ("status", "priority", "command_type", "created_at")
    search_fields = ("id", "command_type", "sync_agent__agent_name", "correlation_id", "last_error")
    readonly_fields = (
        "id",
        "created_at",
        "updated_at",
        "started_at",
        "completed_at",
        "version",
    )
    raw_id_fields = ("sync_agent", "equipment", "booking", "created_by")
    ordering = ("-created_at",)


@admin.register(BookingWorkspace)
class BookingWorkspaceAdmin(admin.ModelAdmin):
    list_display = (
        "workspace_name",
        "sync_agent",
        "booking",
        "equipment",
        "status",
        "configuration_version",
        "updated_at",
    )
    list_filter = ("status", "updated_at")
    search_fields = ("workspace_name", "relative_folder", "booking__virtual_booking_id")
    readonly_fields = ("id", "created_at", "updated_at", "version")
    raw_id_fields = ("sync_agent", "booking", "equipment")
    ordering = ("-created_at",)

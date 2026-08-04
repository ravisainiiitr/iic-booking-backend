"""Django admin for Lab Infrastructure models."""

from django.contrib import admin

from iic_booking.lab_infrastructure.models import (
    ConfigurationAck,
    ConfigurationChange,
    LabAlert,
    LabAuditEvent,
    LabRepairAction,
    SatDefect,
    SatEvidence,
    SatTestCase,
    SatTestResult,
    SatTestRun,
)


@admin.register(ConfigurationChange)
class ConfigurationChangeAdmin(admin.ModelAdmin):
    list_display = ("sync_profile", "configuration_version", "reason", "applied_by", "created_at")
    list_filter = ("created_at",)


@admin.register(ConfigurationAck)
class ConfigurationAckAdmin(admin.ModelAdmin):
    list_display = ("sync_agent", "equipment_pc_id", "configuration_version", "status", "applied_at")
    list_filter = ("status",)


@admin.register(LabRepairAction)
class LabRepairActionAdmin(admin.ModelAdmin):
    list_display = ("node_kind", "node_id", "action", "status", "requested_by", "created_at")
    list_filter = ("action", "status")


@admin.register(LabAuditEvent)
class LabAuditEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "node_kind", "node_id", "success", "created_at")
    list_filter = ("event_type", "success")


@admin.register(LabAlert)
class LabAlertAdmin(admin.ModelAdmin):
    list_display = ("code", "severity", "status", "title", "node_id", "created_at")
    list_filter = ("severity", "status", "code")
    actions = ("resolve_selected",)

    @admin.action(description="Resolve selected alerts")
    def resolve_selected(self, request, queryset):
        from django.utils import timezone

        queryset.update(status=LabAlert.Status.RESOLVED, resolved_at=timezone.now())


@admin.register(SatTestCase)
class SatTestCaseAdmin(admin.ModelAdmin):
    list_display = ("test_id", "stage", "execution_order", "suite", "module", "feature", "severity", "is_active")
    list_filter = ("suite", "stage", "module", "severity", "is_active")
    search_fields = ("test_id", "feature", "module")


@admin.register(SatTestRun)
class SatTestRunAdmin(admin.ModelAdmin):
    list_display = ("name", "suite", "status", "recommendation", "executed_by", "started_at", "finished_at")
    list_filter = ("status", "suite", "recommendation")


@admin.register(SatTestResult)
class SatTestResultAdmin(admin.ModelAdmin):
    list_display = ("run", "test_case", "status", "executed_at")
    list_filter = ("status",)
    search_fields = ("test_case__test_id", "actual_result")


@admin.register(SatEvidence)
class SatEvidenceAdmin(admin.ModelAdmin):
    list_display = ("title", "kind", "run", "result", "original_name", "created_at")
    list_filter = ("kind",)


@admin.register(SatDefect)
class SatDefectAdmin(admin.ModelAdmin):
    list_display = ("title", "kind", "severity", "status", "test_id", "run", "created_at")
    list_filter = ("kind", "severity", "status")

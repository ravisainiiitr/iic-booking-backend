from django.contrib import admin

from .models import MaintenancePlan, PlanCompletion, ReliabilityAlert, UpkeepSetting


@admin.register(MaintenancePlan)
class MaintenancePlanAdmin(admin.ModelAdmin):
    list_display = ("title", "equipment", "kind", "interval_days", "next_due_on", "is_active")
    list_filter = ("kind", "is_active")
    raw_id_fields = ("equipment", "responsible", "created_by")


@admin.register(ReliabilityAlert)
class ReliabilityAlertAdmin(admin.ModelAdmin):
    list_display = ("equipment", "kind", "severity", "opened_at", "resolved_at")
    list_filter = ("kind", "severity")
    raw_id_fields = ("equipment", "acknowledged_by")


admin.site.register(UpkeepSetting)
admin.site.register(PlanCompletion)

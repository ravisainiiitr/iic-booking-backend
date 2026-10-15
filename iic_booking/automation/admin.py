from django.contrib import admin

from .models import AutomationActionLog, AutomationSettings, TicketTriage


@admin.register(AutomationSettings)
class AutomationSettingsAdmin(admin.ModelAdmin):
    list_display = ("pk", "instrument_finder_enabled", "smart_slots_enabled", "ticket_triage_enabled",
                    "oic_alerts_enabled", "no_show_prompts_enabled", "updated_at")


@admin.register(AutomationActionLog)
class AutomationActionLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "kind", "action", "target_type", "target_id", "summary")
    list_filter = ("kind", "action")
    readonly_fields = [f.name for f in AutomationActionLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(TicketTriage)
class TicketTriageAdmin(admin.ModelAdmin):
    list_display = ("ticket", "category", "route_role", "confidence", "status", "created_at")
    list_filter = ("status", "category", "route_role")
    raw_id_fields = ("ticket", "suggested_assignee", "equipment", "decided_by")

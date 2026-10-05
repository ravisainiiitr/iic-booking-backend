"""Read-only Django admin: every change goes through the audited service layer (API or management command)."""

from django.contrib import admin

from .models import DepartmentModuleAuditLog, DepartmentModuleSetting


class ReadOnlyAdmin(admin.ModelAdmin):
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DepartmentModuleSetting)
class DepartmentModuleSettingAdmin(ReadOnlyAdmin):
    list_display = ("department", "module_key", "enabled", "test_users_only", "source", "updated_by", "updated_at")
    list_filter = ("module_key", "enabled", "test_users_only", "source")
    search_fields = ("department__name", "department__code")


@admin.register(DepartmentModuleAuditLog)
class DepartmentModuleAuditLogAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "department_label", "module_key", "action", "actor", "reason")
    list_filter = ("module_key", "action")
    search_fields = ("department_label", "reason")

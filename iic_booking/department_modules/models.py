"""Per-department module switches.

One row per (department, module) for DSA, Remote Analysis and Training. Procurement & Assets is configured by
``procurement_management.ProcurementManagementConfiguration`` and only surfaced here.

The seeding migration writes a row for every department that existed when the switches were installed and records
that moment in ``DepartmentModulesInstallation``. Departments created afterwards start off (rows are created when the
department is created; a later department that somehow has no row is treated as off too). A department without a row
that predates the installation (or any department while the switches are not installed) keeps the behaviour it had
before these switches existed (global and per-equipment flags only).

Rows are changed only through ``services.set_module`` (Main Administrator, reason required, audited), the initial
seeding or the new-department default, and are never deleted by application code.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from iic_booking.procurement_management.models import AppendOnlyModel, ImmutableRecordError

from .constants import LOCAL_MODULE_CHOICES, ModuleKey

USER = settings.AUTH_USER_MODEL


class DepartmentModuleSettingQuerySet(models.QuerySet):
    def delete(self):
        raise ImmutableRecordError("Department module settings are never deleted; switch them off instead.")


class SettingSource(models.TextChoices):
    SEED = "seed", "Initial state from existing data"
    ADMIN = "admin", "Main Administrator"
    NEW_DEPARTMENT = "new", "New department (starts off)"


class DepartmentModulesInstallation(models.Model):
    """Single row (pk=1) written by the seeding migration: departments created from ``installed_at`` on start off."""

    installed_at = models.DateTimeField()

    def delete(self, *args, **kwargs):
        raise ImmutableRecordError("The department modules installation record is never deleted.")

    def __str__(self) -> str:
        return f"department modules installed at {self.installed_at.isoformat()}"


class DepartmentModuleSetting(models.Model):
    department = models.ForeignKey("users.Department", on_delete=models.CASCADE, related_name="module_settings")
    module_key = models.CharField(max_length=32, choices=LOCAL_MODULE_CHOICES)
    enabled = models.BooleanField(default=False)
    test_users_only = models.BooleanField(default=False)
    disabled_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the switch was last turned off. Work started before this (e.g. earlier bookings) may finish.",
    )
    test_only_since = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When test-users-only was last turned on. Work started before this may finish.",
    )
    source = models.CharField(max_length=10, choices=SettingSource.choices, default=SettingSource.ADMIN)
    seed_note = models.CharField(max_length=255, blank=True, default="")
    updated_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = DepartmentModuleSettingQuerySet.as_manager()

    class Meta:
        ordering = ["department_id", "module_key"]
        constraints = [
            models.UniqueConstraint(fields=["department", "module_key"], name="department_module_setting_unique"),
        ]

    def __str__(self) -> str:
        state = "on" if self.enabled else "off"
        if self.enabled and self.test_users_only:
            state = "test users only"
        return f"{self.department_id}:{self.module_key}={state}"

    def delete(self, *args, **kwargs):
        raise ImmutableRecordError("Department module settings are never deleted; switch them off instead.")


class DepartmentModuleAuditLog(AppendOnlyModel):
    department = models.ForeignKey(
        "users.Department", on_delete=models.SET_NULL, null=True, blank=True, related_name="module_audit_logs"
    )
    department_label = models.CharField(max_length=255, blank=True, default="")
    module_key = models.CharField(max_length=32, choices=ModuleKey.choices)
    actor = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    action = models.CharField(max_length=40)
    old_value = models.JSONField(default=dict, blank=True)
    new_value = models.JSONField(default=dict, blank=True)
    reason = models.TextField(blank=True, default="")
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["department", "module_key", "created_at"]),
            models.Index(fields=["created_at"]),
        ]

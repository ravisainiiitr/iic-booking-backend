"""Append-only history of equipment configuration changes (see ``config_audit``)."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class ConfigAuditImmutable(Exception):
    """Raised when code tries to change or delete an audit row."""


class _AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ConfigAuditImmutable("Equipment configuration audit rows are append-only.")

    def delete(self):
        raise ConfigAuditImmutable("Equipment configuration audit rows are append-only.")


class ConfigChangeAction(models.TextChoices):
    CREATED = "CREATED", _("Created")
    UPDATED = "UPDATED", _("Updated")
    DELETED = "DELETED", _("Deleted")


class ConfigChangeSource(models.TextChoices):
    DASHBOARD = "DASHBOARD", _("Dashboard")
    DJANGO_ADMIN = "DJANGO_ADMIN", _("Django Admin")
    API = "API", _("API")
    SCHEDULED_TASK = "SCHEDULED_TASK", _("Scheduled task")
    MANAGEMENT_COMMAND = "MANAGEMENT_COMMAND", _("Management command")
    SYSTEM = "SYSTEM", _("System process")


class EquipmentConfigAuditLog(models.Model):
    """One row per changed setting. Equipment is stored by id and name so rows outlive the equipment."""

    objects = _AppendOnlyQuerySet.as_manager()

    equipment_id = models.IntegerField(db_index=True, verbose_name=_("Equipment ID"))
    equipment_code = models.CharField(max_length=100, blank=True, default="")
    equipment_name = models.CharField(max_length=255, blank=True, default="")
    object_type = models.CharField(
        max_length=64,
        default="Equipment",
        help_text=_("Equipment, or the related setting record (e.g. Slot Master, Charge Profile)."),
    )
    object_id = models.CharField(max_length=64, blank=True, default="")
    object_label = models.CharField(max_length=255, blank=True, default="")
    action = models.CharField(max_length=10, choices=ConfigChangeAction.choices)
    setting = models.CharField(max_length=100, db_index=True, help_text=_("Field name"))
    setting_label = models.CharField(max_length=255, blank=True, default="")
    previous_value = models.TextField(blank=True, default="")
    new_value = models.TextField(blank=True, default="")
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    changed_by_label = models.CharField(max_length=255, blank=True, default="")
    changed_by_role = models.CharField(max_length=64, blank=True, default="")
    changed_by_department = models.CharField(max_length=255, blank=True, default="")
    is_system = models.BooleanField(default=False)
    source = models.CharField(max_length=24, choices=ConfigChangeSource.choices, default=ConfigChangeSource.SYSTEM)
    process = models.CharField(max_length=255, blank=True, default="", help_text=_("Task, command or endpoint"))
    reference = models.CharField(max_length=128, blank=True, default="", help_text=_("Request or transaction id"))
    changed_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Equipment configuration change")
        verbose_name_plural = _("Equipment configuration audit trail")
        ordering = ["-changed_at", "-id"]
        indexes = [
            models.Index(fields=["equipment_id", "-changed_at"], name="eq_cfg_audit_eq_time"),
            models.Index(fields=["changed_by", "-changed_at"], name="eq_cfg_audit_user_time"),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ConfigAuditImmutable("Equipment configuration audit rows are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ConfigAuditImmutable("Equipment configuration audit rows are append-only.")

    def __str__(self):
        return f"{self.equipment_code or self.equipment_id} {self.setting} {self.action} @ {self.changed_at}"

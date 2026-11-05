"""Append-only history of everything that happens to requests on OIC-only equipment (see ``oic_request_audit``)."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class OicRequestAuditImmutable(Exception):
    """Raised when code tries to change or delete an audit row."""


class _AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise OicRequestAuditImmutable("OIC request audit rows are append-only.")

    def delete(self):
        raise OicRequestAuditImmutable("OIC request audit rows are append-only.")


class OicRequestAuditAction(models.TextChoices):
    RAISED = "RAISED", _("Request raised")
    REFUSED = "REFUSED", _("Attempt refused")
    EVENT = "EVENT", _("Request updated")


class OicRequestAuditLog(models.Model):
    """One row per attempt or booking event. Everything is copied in, so rows outlive the booking and equipment."""

    objects = _AppendOnlyQuerySet.as_manager()

    equipment_id = models.IntegerField(db_index=True, verbose_name=_("Equipment ID"))
    equipment_code = models.CharField(max_length=100, blank=True, default="")
    equipment_name = models.CharField(max_length=255, blank=True, default="")
    department_id = models.IntegerField(null=True, blank=True, db_index=True, verbose_name=_("Department ID"))
    department_name = models.CharField(max_length=255, blank=True, default="")
    action = models.CharField(max_length=10, choices=OicRequestAuditAction.choices, db_index=True)
    booking_id = models.IntegerField(null=True, blank=True, db_index=True, verbose_name=_("Booking ID"))
    booking_reference = models.CharField(max_length=64, blank=True, default="")
    event_type = models.CharField(max_length=32, blank=True, default="")
    previous_status = models.CharField(max_length=30, blank=True, default="")
    new_status = models.CharField(max_length=30, blank=True, default="")
    route = models.CharField(max_length=64, blank=True, default="", help_text=_("How the attempt was made"))
    detail = models.TextField(blank=True, default="", help_text=_("Refusal reason or event comment"))
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    actor_label = models.CharField(max_length=255, blank=True, default="")
    actor_email = models.CharField(max_length=255, blank=True, default="")
    actor_role = models.CharField(max_length=64, blank=True, default="")
    actor_department = models.CharField(max_length=255, blank=True, default="")
    is_system = models.BooleanField(default=False)
    source = models.CharField(max_length=128, blank=True, default="")
    ip_address = models.CharField(max_length=64, blank=True, default="")
    user_agent = models.CharField(max_length=512, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("OIC request audit entry")
        verbose_name_plural = _("OIC request audit trail")
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["equipment_id", "-created_at"], name="oic_req_audit_eq_time"),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise OicRequestAuditImmutable("OIC request audit rows are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise OicRequestAuditImmutable("OIC request audit rows are append-only.")

    def __str__(self):
        return f"{self.equipment_code or self.equipment_id} {self.action} @ {self.created_at}"

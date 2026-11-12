"""Append-only record of operations that otherwise leave no row behind: waitlist entries are deleted when cleared or served, and
an urgent request can be deleted. Ids and names are copies, not foreign keys, so the record outlives the user or equipment."""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class OperationsAuditImmutable(Exception):
    """Raised when code tries to change or delete an audit row."""


class _AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise OperationsAuditImmutable("Operations audit rows are append-only.")

    def delete(self):
        raise OperationsAuditImmutable("Operations audit rows are append-only.")


class OperationsAuditEvent(models.Model):
    class Action(models.TextChoices):
        WAITLIST_AUTO_CLEARED = "waitlist_auto_cleared", _("Waitlist cleared automatically")
        WAITLIST_STAFF_CLEARED = "waitlist_staff_cleared", _("Waitlist cleared by staff")
        WAITLIST_AUTO_CONFIRMED = "waitlist_auto_confirmed", _("Waitlist entry booked automatically")
        WAITLIST_STAFF_CONFIRMED = "waitlist_staff_confirmed", _("Waitlist entry booked by staff")
        WAITLIST_CANNOT_FULFILL = "waitlist_cannot_fulfill", _("Automatic booking failed")
        WAITLIST_USER_WITHDREW = "waitlist_user_withdrew", _("User left the waitlist")
        WAITLIST_USER_BOOKED = "waitlist_user_booked", _("Removed: user got a booking")
        URGENT_DELETED = "urgent_deleted", _("Urgent request deleted")

    action = models.CharField(max_length=32, choices=Action.choices, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    is_system = models.BooleanField(default=False)
    actor_id = models.BigIntegerField(null=True, blank=True)
    actor_name = models.CharField(max_length=255, blank=True, default="")
    actor_role = models.CharField(max_length=60, blank=True, default="")
    equipment_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    equipment_name = models.CharField(max_length=255, blank=True, default="")
    subject_user_id = models.BigIntegerField(null=True, blank=True)
    subject_user_name = models.CharField(max_length=255, blank=True, default="")
    booking_ref = models.CharField(max_length=64, blank=True, default="")
    detail = models.TextField(blank=True, default="")

    objects = _AppendOnlyQuerySet.as_manager()

    class Meta:
        verbose_name = _("Operations audit event")
        verbose_name_plural = _("Operations audit events")
        ordering = ["-created_at", "-id"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise OperationsAuditImmutable("Operations audit rows are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise OperationsAuditImmutable("Operations audit rows are append-only.")

    def __str__(self) -> str:
        return f"{self.action} #{self.pk}"

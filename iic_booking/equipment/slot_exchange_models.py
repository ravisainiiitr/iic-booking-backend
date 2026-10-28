"""Slot Exchange: two internal users agree to swap booked slots of the same charge (see ``slot_exchange``)."""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _

from iic_booking.users.models.user import User  # noqa: F401  (string FKs below resolve through the app registry)


class SlotExchangeStatus(models.TextChoices):
    PENDING = "PENDING", _("Awaiting response")
    ACCEPTED = "ACCEPTED", _("Exchanged")
    DECLINED = "DECLINED", _("Declined")
    CANCELLED = "CANCELLED", _("Withdrawn")
    EXPIRED = "EXPIRED", _("Expired")
    FAILED = "FAILED", _("Could not be completed")


class SlotExchangeRequest(models.Model):
    """A request from ``requester`` to give ``offered_slots`` (their booking) for ``requested_slots`` (the
    responder's booking). The swap happens only when the responder accepts and every rule still holds."""

    requester = models.ForeignKey(User, on_delete=models.CASCADE, related_name="slot_exchanges_sent")
    responder = models.ForeignKey(User, on_delete=models.CASCADE, related_name="slot_exchanges_received")
    requester_booking = models.ForeignKey(
        "equipment.Booking", on_delete=models.CASCADE, related_name="slot_exchanges_offered"
    )
    responder_booking = models.ForeignKey(
        "equipment.Booking", on_delete=models.CASCADE, related_name="slot_exchanges_requested"
    )
    offered_slots = models.ManyToManyField("equipment.DailySlot", related_name="+")
    requested_slots = models.ManyToManyField("equipment.DailySlot", related_name="+")
    # Slot times at request time: the slots keep their rows after the swap, so history shows what was agreed.
    offered_summary = models.JSONField(default=list, blank=True)
    requested_summary = models.JSONField(default=list, blank=True)
    status = models.CharField(
        max_length=16, choices=SlotExchangeStatus.choices, default=SlotExchangeStatus.PENDING, db_index=True
    )
    message = models.TextField(blank=True, default="")
    response_note = models.TextField(blank=True, default="")
    failure_reason = models.TextField(blank=True, default="")
    expires_at = models.DateTimeField(help_text=_("5 minutes before the earliest slot in the exchange starts."))
    created_at = models.DateTimeField(auto_now_add=True)
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        verbose_name = _("Slot exchange request")
        verbose_name_plural = _("Slot exchange requests")
        indexes = [
            models.Index(fields=["responder", "status"], name="slotx_responder_status"),
            models.Index(fields=["requester", "status"], name="slotx_requester_status"),
        ]

    def __str__(self):
        return f"Slot exchange #{self.pk}: booking {self.requester_booking_id} ↔ {self.responder_booking_id}"

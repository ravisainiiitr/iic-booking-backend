"""In-app internet voice calls between portal users and lab staff (WebRTC peer to peer; no audio is stored).

``CallSession`` holds one call and its outcome, ``CallEvent`` the minimal audit trail of state changes, and
``CallDevice`` the push tokens of a user's apps (Android FCM, iOS VoIP) used to ring them while the app is closed.
"""

from __future__ import annotations

import uuid
from datetime import time

from django.conf import settings
from django.db import models


class CallRole(models.TextChoices):
    OPERATOR = "operator", "Lab Operator"
    OIC = "oic", "Officer In Charge"
    ACCOUNTS = "accounts", "Accounts In Charge"
    USER = "user", "Booking user"


class CallContext(models.TextChoices):
    BOOKING = "booking", "Booking"
    EQUIPMENT = "equipment", "Equipment"
    INVOICE = "invoice", "Invoice or payment"


class CallStatus(models.TextChoices):
    RINGING = "ringing", "Ringing"
    ACCEPTED = "accepted", "In call"
    ENDED = "ended", "Completed"
    DECLINED = "declined", "Declined"
    MISSED = "missed", "No answer"
    BUSY = "busy", "Busy"
    CANCELLED = "cancelled", "Cancelled"
    FAILED = "failed", "Failed"


ACTIVE_STATUSES = (CallStatus.RINGING, CallStatus.ACCEPTED)


class CallSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    caller = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="calls_made")
    callee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="calls_received")
    role = models.CharField(max_length=20, choices=CallRole.choices)
    context_type = models.CharField(max_length=20, choices=CallContext.choices)
    context_id = models.PositiveIntegerField()
    context_label = models.CharField(max_length=200, blank=True)
    reason = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=20, choices=CallStatus.choices, default=CallStatus.RINGING, db_index=True)
    failure_code = models.CharField(max_length=50, blank=True)
    caller_platform = models.CharField(max_length=20, blank=True)
    callee_platform = models.CharField(max_length=20, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    answered_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["callee", "status"]), models.Index(fields=["caller", "created_at"])]

    def __str__(self):
        return f"Call {self.id} {self.status}"

    @property
    def duration_seconds(self) -> int:
        if self.answered_at and self.ended_at:
            return max(0, int((self.ended_at - self.answered_at).total_seconds()))
        return 0


class CallEvent(models.Model):
    session = models.ForeignKey(CallSession, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=30)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    detail = models.CharField(max_length=200, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["at", "pk"]


class DevicePlatform(models.TextChoices):
    ANDROID = "android", "Android (FCM)"
    IOS = "ios", "iOS (VoIP push)"


class CallDevice(models.Model):
    """A push token of one of the user's app installs. The token itself is never logged or returned."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="call_devices")
    platform = models.CharField(max_length=10, choices=DevicePlatform.choices)
    device_id = models.CharField(max_length=100, help_text="App-generated install id (one row per install)")
    token = models.TextField()
    app_version = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["platform", "device_id"], name="uniq_call_device_install")]


class CallSilentHours(models.Model):
    """Per instrument, set by its Officer In Charge: when users cannot call its staff (see calls/silent.py)."""

    equipment = models.OneToOneField("equipment.Equipment", on_delete=models.CASCADE, related_name="call_silent_hours")
    enabled = models.BooleanField(default=False)
    office_start = models.TimeField(default=time(9, 0))
    office_end = models.TimeField(default=time(17, 30))
    silent_weekends = models.BooleanField(default=True)
    silent_holidays = models.BooleanField(default=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Call silent hours"
        verbose_name_plural = "Call silent hours"

    def __str__(self) -> str:
        return f"Silent hours for equipment {self.equipment_id}: {'on' if self.enabled else 'off'}"

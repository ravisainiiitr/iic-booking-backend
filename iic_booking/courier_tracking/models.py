"""Courier tracking and sample arrival records.

* ``SampleArrival`` - how a booking's sample will reach the lab: MANUAL (brought in person) or COURIER (with courier
  and tracking number). One active row per booking; switching mode supersedes the old row, so history is kept.
  Internal users only ever get MANUAL rows. A courier row is never "received": physical receipt stays in the sample
  trace (``BookingSampleTrace``) and is recorded by lab staff.
* ``TrackingLookup`` - ledger of every request to the TrackCourier.io tracking endpoint (never the API key).
* ``TrackingQuotaMonth`` - per-month counter rows; the row lock serialises quota reservations.
* ``Courier`` - the supported-courier catalog as last returned by ``GET /v1/couriers``.
* ``TrackingWebhookEvent`` - signature-verified webhook deliveries, stored before they are acknowledged.
* ``ShipmentNotification`` / ``ShipmentReminder`` - idempotency ledgers for messages.
* ``CourierTrackingAlert`` - administrative warnings (quota, unresolved recipients, account problems).
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from .constants import ArrivalMode, LookupOutcome, LookupTrigger, ShipmentStatus

USER = settings.AUTH_USER_MODEL


class CourierTrackingSettings(models.Model):
    """Global integration settings (single row, pk=1). Changed only by the Main Administrator; changes are audited."""

    monthly_quota_fallback = models.PositiveIntegerField(
        default=100, help_text="Monthly tracking requests assumed until the provider reports its quota."
    )
    safety_reserve = models.PositiveIntegerField(
        default=10,
        help_text="Requests kept back each month for first lookups of new shipments and staff refreshes.",
    )
    alert_threshold_percent = models.PositiveSmallIntegerField(
        default=80, help_text="Alert the Main Administrator once usage reaches this share of the quota."
    )
    scheduled_interval_hours = models.PositiveIntegerField(
        default=48, help_text="Minimum time between scheduled checks of a shipment in transit."
    )
    priority_interval_hours = models.PositiveIntegerField(
        default=12, help_text="Minimum time between scheduled checks of a shipment out for delivery or with a problem."
    )
    user_refresh_interval_hours = models.PositiveIntegerField(
        default=12, help_text="Minimum time between refreshes a user can request for one shipment."
    )
    user_refresh_daily_limit = models.PositiveSmallIntegerField(
        default=3, help_text="Refreshes one user can request per day across all their shipments."
    )
    max_lookups_per_shipment = models.PositiveSmallIntegerField(
        default=12, help_text="Lookups after which a shipment is only checked when staff ask."
    )
    scheduled_lookups_enabled = models.BooleanField(
        default=True, help_text="Run scheduled checks (the free plan has no live webhooks)."
    )
    arrival_reminder_after_days = models.PositiveSmallIntegerField(
        default=2, help_text="Remind external users to choose an arrival method this many days after booking."
    )
    stalled_after_days = models.PositiveSmallIntegerField(
        default=5, help_text="A shipment whose status has not changed for this long counts as not progressing."
    )
    reminder_interval_days = models.PositiveSmallIntegerField(default=3)
    reminder_max_count = models.PositiveSmallIntegerField(default=3, help_text="Per reminder kind and booking.")
    user_status_emails_enabled = models.BooleanField(
        default=True, help_text="Send status emails to users who opted in."
    )
    staff_delivery_in_app = models.BooleanField(
        default=True, help_text="Also notify staff in the portal when a delivery is reported (email is always sent)."
    )
    catalog_refreshed_at = models.DateTimeField(null=True, blank=True)
    updated_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Courier tracking settings"
        verbose_name_plural = "Courier tracking settings"

    @classmethod
    def load(cls) -> "CourierTrackingSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self) -> str:
        return "Courier tracking settings"


class Courier(models.Model):
    """One supported courier from ``GET /v1/couriers``. Missing from the latest list -> inactive (kept for history)."""

    slug = models.CharField(max_length=64, primary_key=True)
    name = models.CharField(max_length=128)
    is_active = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.slug})"


class SampleArrival(models.Model):
    booking = models.ForeignKey("equipment.Booking", on_delete=models.CASCADE, related_name="sample_arrivals")
    user = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="sample_arrivals")
    department = models.ForeignKey(
        "users.Department", on_delete=models.PROTECT, null=True, blank=True, related_name="sample_arrivals",
        help_text="Equipment department when registered (tracking enablement is checked against it).",
    )
    equipment = models.ForeignKey(
        "equipment.Equipment", on_delete=models.PROTECT, related_name="sample_arrivals"
    )
    arrival_mode = models.CharField(max_length=16, choices=ArrivalMode.choices)
    is_active = models.BooleanField(default=True)
    superseded_at = models.DateTimeField(null=True, blank=True)
    superseded_reason = models.CharField(max_length=255, blank=True, default="")
    sample_identifiers = models.TextField(blank=True, default="")
    dispatch_date = models.DateField(null=True, blank=True)
    expected_arrival_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")
    email_updates = models.BooleanField(default=False, help_text="User asked for emails when the courier status changes.")

    # Courier shipments only (empty for MANUAL).
    courier_slug = models.CharField(max_length=64, blank=True, default="")
    courier_name = models.CharField(max_length=128, blank=True, default="")
    tracking_number_raw = models.CharField(max_length=64, blank=True, default="", help_text="As entered.")
    tracking_number = models.CharField(max_length=64, blank=True, default="", help_text="Normalised (no spaces, upper case).")
    status = models.CharField(max_length=24, choices=ShipmentStatus.choices, blank=True, default="")
    provider_state = models.CharField(max_length=32, blank=True, default="", help_text="Provider ShipmentState as returned.")
    provider_status_label = models.CharField(max_length=255, blank=True, default="")
    courier_has_no_record = models.BooleanField(default=False)
    checkpoints = models.JSONField(default=list, blank=True, help_text="Full known history, newest first.")
    latest_checkpoint = models.JSONField(default=dict, blank=True)
    status_changed_at = models.DateTimeField(null=True, blank=True)
    delivery_reported_at = models.DateTimeField(null=True, blank=True, help_text="When we learned of the delivery.")
    delivery_event_at = models.CharField(max_length=40, blank=True, default="", help_text="Courier's delivery date/time.")
    last_lookup_attempt_at = models.DateTimeField(null=True, blank=True)
    last_lookup_success_at = models.DateTimeField(null=True, blank=True)
    last_webhook_at = models.DateTimeField(null=True, blank=True)
    last_event_at = models.CharField(
        max_length=40, blank=True, default="",
        help_text="Newest provider event applied (webhook enqueued_at, or lookup time; ISO 8601 UTC). Older events "
        "are recorded but cannot overwrite newer state.",
    )
    next_lookup_at = models.DateTimeField(null=True, blank=True, help_text="Earliest next scheduled check; null: none.")
    lookup_count = models.PositiveIntegerField(default=0)
    last_problem = models.CharField(
        max_length=40, blank=True, default="", help_text="Last lookup outcome other than success (code only)."
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["booking"], condition=Q(is_active=True), name="courier_one_active_arrival_per_booking"
            ),
            models.CheckConstraint(
                name="courier_fields_match_arrival_mode",
                condition=(
                    Q(arrival_mode=ArrivalMode.MANUAL, courier_slug="", tracking_number="")
                    | (Q(arrival_mode=ArrivalMode.COURIER) & ~Q(courier_slug="") & ~Q(tracking_number=""))
                ),
            ),
        ]
        indexes = [
            models.Index(fields=["courier_slug", "tracking_number"]),
            models.Index(fields=["is_active", "arrival_mode", "next_lookup_at"]),
            models.Index(fields=["department", "status"]),
            models.Index(fields=["user", "created_at"]),
        ]

    @property
    def is_courier(self) -> bool:
        return self.arrival_mode == ArrivalMode.COURIER

    def __str__(self) -> str:
        what = f"{self.courier_slug} {self.tracking_number[-4:]}" if self.is_courier else "manual"
        return f"Arrival #{self.pk} booking {self.booking_id} ({what})"


class TrackingQuotaMonth(models.Model):
    """Usage for one calendar month (UTC). Locked for every reservation so concurrent requests cannot overspend."""

    period = models.CharField(max_length=7, primary_key=True, help_text="YYYY-MM (UTC).")
    in_flight = models.PositiveIntegerField(default=0)
    local_consumed = models.PositiveIntegerField(default=0, help_text="Requests counted locally (conservative).")
    local_requests = models.PositiveIntegerField(default=0)
    provider_used = models.PositiveIntegerField(null=True, blank=True)
    provider_quota = models.PositiveIntegerField(null=True, blank=True)
    provider_plan = models.CharField(max_length=32, blank=True, default="")
    provider_seen_at = models.DateTimeField(null=True, blank=True)
    blocked_outcome = models.CharField(max_length=40, blank=True, default="", help_text="Set while lookups are blocked.")
    blocked_at = models.DateTimeField(null=True, blank=True)
    alert_sent_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Courier tracking usage {self.period}"


class TrackingLookup(models.Model):
    correlation_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    arrival = models.ForeignKey(
        SampleArrival, on_delete=models.SET_NULL, null=True, blank=True, related_name="lookups"
    )
    period = models.CharField(max_length=7, db_index=True)
    trigger = models.CharField(max_length=16, choices=LookupTrigger.choices)
    requested_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    courier_slug = models.CharField(max_length=64)
    tracking_number_hash = models.CharField(max_length=16, help_text="Short SHA-256 of the number; not the number.")
    outcome = models.CharField(max_length=40, choices=LookupOutcome.choices, default=LookupOutcome.RESERVED)
    provider_error_code = models.CharField(max_length=40, blank=True, default="")
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    consumed = models.BooleanField(null=True, blank=True, help_text="Counted against the quota (null: unknown).")
    provider_used = models.PositiveIntegerField(null=True, blank=True)
    provider_quota = models.PositiveIntegerField(null=True, blank=True)
    provider_plan = models.CharField(max_length=32, blank=True, default="")
    shared_with = models.PositiveSmallIntegerField(
        default=0, help_text="Other active shipments with the same courier and number updated from this lookup."
    )
    reserved_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["-reserved_at"]
        indexes = [models.Index(fields=["period", "outcome"]), models.Index(fields=["arrival", "reserved_at"])]

    def __str__(self) -> str:
        return f"Lookup {self.correlation_id} {self.trigger} {self.outcome}"


class WebhookEventStatus(models.TextChoices):
    RECEIVED = "RECEIVED", "Received"
    PROCESSED = "PROCESSED", "Processed"
    UNMATCHED = "UNMATCHED", "No matching shipment"
    IGNORED = "IGNORED", "Ignored (test or unknown event)"
    FAILED = "FAILED", "Processing failed (will be retried)"


class TrackingWebhookEvent(models.Model):
    """A verified webhook body. Keyed by the body's SHA-256: an exact retry is the same row; anything else is new."""

    body_sha256 = models.CharField(max_length=64, unique=True)
    event = models.CharField(max_length=40)
    mode = models.CharField(max_length=8, blank=True, default="", help_text="X-Webhook-Mode (unsigned, informational).")
    courier_slug = models.CharField(max_length=64, blank=True, default="")
    tracking_number = models.CharField(max_length=64, blank=True, default="")
    enqueued_at = models.CharField(max_length=40, blank=True, default="")
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=16, choices=WebhookEventStatus.choices, default=WebhookEventStatus.RECEIVED)
    outcome = models.CharField(max_length=255, blank=True, default="")
    matched = models.PositiveSmallIntegerField(default=0)
    deliveries = models.PositiveIntegerField(default=1)
    attempts = models.PositiveSmallIntegerField(default=0)
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-received_at"]
        indexes = [models.Index(fields=["status", "received_at"]), models.Index(fields=["courier_slug", "tracking_number"])]

    def __str__(self) -> str:
        return f"{self.event} {self.courier_slug} ({self.status})"


class NotificationChannel(models.TextChoices):
    EMAIL = "EMAIL", "Email"
    IN_APP = "IN_APP", "In-app"


class DeliveryStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    SENT = "SENT", "Sent"
    FAILED = "FAILED", "Failed (will be retried)"
    GAVE_UP = "GAVE_UP", "Failed permanently"
    SKIPPED = "SKIPPED", "Skipped"


class ShipmentNotification(models.Model):
    """One message to one recipient about one state of one arrival. The unique key makes repeats no-ops."""

    arrival = models.ForeignKey(SampleArrival, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=40)
    state_key = models.CharField(max_length=64, help_text="What the message is about, e.g. the status it reports.")
    recipient = models.ForeignKey(USER, on_delete=models.CASCADE, related_name="+")
    channel = models.CharField(max_length=8, choices=NotificationChannel.choices)
    status = models.CharField(max_length=8, choices=DeliveryStatus.choices, default=DeliveryStatus.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.CharField(max_length=255, blank=True, default="")
    context = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["arrival", "kind", "state_key", "recipient", "channel"], name="courier_notification_once"
            )
        ]
        indexes = [models.Index(fields=["status", "created_at"])]


class ReminderKind(models.TextChoices):
    ARRIVAL_NOT_SELECTED = "ARRIVAL_NOT_SELECTED", "Arrival method not chosen"
    DETAILS_INCOMPLETE = "DETAILS_INCOMPLETE", "Courier details need attention"
    NOT_PROGRESSING = "NOT_PROGRESSING", "Shipment not progressing"
    DELIVERY_PROBLEM = "DELIVERY_PROBLEM", "Delivery attempt failed or courier reports a problem"


class ShipmentReminder(models.Model):
    booking = models.ForeignKey("equipment.Booking", on_delete=models.CASCADE, related_name="courier_reminders")
    arrival = models.ForeignKey(
        SampleArrival, on_delete=models.SET_NULL, null=True, blank=True, related_name="reminders"
    )
    kind = models.CharField(max_length=32, choices=ReminderKind.choices)
    sequence = models.PositiveSmallIntegerField()
    recipient = models.ForeignKey(USER, on_delete=models.CASCADE, related_name="+")
    status = models.CharField(max_length=8, choices=DeliveryStatus.choices, default=DeliveryStatus.PENDING)
    last_error = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["booking", "kind", "sequence"], name="courier_reminder_once")
        ]


class AlertKind(models.TextChoices):
    QUOTA_THRESHOLD = "QUOTA_THRESHOLD", "Quota threshold reached"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED", "Quota exhausted"
    ACCOUNT_PROBLEM = "ACCOUNT_PROBLEM", "API key or account problem"
    RECIPIENT_MISSING = "RECIPIENT_MISSING", "Required notification recipient not found"
    NOTIFICATION_FAILED = "NOTIFICATION_FAILED", "Notification failed permanently"
    CATALOG_UNAVAILABLE = "CATALOG_UNAVAILABLE", "Courier list could not be loaded"


class CourierTrackingAlert(models.Model):
    kind = models.CharField(max_length=32, choices=AlertKind.choices)
    dedupe_key = models.CharField(max_length=128, unique=True)
    arrival = models.ForeignKey(SampleArrival, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    message = models.CharField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["resolved_at", "created_at"])]

    def __str__(self) -> str:
        return f"{self.kind}: {self.message[:60]}"


__all__ = [
    "ArrivalMode",
    "AlertKind",
    "Courier",
    "CourierTrackingAlert",
    "CourierTrackingSettings",
    "DeliveryStatus",
    "LookupOutcome",
    "LookupTrigger",
    "NotificationChannel",
    "ReminderKind",
    "SampleArrival",
    "ShipmentNotification",
    "ShipmentReminder",
    "ShipmentStatus",
    "TrackingLookup",
    "TrackingQuotaMonth",
    "TrackingWebhookEvent",
    "WebhookEventStatus",
]

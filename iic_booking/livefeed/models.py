"""Live instrument feed: the Lab Operator starts it at the instrument PC, the booking owner watches during their
slot. Media flows peer to peer (WebRTC) between the viewer's browser and the Department Sync Agent; the portal only
relays the session description (offer / answer) and checks who may watch."""

from django.conf import settings
from django.db import models


class LiveFeedSession(models.Model):
    ACTIVE, ENDED = "active", "ended"
    STATUS_CHOICES = [(ACTIVE, "Active"), (ENDED, "Ended")]

    equipment = models.ForeignKey("equipment.Equipment", on_delete=models.CASCADE, related_name="live_feed_sessions")
    booking = models.ForeignKey("equipment.Booking", on_delete=models.CASCADE, related_name="live_feed_sessions")
    sync_agent = models.ForeignKey("sync.DepartmentSyncAgent", on_delete=models.CASCADE, related_name="live_feed_sessions")
    equipment_pc = models.CharField(max_length=120, blank=True, help_text="Instrument PC the operator started it from")
    sources = models.JSONField(default=list, help_text='Available video: "screen", "camera"')
    audio = models.BooleanField(default=True, help_text="Two-way audio with the Lab Operator")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=ACTIVE, db_index=True)
    slot_end = models.DateTimeField()
    started_at = models.DateTimeField(auto_now_add=True)
    last_agent_poll_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    end_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"Live feed {self.pk} · {self.equipment_id} · booking {self.booking_id} · {self.status}"


class LiveFeedPeer(models.Model):
    """One viewer connection for one source (the screen with the operator's audio, or the camera)."""

    OFFERED, ANSWERED, CLOSED, FAILED = "offered", "answered", "closed", "failed"
    STATUS_CHOICES = [(OFFERED, "Offered"), (ANSWERED, "Answered"), (CLOSED, "Closed"), (FAILED, "Failed")]
    SCREEN, CAMERA = "screen", "camera"
    KIND_CHOICES = [(SCREEN, "Instrument PC screen"), (CAMERA, "Lab camera")]

    session = models.ForeignKey(LiveFeedSession, on_delete=models.CASCADE, related_name="peers")
    viewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="live_feed_peers")
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    offer_sdp = models.TextField()
    answer_sdp = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=OFFERED)
    error = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    answered_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]

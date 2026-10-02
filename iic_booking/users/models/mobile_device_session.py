"""
Revocable, device-bound sessions for the native mobile app.

The web keeps its single DRF Token per user (last login wins). Each enrolled phone gets its own
short-lived access token and rotating refresh token, so a web login elsewhere does not sign the
app out. Only SHA-256 hashes of the tokens are stored.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone


class MobileDeviceSession(models.Model):
    PLATFORM_ANDROID = "android"
    PLATFORM_IOS = "ios"
    PLATFORM_CHOICES = [
        (PLATFORM_ANDROID, "Android"),
        (PLATFORM_IOS, "iOS"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="mobile_device_sessions",
    )
    device_id = models.CharField(max_length=128, db_index=True)
    device_name = models.CharField(max_length=100, blank=True, default="")
    platform = models.CharField(max_length=16, choices=PLATFORM_CHOICES)
    app_version = models.CharField(max_length=32, blank=True, default="")

    access_hash = models.CharField(max_length=64, unique=True, db_index=True)
    access_expires_at = models.DateTimeField()
    prev_access_hash = models.CharField(max_length=64, blank=True, default="", db_index=True)
    prev_access_valid_until = models.DateTimeField(null=True, blank=True)

    refresh_hash = models.CharField(max_length=64, unique=True, db_index=True)
    prev_refresh_hash = models.CharField(max_length=64, blank=True, default="", db_index=True)
    refreshed_at = models.DateTimeField(null=True, blank=True)
    refresh_expires_at = models.DateTimeField()
    absolute_expires_at = models.DateTimeField()

    require_biometric = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    last_ip = models.GenericIPAddressField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True, db_index=True)
    revoke_reason = models.CharField(max_length=32, blank=True, default="")

    class Meta:
        verbose_name = "Mobile device session"
        verbose_name_plural = "Mobile device sessions"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.device_name or self.device_id} ({self.platform}) for user {self.user_id}"

    @property
    def is_active(self) -> bool:
        if self.revoked_at is not None:
            return False
        now = timezone.now()
        return self.refresh_expires_at > now and self.absolute_expires_at > now

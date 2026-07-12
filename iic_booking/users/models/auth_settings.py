"""
Singleton-style model for auth-related settings editable from Django admin.
Used for inactivity timeout (auto-logout) and similar config.
"""

from django.db import models


class AuthSettings(models.Model):
    """
    Single-row configuration for authentication (e.g. inactivity timeout).
    Only one row is used (singleton); id=1 is created by migration.
    """
    # Auto-logout after this many seconds of no API activity (default 30 min).
    # Frontend should use a slightly lower value so it logs out before the backend invalidates.
    inactivity_timeout_seconds = models.PositiveIntegerField(
        default=1800,
        help_text="Seconds of inactivity after which the user is automatically logged out (default 1800 = 30 minutes).",
    )

    class Meta:
        db_table = "users_authsettings"
        verbose_name = "Auth settings"
        verbose_name_plural = "Auth settings"

    def __str__(self):
        return "Auth settings (inactivity timeout: {} min)".format(
            self.inactivity_timeout_seconds // 60
        )

    @classmethod
    def get_singleton(cls):
        """Return the single settings row, creating it with defaults if missing."""
        obj, _ = cls.objects.get_or_create(
            pk=1,
            defaults={"inactivity_timeout_seconds": 1800},
        )
        return obj

"""
Per-user-type inactivity timeout (auto-logout). Overrides global AuthSettings when set.
"""

from django.db import models

from .user_type import UserType


class UserTypeInactivityTimeout(models.Model):
    """
    Inactivity timeout in seconds for a specific user type (e.g. student, faculty, admin).
    When set, users of this type are logged out after this many seconds of no activity.
    If no row exists for a user type, the global AuthSettings.inactivity_timeout_seconds is used.
    """
    user_type = models.CharField(
        max_length=50,
        unique=True,
        choices=UserType.get_choices(),
        help_text="User type (e.g. student, faculty, admin). Each type can have its own timeout.",
    )
    inactivity_timeout_seconds = models.PositiveIntegerField(
        default=1800,
        help_text="Seconds of inactivity after which this user type is automatically logged out (e.g. 1800 = 30 min).",
    )

    class Meta:
        db_table = "users_usertypeinactivitytimeout"
        verbose_name = "User type inactivity timeout"
        verbose_name_plural = "User type inactivity timeouts"
        ordering = ["user_type"]

    def __str__(self):
        return "{}: {} min".format(
            self.get_user_type_display() if hasattr(self, "get_user_type_display") else self.user_type,
            self.inactivity_timeout_seconds // 60,
        )

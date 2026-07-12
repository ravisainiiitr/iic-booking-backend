"""
Dedicated lock row per user for serializing login/token regeneration across all workers.
Used in _regenerate_auth_token so single-session works even with LocMemCache (no Redis).
"""

from django.conf import settings
from django.db import models


class UserLoginLock(models.Model):
    """
    One row per user; select_for_update() on this row serializes token regeneration
    across all processes (same machine or multiple workers). Not used for business logic.
    """
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="+",
    )

    class Meta:
        db_table = "users_userloginlock"
        verbose_name = "User login lock"
        verbose_name_plural = "User login locks"

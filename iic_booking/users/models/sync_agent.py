"""Department Sync Agent (DSA) identity stored by the booking portal."""

from __future__ import annotations

import secrets
import uuid
from datetime import timedelta

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


def _generate_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


class SyncAgent(models.Model):
    """Registered Support PC running the Department Sync Agent."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agent_name = models.CharField(_("Agent name"), max_length=200)
    department = models.ForeignKey(
        "users.Department",
        on_delete=models.PROTECT,
        related_name="sync_agents",
        null=True,
        blank=True,
    )
    department_code = models.CharField(_("Department code"), max_length=50)
    machine_name = models.CharField(_("Machine name"), max_length=200, blank=True)
    machine_guid = models.UUIDField(_("Machine GUID"), unique=True, db_index=True)
    version = models.CharField(_("Agent version"), max_length=50, blank=True)
    operating_system = models.CharField(_("Operating system"), max_length=200, blank=True)

    registration_token = models.CharField(
        _("Registration token"),
        max_length=128,
        default=_generate_token,
        unique=True,
    )
    refresh_token = models.CharField(_("Refresh token"), max_length=128, blank=True, default="")
    access_token = models.CharField(_("Access token"), max_length=128, blank=True, default="")
    access_token_expires_at = models.DateTimeField(
        _("Access token expires at"),
        null=True,
        blank=True,
    )

    is_active = models.BooleanField(_("Active"), default=True)
    registered_at = models.DateTimeField(_("Registered at"), default=timezone.now)
    last_authenticated_at = models.DateTimeField(
        _("Last authenticated at"),
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Sync Agent")
        verbose_name_plural = _("Sync Agents")
        ordering = ["-registered_at"]

    def __str__(self) -> str:
        return f"{self.agent_name} ({self.machine_name or self.machine_guid})"

    def issue_tokens(self, *, access_lifetime_hours: int = 12) -> None:
        self.access_token = _generate_token(48)
        self.refresh_token = _generate_token(48)
        self.access_token_expires_at = timezone.now() + timedelta(hours=access_lifetime_hours)
        self.last_authenticated_at = timezone.now()

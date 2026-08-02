"""DSA installer release artifacts (Phase 1 MVP)."""

from __future__ import annotations

import uuid

from django.db import models
from django.utils.translation import gettext_lazy as _


class DsaInstallerRelease(models.Model):
    """Published Department Sync Agent Setup EXE available from the portal."""

    class Channel(models.TextChoices):
        STABLE = "stable", _("Stable")
        RC = "rc", _("Release Candidate")
        BETA = "beta", _("Beta")

    class SignatureStatus(models.TextChoices):
        UNSIGNED = "unsigned", _("Unsigned")
        SIGNED = "signed", _("Signed")
        VERIFIED = "verified", _("Verified")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    version = models.CharField(max_length=64)
    channel = models.CharField(max_length=16, choices=Channel.choices, default=Channel.STABLE)
    release_date = models.DateField()
    release_notes = models.TextField(blank=True, default="")
    supported_windows = models.CharField(
        max_length=255,
        blank=True,
        default="Windows 10 Pro, Windows 11 Pro, Windows Server 2019/2022",
    )
    min_ram_gb = models.PositiveIntegerField(default=8)
    min_disk_gb = models.PositiveIntegerField(default=20)
    download_size_bytes = models.BigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True, default="")
    signature_status = models.CharField(
        max_length=16,
        choices=SignatureStatus.choices,
        default=SignatureStatus.UNSIGNED,
    )
    file = models.FileField(
        upload_to="dsa_installers/%Y/%m/%d/",
        max_length=512,
        blank=True,
        null=True,
        help_text=_("Online/setup EXE (self-contained bootstrapper)."),
    )
    offline_file = models.FileField(
        upload_to="dsa_installers/%Y/%m/%d/offline/",
        max_length=512,
        blank=True,
        null=True,
        help_text=_("Optional offline installer package."),
    )
    original_name = models.CharField(max_length=255, blank=True, default="")
    offline_original_name = models.CharField(max_length=255, blank=True, default="")
    documentation_url = models.URLField(blank=True, default="")
    installation_guide_url = models.URLField(blank=True, default="")
    troubleshooting_guide_url = models.URLField(blank=True, default="")
    is_latest = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-release_date", "-created_at"]
        verbose_name = _("DSA installer release")
        verbose_name_plural = _("DSA installer releases")

    def __str__(self) -> str:
        latest = " (latest)" if self.is_latest else ""
        return f"{self.version}{latest}"

    def mark_latest(self) -> None:
        type(self).objects.filter(is_latest=True).exclude(pk=self.pk).update(is_latest=False)
        if not self.is_latest:
            self.is_latest = True
            self.save(update_fields=["is_latest", "updated_at"])

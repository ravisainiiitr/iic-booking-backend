"""Unified Deployment Center — installer catalog for Main Administrators."""

from __future__ import annotations

import uuid

from django.db import models
from django.utils.translation import gettext_lazy as _


class EquipmentPcWizardRelease(models.Model):
    """Published Equipment PC Configuration Wizard EXE."""

    class Channel(models.TextChoices):
        STABLE = "stable", _("Stable")
        RC = "rc", _("Release Candidate")
        BETA = "beta", _("Beta")

    class SignatureStatus(models.TextChoices):
        UNSIGNED = "unsigned", _("Unsigned")
        SIGNED = "signed", _("Signed")
        VERIFIED = "verified", _("Verified")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product_name = models.CharField(max_length=128, default="Equipment PC Configuration Wizard")
    version = models.CharField(max_length=64)
    build_number = models.CharField(max_length=64, blank=True, default="")
    channel = models.CharField(max_length=16, choices=Channel.choices, default=Channel.STABLE)
    release_date = models.DateField()
    release_notes = models.TextField(blank=True, default="")
    supported_windows = models.CharField(
        max_length=255,
        blank=True,
        default="Windows 10 Pro, Windows 11 Pro, Windows Server 2019/2022",
    )
    download_size_bytes = models.BigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True, default="")
    signature_status = models.CharField(
        max_length=16,
        choices=SignatureStatus.choices,
        default=SignatureStatus.UNSIGNED,
    )
    file = models.FileField(
        upload_to="equipment_pc_wizard/%Y/%m/%d/",
        max_length=512,
        blank=True,
        null=True,
    )
    original_name = models.CharField(max_length=255, blank=True, default="")
    documentation_url = models.URLField(blank=True, default="")
    installation_guide_url = models.URLField(blank=True, default="")
    troubleshooting_guide_url = models.URLField(blank=True, default="")
    download_count = models.PositiveIntegerField(default=0)
    is_latest = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    # Phase 2 Deployment Center metadata
    compatibility = models.JSONField(
        blank=True,
        default=dict,
        help_text=_('e.g. {"min_portal":"1.0","min_dsa":"1.0","min_raa":"1.0"}'),
    )
    rollback_of = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rollbacks",
    )
    repair_file = models.FileField(
        upload_to="equipment_pc_wizard/%Y/%m/%d/repair/",
        max_length=512,
        blank=True,
        null=True,
    )
    emergency_file = models.FileField(
        upload_to="equipment_pc_wizard/%Y/%m/%d/emergency/",
        max_length=512,
        blank=True,
        null=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-release_date", "-created_at"]
        verbose_name = _("Equipment PC Wizard release")
        verbose_name_plural = _("Equipment PC Wizard releases")

    def __str__(self) -> str:
        latest = " (latest)" if self.is_latest else ""
        return f"{self.version}{latest}"

    def mark_latest(self) -> None:
        type(self).objects.filter(is_latest=True).exclude(pk=self.pk).update(is_latest=False)
        if not self.is_latest:
            self.is_latest = True
            self.save(update_fields=["is_latest", "updated_at"])

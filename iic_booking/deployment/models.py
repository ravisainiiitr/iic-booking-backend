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


def default_mobile_app_audience() -> list[str]:
    return ["manager", "operator", "admin"]


class MobileAppSettings(models.Model):
    """Single row: who may sign in through the IIC Booking mobile app (website use is unaffected)."""

    audience_user_types = models.JSONField(
        default=default_mobile_app_audience,
        blank=True,
        help_text=_("User type codes allowed to sign in through the app, e.g. manager, operator, admin."),
    )
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        verbose_name = _("Mobile app settings")
        verbose_name_plural = _("Mobile app settings")

    def __str__(self) -> str:
        return "Mobile app audience: " + ", ".join(self.audience_user_types or [])

    @classmethod
    def get_singleton(cls) -> "MobileAppSettings":
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj


class MobileAppRelease(models.Model):
    """A published Android app build (APK) offered to signed-in users in the app audience."""

    class Platform(models.TextChoices):
        ANDROID = "android", _("Android")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    platform = models.CharField(max_length=16, choices=Platform.choices, default=Platform.ANDROID)
    version_name = models.CharField(max_length=32)
    version_code = models.PositiveIntegerField()
    release_date = models.DateField()
    release_notes = models.TextField(blank=True, default="")
    min_android = models.CharField(max_length=32, blank=True, default="Android 7.0 or newer")
    file = models.FileField(upload_to="mobile_app/%Y/%m/%d/", max_length=512, blank=True, null=True)
    original_name = models.CharField(max_length=255, blank=True, default="")
    download_size_bytes = models.BigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True, default="")
    signing_cert_sha256 = models.CharField(max_length=128, blank=True, default="")
    download_count = models.PositiveIntegerField(default=0)
    is_latest = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-version_code", "-created_at"]
        verbose_name = _("Mobile app release")
        verbose_name_plural = _("Mobile app releases")

    def __str__(self) -> str:
        latest = " (latest)" if self.is_latest else ""
        return f"{self.platform} {self.version_name} ({self.version_code}){latest}"

    def mark_latest(self) -> None:
        type(self).objects.filter(platform=self.platform, is_latest=True).exclude(pk=self.pk).update(
            is_latest=False
        )
        if not self.is_latest:
            self.is_latest = True
            self.save(update_fields=["is_latest", "updated_at"])

"""Analysis software catalog and equipment mapping for Analyze Data."""

from __future__ import annotations

import uuid

from django.db import models
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _


class AnalysisSoftwareCatalog(models.Model):
    """Admin-managed analysis software package (user-facing catalog)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)
    vendor = models.CharField(max_length=255, blank=True, default="")
    version_constraint = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text=_("Minimum / preferred version string matched against inventory."),
    )
    license_type = models.CharField(
        max_length=128,
        blank=True,
        default="",
        choices=[
            ("unlimited", "Unlimited"),
            ("node_locked", "Node Locked"),
            ("concurrent", "Concurrent"),
            ("floating", "Floating"),
            ("network", "Network License Server"),
            ("dongle", "Dongle"),
            ("expired", "Expired"),
            ("other", "Other"),
        ],
        help_text=_("License model for scheduling and seat checks (AI/ops metadata)."),
    )
    max_concurrent = models.PositiveIntegerField(
        default=0,
        help_text=_("Max concurrent sessions using this software (0 = unlimited)."),
    )
    supported_departments = models.ManyToManyField(
        "users.Department",
        blank=True,
        related_name="ra_software_catalog",
    )
    description = models.TextField(blank=True, default="")
    category = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text=_("Optional grouping for installer software checklist (e.g. XRD, General)."),
    )
    icon_url = models.URLField(
        blank=True,
        default="",
        help_text=_("Optional icon URL shown in the agent installer software list."),
    )
    default_session_duration_hours = models.PositiveIntegerField(default=4)
    is_active = models.BooleanField(default=True)
    is_archived = models.BooleanField(
        default=False,
        help_text=_("Archived catalog entries are hidden from new mappings but retained for history."),
    )
    typical_usage = models.TextField(
        blank=True,
        default="",
        help_text=_("Short typical-usage blurb shown in Analysis Workspace."),
    )
    accepted_file_types = models.JSONField(
        blank=True,
        default=list,
        help_text=_("File extensions / types this software typically accepts (e.g. ['.raw', '.xy'])."),
    )
    license_server_url = models.CharField(
        max_length=512,
        blank=True,
        default="",
        help_text=_("Optional license server URL / host for network or floating licenses."),
    )
    license_seats = models.PositiveIntegerField(
        default=0,
        help_text=_("Configured seats for network/floating/concurrent (0 = use max_concurrent)."),
    )
    ai_tags = models.JSONField(
        blank=True,
        default=list,
        help_text=_("Optional tags for future AI software recommendation (e.g. technique, file type)."),
    )
    ai_metadata = models.JSONField(
        blank=True,
        default=dict,
        help_text=_("Opaque AI-ready metadata blob."),
    )
    capabilities = models.ManyToManyField(
        "remote_analysis.AnalysisCapability",
        blank=True,
        related_name="software_packages",
        help_text=_("Capability tags advertised by this software (Peak Fitting, Image Analysis, …)."),
    )
    software_requirement = models.OneToOneField(
        "remote_analysis.SoftwareRequirement",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="catalog_entry",
        help_text=_("Linked scheduler profile used by AllocationService."),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name = _("Analysis software catalog")
        verbose_name_plural = _("Analysis software catalog")

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name) or "software"
            candidate = base
            n = 1
            while AnalysisSoftwareCatalog.objects.filter(slug=candidate).exclude(pk=self.pk).exists():
                n += 1
                candidate = f"{base}-{n}"
            self.slug = candidate
        super().save(*args, **kwargs)

    def ensure_software_requirement(self) -> "SoftwareRequirement":
        from iic_booking.remote_analysis.scheduler_models import SoftwareRequirement

        if self.software_requirement_id:
            req = self.software_requirement
            changed = False
            if req.software != self.name and req.software != (self.name.split()[0] if self.name else ""):
                # Keep software match string aligned with catalog name
                if not req.software:
                    req.software = self.name
                    changed = True
            if self.version_constraint and req.minimum_version != self.version_constraint:
                req.minimum_version = self.version_constraint
                changed = True
            if self.max_concurrent and not req.license_required:
                req.license_required = True
                changed = True
            if changed:
                req.save()
            return req

        req = SoftwareRequirement.objects.create(
            name=f"Catalog: {self.name}",
            software=self.name,
            minimum_version=self.version_constraint or "",
            required=True,
            license_required=bool(self.max_concurrent),
        )
        self.software_requirement = req
        self.save(update_fields=["software_requirement", "updated_at"])
        return req


class EquipmentAnalysisSoftware(models.Model):
    """Maps equipment to catalog software for Analyze Data."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    equipment = models.ForeignKey(
        "equipment.Equipment",
        on_delete=models.CASCADE,
        related_name="analysis_software_mappings",
    )
    catalog = models.ForeignKey(
        AnalysisSoftwareCatalog,
        on_delete=models.CASCADE,
        related_name="equipment_mappings",
    )
    is_default = models.BooleanField(default=False)
    button_label_override = models.CharField(max_length=128, blank=True, default="")
    sort_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "catalog__name"]
        verbose_name = _("Equipment analysis software")
        verbose_name_plural = _("Equipment analysis software")
        constraints = [
            models.UniqueConstraint(
                fields=["equipment", "catalog"],
                name="uniq_equipment_analysis_software",
            ),
        ]

    def __str__(self) -> str:
        flag = " (default)" if self.is_default else ""
        return f"{self.equipment_id} → {self.catalog}{flag}"


class EquipmentAnalysisPool(models.Model):
    """Preferred Analysis PC pool for an equipment (EQUIPMENT_PRIORITY)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    equipment = models.ForeignKey(
        "equipment.Equipment",
        on_delete=models.CASCADE,
        related_name="analysis_workstation_pool",
    )
    workstation = models.ForeignKey(
        "remote_analysis.AnalysisWorkstation",
        on_delete=models.CASCADE,
        related_name="equipment_pools",
    )
    priority_boost = models.IntegerField(default=10)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-priority_boost", "workstation__hostname"]
        verbose_name = _("Equipment analysis pool member")
        verbose_name_plural = _("Equipment analysis pool")
        constraints = [
            models.UniqueConstraint(
                fields=["equipment", "workstation"],
                name="uniq_equipment_analysis_pool",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.equipment_id} ↔ {self.workstation_id}"

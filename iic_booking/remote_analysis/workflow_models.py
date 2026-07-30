"""Analysis Workflow templates, versions, steps, and Analysis Jobs (runtime)."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from iic_booking.remote_analysis.constants import (
    AnalysisJobCollaboratorRole,
    WorkflowJobStatus,
    WorkflowJobStepStatus,
)


class AnalysisCapability(models.Model):
    """Software capability tags (Peak Fitting, Rietveld, Image Analysis, …)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=128)
    slug = models.SlugField(max_length=128, unique=True)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name = _("Analysis capability")
        verbose_name_plural = _("Analysis capabilities")

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name) or "capability"
            candidate = base
            n = 1
            while AnalysisCapability.objects.filter(slug=candidate).exclude(pk=self.pk).exists():
                n += 1
                candidate = f"{base}-{n}"
            self.slug = candidate
        super().save(*args, **kwargs)


class AnalysisWorkflow(models.Model):
    """Reusable analysis pipeline template (Equipment → Workflow)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    is_template = models.BooleanField(
        default=False,
        help_text=_("When True, this workflow is a cloneable lab template."),
    )
    cloned_from = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="clones",
    )
    estimated_duration_minutes = models.PositiveIntegerField(default=60)

    # Input requirements (verified before launch)
    require_raw_data = models.BooleanField(default=True)
    require_calibration = models.BooleanField(default=False)
    require_reference_files = models.BooleanField(default=False)
    optional_input_types = models.JSONField(
        default=list,
        blank=True,
        help_text=_('Optional input labels, e.g. ["Crystal database", "Previous experiment"].'),
    )
    input_requirements = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Extensible input schema: {raw, calibration, reference, optional: [...]}."),
    )
    # Workflow variables schema: [{key, label, type, required, default}]
    variables_schema = models.JSONField(default=list, blank=True)

    # Reserved for future AI integration (do not implement consumers yet)
    ai_assistance_enabled = models.BooleanField(default=False)
    ai_suggested_parameters = models.JSONField(default=dict, blank=True)
    ai_auto_classification = models.JSONField(default=dict, blank=True)
    ai_quality_score_schema = models.JSONField(default=dict, blank=True)
    ai_analysis_notes_prompt = models.TextField(blank=True, default="")

    # Reserved for v2 collaboration
    collaboration_enabled = models.BooleanField(
        default=False,
        help_text=_("Reserved for multi-user Analysis Jobs (v2)."),
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_analysis_workflows",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name = _("Analysis workflow")
        verbose_name_plural = _("Analysis workflows")

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name) or "workflow"
            candidate = base
            n = 1
            while AnalysisWorkflow.objects.filter(slug=candidate).exclude(pk=self.pk).exists():
                n += 1
                candidate = f"{base}-{n}"
            self.slug = candidate
        super().save(*args, **kwargs)

    def published_version(self):
        return (
            self.versions.filter(is_published=True).order_by("-published_at", "-version_number").first()
        )


class AnalysisWorkflowVersion(models.Model):
    """Immutable published snapshot of a workflow definition."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workflow = models.ForeignKey(
        AnalysisWorkflow,
        on_delete=models.CASCADE,
        related_name="versions",
    )
    version_number = models.PositiveIntegerField(default=1)
    label = models.CharField(max_length=64, blank=True, default="")
    changelog = models.TextField(blank=True, default="")
    is_published = models.BooleanField(default=False)
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version_number"]
        verbose_name = _("Analysis workflow version")
        verbose_name_plural = _("Analysis workflow versions")
        constraints = [
            models.UniqueConstraint(
                fields=["workflow", "version_number"],
                name="uniq_analysis_workflow_version",
            ),
        ]

    def __str__(self) -> str:
        label = self.label or f"v{self.version_number}"
        return f"{self.workflow.name} {label}"


class AnalysisWorkflowStep(models.Model):
    """Ordered processing step within a workflow version."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    version = models.ForeignKey(
        AnalysisWorkflowVersion,
        on_delete=models.CASCADE,
        related_name="steps",
    )
    step_number = models.PositiveIntegerField()
    title = models.CharField(max_length=255, blank=True, default="")
    software = models.ForeignKey(
        "remote_analysis.AnalysisSoftwareCatalog",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="workflow_steps",
    )
    capability = models.ForeignKey(
        AnalysisCapability,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="workflow_steps",
        help_text=_("Prefer capability tags so software can be swapped without editing workflows."),
    )
    version_constraint = models.CharField(max_length=128, blank=True, default="")
    mandatory = models.BooleanField(default=True)
    estimated_duration_minutes = models.PositiveIntegerField(default=30)
    expected_output_folder = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=_("Defaults to StepNN from step_number when blank."),
    )
    expected_outputs = models.JSONField(
        default=list,
        blank=True,
        help_text=_('Glob patterns for automatic verification, e.g. ["*.xy", "*.pdf"].'),
    )
    allowed_file_types = models.JSONField(default=list, blank=True)
    validation_rules = models.JSONField(default=dict, blank=True)
    description = models.TextField(blank=True, default="")
    operator_instructions = models.TextField(blank=True, default="")
    help_url = models.URLField(blank=True, default="")
    reference_manual_url = models.URLField(blank=True, default="")
    environment_label = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text=_('User-facing Analysis Environment name, e.g. "OriginPro Environment".'),
    )
    # Reserved AI fields
    ai_suggested_parameters = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["step_number"]
        verbose_name = _("Analysis workflow step")
        verbose_name_plural = _("Analysis workflow steps")
        constraints = [
            models.UniqueConstraint(
                fields=["version", "step_number"],
                name="uniq_analysis_workflow_step_number",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.version_id} step {self.step_number}: {self.display_title}"

    @property
    def display_title(self) -> str:
        if self.title:
            return self.title
        if self.software_id:
            return self.software.name
        if self.capability_id:
            return self.capability.name
        return f"Step {self.step_number}"

    @property
    def folder_name(self) -> str:
        if self.expected_output_folder:
            return self.expected_output_folder
        return f"Step{self.step_number:02d}"

    @property
    def analysis_environment_label(self) -> str:
        if self.environment_label:
            return self.environment_label
        soft = self.software.name if self.software_id else (self.capability.name if self.capability_id else "Analysis")
        return f"{soft} Environment"


class EquipmentAnalysisWorkflow(models.Model):
    """Maps equipment to an Analysis Workflow (replaces single-software as primary path)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    equipment = models.ForeignKey(
        "equipment.Equipment",
        on_delete=models.CASCADE,
        related_name="analysis_workflow_mappings",
    )
    workflow = models.ForeignKey(
        AnalysisWorkflow,
        on_delete=models.CASCADE,
        related_name="equipment_mappings",
    )
    is_default = models.BooleanField(default=False)
    sort_order = models.PositiveIntegerField(default=0)
    button_label_override = models.CharField(max_length=128, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "workflow__name"]
        verbose_name = _("Equipment analysis workflow")
        verbose_name_plural = _("Equipment analysis workflows")
        constraints = [
            models.UniqueConstraint(
                fields=["equipment", "workflow"],
                name="uniq_equipment_analysis_workflow",
            ),
        ]

    def __str__(self) -> str:
        flag = " (default)" if self.is_default else ""
        return f"{self.equipment_id} → {self.workflow}{flag}"


class AnalysisJob(models.Model):
    """Runtime instance of a workflow for a booking (template → job)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking = models.ForeignKey(
        "equipment.Booking",
        on_delete=models.CASCADE,
        related_name="analysis_jobs",
    )
    workflow_version = models.ForeignKey(
        AnalysisWorkflowVersion,
        on_delete=models.PROTECT,
        related_name="jobs",
    )
    workspace = models.ForeignKey(
        "remote_analysis.AnalysisWorkspace",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="analysis_jobs",
    )
    reservation = models.ForeignKey(
        "remote_analysis.AnalysisReservation",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="analysis_jobs",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="analysis_jobs",
    )
    status = models.CharField(
        max_length=32,
        choices=WorkflowJobStatus.choices,
        default=WorkflowJobStatus.PENDING,
        db_index=True,
    )
    current_step_number = models.PositiveIntegerField(default=1)
    preferred_workstation = models.ForeignKey(
        "remote_analysis.AnalysisWorkstation",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="preferred_analysis_jobs",
        help_text=_("Internal same-PC preference when all mandatory software is present."),
    )
    variables = models.JSONField(default=dict, blank=True)
    ux_status = models.CharField(max_length=128, blank=True, default="")
    status_detail = models.TextField(blank=True, default="")
    same_environment = models.BooleanField(
        default=False,
        help_text=_("True when all mandatory steps stay on one Analysis Environment."),
    )
    started_at = models.DateTimeField(null=True, blank=True)
    paused_at = models.DateTimeField(null=True, blank=True)
    resumed_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Analysis job")
        verbose_name_plural = _("Analysis jobs")
        indexes = [
            models.Index(fields=["booking", "status"]),
            models.Index(fields=["status", "updated_at"]),
        ]

    def __str__(self) -> str:
        return f"Job {self.id} booking={self.booking_id} {self.status}"

    @property
    def workflow(self):
        return self.workflow_version.workflow


class AnalysisJobStep(models.Model):
    """Runtime progress for one workflow step within an Analysis Job."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(AnalysisJob, on_delete=models.CASCADE, related_name="steps")
    workflow_step = models.ForeignKey(
        AnalysisWorkflowStep,
        on_delete=models.PROTECT,
        related_name="job_steps",
    )
    step_number = models.PositiveIntegerField()
    status = models.CharField(
        max_length=32,
        choices=WorkflowJobStepStatus.choices,
        default=WorkflowJobStepStatus.PENDING,
        db_index=True,
    )
    session = models.ForeignKey(
        "remote_analysis.RemoteDesktopSession",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="analysis_job_steps",
    )
    workstation = models.ForeignKey(
        "remote_analysis.AnalysisWorkstation",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="analysis_job_steps",
    )
    input_folder = models.CharField(max_length=64, blank=True, default="")
    output_folder = models.CharField(max_length=64, blank=True, default="")
    environment_label = models.CharField(max_length=128, blank=True, default="")
    verification_result = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    checkpoint_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["step_number"]
        verbose_name = _("Analysis job step")
        verbose_name_plural = _("Analysis job steps")
        constraints = [
            models.UniqueConstraint(fields=["job", "step_number"], name="uniq_analysis_job_step"),
        ]

    def __str__(self) -> str:
        return f"Job {self.job_id} step {self.step_number} ({self.status})"


class AnalysisJobCollaborator(models.Model):
    """Reserved v2: Owner / Collaborator / Viewer / Observer on an Analysis Job."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(AnalysisJob, on_delete=models.CASCADE, related_name="collaborators")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="analysis_job_collaborations",
    )
    role = models.CharField(
        max_length=32,
        choices=AnalysisJobCollaboratorRole.choices,
        default=AnalysisJobCollaboratorRole.VIEWER,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Analysis job collaborator (v2 reserved)")
        verbose_name_plural = _("Analysis job collaborators (v2 reserved)")
        constraints = [
            models.UniqueConstraint(fields=["job", "user"], name="uniq_analysis_job_collaborator"),
        ]

    def __str__(self) -> str:
        return f"{self.job_id} → {self.user_id} ({self.role})"

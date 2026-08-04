"""Lab Infrastructure domain models — config history, acks, repair audit."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class LabNodeKind(models.TextChoices):
    DSA = "dsa", _("Department Sync Agent")
    EQUIPMENT_PC = "equipment_pc", _("Equipment PC")
    RAA = "raa", _("Remote Analysis Agent")
    ANALYSIS_PC = "analysis_pc", _("Analysis PC")


class LabNodeStatus(models.TextChoices):
    ONLINE = "online", _("Online")
    OFFLINE = "offline", _("Offline")
    SYNCHRONIZING = "synchronizing", _("Synchronizing")
    BUSY = "busy", _("Busy")
    MAINTENANCE = "maintenance", _("Maintenance")
    COMMISSIONING = "commissioning", _("Commissioning")
    ERROR = "error", _("Error")
    WAITING = "waiting", _("Waiting")


class ConfigurationChange(models.Model):
    """History of Equipment Sync Profile configuration changes (Portal master)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sync_profile = models.ForeignKey(
        "sync.EquipmentSyncProfile",
        on_delete=models.CASCADE,
        related_name="configuration_changes",
    )
    configuration_version = models.PositiveIntegerField()
    previous_value = models.JSONField(default=dict, blank=True)
    new_value = models.JSONField(default=dict, blank=True)
    reason = models.CharField(max_length=500, blank=True, default="")
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="lab_configuration_changes",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["sync_profile", "configuration_version"])]


class ConfigurationAck(models.Model):
    """Agent / Equipment PC acknowledgement of a configuration version."""

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        APPLIED = "applied", _("Applied")
        FAILED = "failed", _("Failed")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sync_agent = models.ForeignKey(
        "sync.DepartmentSyncAgent",
        on_delete=models.CASCADE,
        related_name="configuration_acks",
        null=True,
        blank=True,
    )
    sync_profile = models.ForeignKey(
        "sync.EquipmentSyncProfile",
        on_delete=models.CASCADE,
        related_name="configuration_acks",
        null=True,
        blank=True,
    )
    equipment_pc_id = models.CharField(max_length=64, blank=True, default="")
    configuration_version = models.PositiveIntegerField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    error_message = models.TextField(blank=True, default="")
    applied_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["configuration_version", "status"]),
            models.Index(fields=["equipment_pc_id"]),
        ]


class LabRepairAction(models.Model):
    """Self-heal / repair command issued from Laboratory Infrastructure."""

    class Action(models.TextChoices):
        REPAIR = "repair", _("Repair")
        RECONFIGURE = "reconfigure", _("Reconfigure")
        RECOMMISSION = "recommission", _("Recommission")
        RESTART_AGENT = "restart_agent", _("Restart Agent")
        REFRESH_CONFIGURATION = "refresh_configuration", _("Refresh Configuration")
        RESCAN_SOFTWARE = "rescan_software", _("Rescan Software")
        RETRY_SYNCHRONIZATION = "retry_synchronization", _("Retry Synchronization")

    class Status(models.TextChoices):
        QUEUED = "queued", _("Queued")
        SENT = "sent", _("Sent")
        SUCCEEDED = "succeeded", _("Succeeded")
        FAILED = "failed", _("Failed")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    node_kind = models.CharField(max_length=32, choices=LabNodeKind.choices)
    node_id = models.CharField(max_length=64)
    action = models.CharField(max_length=32, choices=Action.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="lab_repair_actions",
    )
    result = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]


class LabAuditEvent(models.Model):
    """Unified audit events for Lab Infrastructure (complements SyncLog / WorkstationEvent)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event_type = models.CharField(max_length=64, db_index=True)
    message = models.TextField(blank=True, default="")
    node_kind = models.CharField(max_length=32, blank=True, default="")
    node_id = models.CharField(max_length=64, blank=True, default="")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="lab_audit_events",
    )
    payload = models.JSONField(default=dict, blank=True)
    success = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["event_type", "created_at"])]


class LabAlert(models.Model):
    """Unified alerts for Laboratory Infrastructure dashboard."""

    class Severity(models.TextChoices):
        WARNING = "warning", _("Warning")
        ERROR = "error", _("Error")
        CRITICAL = "critical", _("Critical")

    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        ACKNOWLEDGED = "acknowledged", _("Acknowledged")
        RESOLVED = "resolved", _("Resolved")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=64, db_index=True)
    severity = models.CharField(max_length=16, choices=Severity.choices, default=Severity.WARNING)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    title = models.CharField(max_length=255)
    detail = models.TextField(blank=True, default="")
    node_kind = models.CharField(max_length=32, blank=True, default="")
    node_id = models.CharField(max_length=64, blank=True, default="")
    department_id = models.IntegerField(null=True, blank=True, db_index=True)
    source = models.CharField(max_length=32, blank=True, default="lab")  # lab|dsa|ra
    fingerprint = models.CharField(max_length=128, blank=True, default="", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "severity"])]


class SatTestCase(models.Model):
    """Catalog entry for System Acceptance / UAT / Integration tests (Phase 2.5)."""

    class Suite(models.TextChoices):
        SAT = "sat", _("System Acceptance")
        UAT = "uat", _("User Acceptance")
        INTEGRATION = "integration", _("Integration")
        PERFORMANCE = "performance", _("Performance")
        SECURITY = "security", _("Security")

    class Severity(models.TextChoices):
        CRITICAL = "critical", _("Critical")
        HIGH = "high", _("High")
        MEDIUM = "medium", _("Medium")
        LOW = "low", _("Low")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    test_id = models.CharField(max_length=32, unique=True, db_index=True)
    suite = models.CharField(max_length=16, choices=Suite.choices, default=Suite.SAT, db_index=True)
    module = models.CharField(max_length=64, db_index=True)
    feature = models.CharField(max_length=255)
    preconditions = models.TextField(blank=True, default="")
    steps = models.TextField(blank=True, default="")
    expected_result = models.TextField(blank=True, default="")
    severity = models.CharField(max_length=16, choices=Severity.choices, default=Severity.HIGH)
    stage = models.PositiveSmallIntegerField(
        default=1,
        db_index=True,
        help_text=_("Lab SAT execution stage 1–5"),
    )
    execution_order = models.PositiveIntegerField(default=0, db_index=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["stage", "execution_order", "test_id"]
        indexes = [models.Index(fields=["suite", "module"]), models.Index(fields=["stage", "execution_order"])]


class SatTestRun(models.Model):
    """One execution wave of the acceptance catalog."""

    class Status(models.TextChoices):
        RUNNING = "running", _("Running")
        COMPLETED = "completed", _("Completed")
        ABORTED = "aborted", _("Aborted")

    class Recommendation(models.TextChoices):
        GO = "go", _("GO")
        CONDITIONAL_GO = "conditional_go", _("Conditional GO")
        NO_GO = "no_go", _("NO GO")
        PENDING = "pending", _("Pending")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, blank=True, default="")
    suite = models.CharField(max_length=16, choices=SatTestCase.Suite.choices, blank=True, default="")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING)
    executed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sat_test_runs",
    )
    notes = models.TextField(blank=True, default="")
    current_result = models.ForeignKey(
        "SatTestResult",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    lab_context = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Optional building/floor/lab/equipment focus for this run"),
    )
    readiness_snapshot = models.JSONField(default=dict, blank=True)
    recommendation = models.CharField(
        max_length=32,
        choices=Recommendation.choices,
        default=Recommendation.PENDING,
    )
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]


class SatTestResult(models.Model):
    """Per-case result within a run (Actual Result / Status for SAT sheets)."""

    class Status(models.TextChoices):
        PASSED = "passed", _("Passed")
        FAILED = "failed", _("Failed")
        SKIPPED = "skipped", _("Skipped")
        BLOCKED = "blocked", _("Blocked")
        NOT_RUN = "not_run", _("Not Run")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(SatTestRun, on_delete=models.CASCADE, related_name="results")
    test_case = models.ForeignKey(SatTestCase, on_delete=models.CASCADE, related_name="results")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.NOT_RUN, db_index=True)
    actual_result = models.TextField(blank=True, default="")
    remarks = models.TextField(blank=True, default="")
    administrator_notes = models.TextField(blank=True, default="")
    log_url = models.URLField(blank=True, default="")
    evidence = models.JSONField(default=dict, blank=True)
    failure_snapshot = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Auto-captured environment at failure time"),
    )
    executed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["test_case__stage", "test_case__execution_order", "test_case__test_id"]
        unique_together = [("run", "test_case")]
        indexes = [models.Index(fields=["status", "run"])]


def sat_evidence_upload_to(instance, filename):
    run_id = getattr(instance, "run_id", None) or "unknown"
    return f"sat_evidence/{run_id}/{filename}"


class SatEvidence(models.Model):
    """Screenshots, logs, configs, captures attached to a SAT result/run."""

    class Kind(models.TextChoices):
        SCREENSHOT = "screenshot", _("Screenshot")
        LOG = "log", _("Log File")
        CONFIG = "config", _("Configuration File")
        NETWORK = "network", _("Network Capture")
        VIDEO = "video", _("Video")
        OTHER = "other", _("Other")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(SatTestRun, on_delete=models.CASCADE, related_name="evidence_files")
    result = models.ForeignKey(
        SatTestResult,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="evidence_files",
    )
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.OTHER)
    title = models.CharField(max_length=255, blank=True, default="")
    file = models.FileField(upload_to=sat_evidence_upload_to, max_length=512)
    original_name = models.CharField(max_length=255, blank=True, default="")
    content_type = models.CharField(max_length=128, blank=True, default="")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sat_evidence_uploads",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class SatDefect(models.Model):
    """Defect raised from a failed / blocked SAT case."""

    class Kind(models.TextChoices):
        BUG = "bug", _("Bug")
        IMPROVEMENT = "improvement", _("Improvement")
        CONFIGURATION = "configuration", _("Configuration Issue")
        HARDWARE = "hardware", _("Hardware Issue")
        NETWORK = "network", _("Network Issue")
        USER_ERROR = "user_error", _("User Error")

    class Severity(models.TextChoices):
        CRITICAL = "critical", _("Critical")
        HIGH = "high", _("High")
        MEDIUM = "medium", _("Medium")
        LOW = "low", _("Low")

    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        IN_PROGRESS = "in_progress", _("In Progress")
        RESOLVED = "resolved", _("Resolved")
        WONT_FIX = "wont_fix", _("Won't Fix")
        DUPLICATE = "duplicate", _("Duplicate")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(SatTestRun, on_delete=models.CASCADE, related_name="defects")
    result = models.ForeignKey(
        SatTestResult,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="defects",
    )
    kind = models.CharField(max_length=32, choices=Kind.choices, default=Kind.BUG)
    severity = models.CharField(max_length=16, choices=Severity.choices, default=Severity.HIGH)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN, db_index=True)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    test_id = models.CharField(max_length=32, blank=True, default="", db_index=True)
    equipment_id = models.CharField(max_length=64, blank=True, default="")
    department_id = models.IntegerField(null=True, blank=True)
    machine_name = models.CharField(max_length=255, blank=True, default="")
    node_id = models.CharField(max_length=64, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sat_defects_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "severity"])]

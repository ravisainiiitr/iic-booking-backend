"""
Department Sync Agent persistence models.

Equipment-centric design:
- Equipment is the permanent portal asset (booking / sync target).
- DepartmentSyncAgent is a replaceable Support PC / workstation node.
- An agent may optionally declare a primary Equipment (department-scoped).
- Additional instruments bind via AgentAssignment → EquipmentSyncProfile.
- EquipmentSyncProfile stores long-lived sync configuration only.
- AgentHeartbeat stores runtime telemetry.
- Bootstrap is the control-plane source of truth;
  heartbeat returns operational commands only.

sync.Laboratory remains for enterprise topology / historical assignment rows only.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _


class AgentLifecycleStatus(models.TextChoices):
    """
    Durable agent lifecycle only.

    Online/offline connectivity is never stored here. Derive it from
    last_heartbeat_at plus the configured heartbeat timeout.

    Enterprise operational states (Milestone 14) extend the original set
    without removing REGISTERED/ENROLLED/DISABLED/REVOKED.
    """

    REGISTERED = "REGISTERED", _("Registered")
    ENROLLED = "ENROLLED", _("Enrolled")
    ACTIVE = "ACTIVE", _("Active")
    MAINTENANCE = "MAINTENANCE", _("Maintenance")
    DRAINING = "DRAINING", _("Draining")
    OFFLINE = "OFFLINE", _("Offline (operational)")
    RECOVERING = "RECOVERING", _("Recovering")
    RETIRED = "RETIRED", _("Retired")
    DELETED = "DELETED", _("Deleted")
    DISABLED = "DISABLED", _("Disabled")
    REVOKED = "REVOKED", _("Revoked")


class SyncLogSeverity(models.TextChoices):
    DEBUG = "DEBUG", _("Debug")
    INFO = "INFO", _("Info")
    WARNING = "WARNING", _("Warning")
    ERROR = "ERROR", _("Error")
    CRITICAL = "CRITICAL", _("Critical")


class SyncLogCategory(models.TextChoices):
    """High-level event families; pair with stable event_code values."""

    SYNC = "SYNC", _("Sync")
    HEARTBEAT = "HEARTBEAT", _("Heartbeat")
    UPLOAD = "UPLOAD", _("Upload")
    BOOTSTRAP = "BOOTSTRAP", _("Bootstrap")
    AUTH = "AUTH", _("Authentication")
    ASSIGNMENT = "ASSIGNMENT", _("Assignment")
    ENTERPRISE = "ENTERPRISE", _("Enterprise")
    MONITORING = "MONITORING", _("Monitoring")
    UPDATES = "UPDATES", _("Updates")
    EXPERIMENTS = "EXPERIMENTS", _("Experiments")
    WORKSPACE = "WORKSPACE", _("Workspace")
    BOOKING = "BOOKING", _("Booking")
    DIAGNOSTICS = "DIAGNOSTICS", _("Diagnostics")
    AGENT = "AGENT", _("Agent")
    COMMAND = "COMMAND", _("Command")
    OTHER = "OTHER", _("Other")


class AlertSeverity(models.TextChoices):
    """Intelligent alerting severity (Milestone 15)."""

    INFO = "INFO", _("Info")
    WARNING = "WARNING", _("Warning")
    ERROR = "ERROR", _("Error")
    CRITICAL = "CRITICAL", _("Critical")


class AlertLifecycleStatus(models.TextChoices):
    """Alert lifecycle states (Milestone 15)."""

    NEW = "NEW", _("New")
    ACKNOWLEDGED = "ACKNOWLEDGED", _("Acknowledged")
    SUPPRESSED = "SUPPRESSED", _("Suppressed")
    RESOLVED = "RESOLVED", _("Resolved")
    EXPIRED = "EXPIRED", _("Expired")


class HistoricalMetricPeriod(models.TextChoices):
    """Aggregation buckets for historical metrics (Milestone 15)."""

    FIVE_MIN = "5m", _("5 minutes")
    FIFTEEN_MIN = "15m", _("15 minutes")
    HOURLY = "hourly", _("Hourly")
    DAILY = "daily", _("Daily")
    WEEKLY = "weekly", _("Weekly")
    MONTHLY = "monthly", _("Monthly")


class ReleaseChannel(models.TextChoices):
    """Agent update subscription channels (Milestone 16)."""

    PRODUCTION = "PRODUCTION", _("Production")
    STAGING = "STAGING", _("Staging")
    BETA = "BETA", _("Beta")
    DEVELOPMENT = "DEVELOPMENT", _("Development")
    DEPARTMENT = "DEPARTMENT", _("Department-specific")
    CUSTOM = "CUSTOM", _("Custom")


class ReleasePackageType(models.TextChoices):
    AGENT = "AGENT", _("Agent")
    PLUGIN = "PLUGIN", _("Plugin")
    CONFIGURATION = "CONFIGURATION", _("Configuration")
    EMERGENCY = "EMERGENCY", _("Emergency")
    HOTFIX = "HOTFIX", _("Hotfix")
    BETA = "BETA", _("Beta")
    LTS = "LTS", _("Long-Term Support")


class ReleasePackageStatus(models.TextChoices):
    DRAFT = "DRAFT", _("Draft")
    PUBLISHED = "PUBLISHED", _("Published")
    SUPERSEDED = "SUPERSEDED", _("Superseded")
    YANKED = "YANKED", _("Yanked")
    ARCHIVED = "ARCHIVED", _("Archived")


class UpdateLifecycleState(models.TextChoices):
    """Agent-side update state machine (Milestone 16)."""

    AVAILABLE = "AVAILABLE", _("Available")
    DOWNLOADING = "DOWNLOADING", _("Downloading")
    VERIFYING = "VERIFYING", _("Verifying")
    READY = "READY", _("Ready")
    INSTALLING = "INSTALLING", _("Installing")
    VALIDATING = "VALIDATING", _("Validating")
    COMPLETED = "COMPLETED", _("Completed")
    FAILED = "FAILED", _("Failed")
    ROLLING_BACK = "ROLLING_BACK", _("Rolling back")
    ROLLED_BACK = "ROLLED_BACK", _("Rolled back")
    CANCELLED = "CANCELLED", _("Cancelled")


class UpdateDeploymentStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    IN_PROGRESS = "IN_PROGRESS", _("In progress")
    COMPLETED = "COMPLETED", _("Completed")
    FAILED = "FAILED", _("Failed")
    ROLLED_BACK = "ROLLED_BACK", _("Rolled back")
    CANCELLED = "CANCELLED", _("Cancelled")


class RolloutStrategy(models.TextChoices):
    PERCENTAGE = "PERCENTAGE", _("Percentage")
    DEPARTMENT = "DEPARTMENT", _("Department")
    BUILDING = "BUILDING", _("Building")
    AGENT_GROUP = "AGENT_GROUP", _("Agent group")
    MANUAL = "MANUAL", _("Manual")
    IMMEDIATE = "IMMEDIATE", _("Immediate")


def default_enabled_features() -> dict:
    """Default capability map for new equipment sync profiles."""
    return {
        "watcher": True,
        "upload": True,
        "analysis": False,
        "diagnostics": True,
        "remote_execution": False,
    }


class Laboratory(models.Model):
    """
    Laboratory within a department.

    Retained after portal analysis: no equivalent Laboratory / Lab / Lab Section
    entity exists elsewhere. EquipmentGroup is quota-oriented; Equipment.location
    is unstructured text; lab-in-charge is an operator role, not a lab record.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    department = models.ForeignKey(
        "users.Department",
        on_delete=models.PROTECT,
        related_name="laboratories",
        verbose_name=_("Department"),
    )
    name = models.CharField(_("Name"), max_length=200)
    code = models.CharField(
        _("Code"),
        max_length=50,
        blank=True,
        default="",
        help_text=_("Optional short code unique within the department."),
    )
    description = models.TextField(_("Description"), blank=True, default="")
    location = models.CharField(_("Location"), max_length=255, blank=True, default="")
    is_active = models.BooleanField(_("Active"), default=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("Laboratory")
        verbose_name_plural = _("Laboratories")
        ordering = ["department__name", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["department", "code"],
                condition=~models.Q(code=""),
                name="sync_laboratory_unique_department_code",
            ),
        ]
        indexes = [
            models.Index(fields=["department", "is_active"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.department})"


class Building(models.Model):
    """Physical building within a department (Milestone 14 topology)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    department = models.ForeignKey(
        "users.Department",
        on_delete=models.PROTECT,
        related_name="sync_buildings",
        verbose_name=_("Department"),
    )
    name = models.CharField(_("Name"), max_length=200)
    code = models.CharField(_("Code"), max_length=50, blank=True, default="")
    address = models.CharField(_("Address"), max_length=500, blank=True, default="")
    campus = models.CharField(_("Campus"), max_length=200, blank=True, default="")
    is_active = models.BooleanField(_("Active"), default=True, db_index=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("Building")
        verbose_name_plural = _("Buildings")
        ordering = ["department__name", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["department", "code"],
                condition=~models.Q(code=""),
                name="sync_building_unique_department_code",
            ),
        ]
        indexes = [
            models.Index(fields=["department", "is_active"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.department})"


class DepartmentSyncAgent(models.Model):
    """
    Replaceable Windows laboratory workstation running the Department Sync Agent.

    Identity is agent UUID + hashed secret. Lifecycle status never encodes
    connectivity online/offline — derive connectivity from last_heartbeat_at and the
    configured heartbeat timeout. Denormalized last_* fields are heartbeat
    caches for admin listing only.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agent_uuid = models.UUIDField(
        _("Agent UUID"),
        default=uuid.uuid4,
        unique=True,
        db_index=True,
        editable=False,
        help_text=_("Public agent identity used for authentication."),
    )
    agent_name = models.CharField(_("Agent name"), max_length=200)
    department = models.ForeignKey(
        "users.Department",
        on_delete=models.PROTECT,
        related_name="department_sync_agents",
        verbose_name=_("Department"),
    )
    equipment = models.ForeignKey(
        "equipment.Equipment",
        on_delete=models.PROTECT,
        related_name="primary_sync_agents",
        null=True,
        blank=True,
        verbose_name=_("Equipment"),
        help_text=_(
            "Primary equipment for this agent. Choices are limited to the selected department. "
            "Additional instruments can still be assigned via Equipment Sync Profiles."
        ),
    )
    building = models.ForeignKey(
        Building,
        on_delete=models.SET_NULL,
        related_name="sync_agents",
        null=True,
        blank=True,
        verbose_name=_("Building"),
        help_text=_("Milestone 14 building topology node."),
    )
    custom_tags = models.JSONField(
        _("Custom tags"),
        default=list,
        blank=True,
        help_text=_("Free-form tags for enterprise grouping (e.g. ['gpu','floor-2'])."),
    )
    max_parallel_uploads = models.PositiveIntegerField(
        _("Max parallel uploads"),
        default=2,
        validators=[MinValueValidator(1)],
    )
    max_parallel_processing = models.PositiveIntegerField(
        _("Max parallel processing"),
        default=1,
        validators=[MinValueValidator(1)],
    )
    processing_capacity = models.PositiveIntegerField(
        _("Processing capacity score"),
        default=100,
        help_text=_("Relative capacity used by least-loaded scheduling (higher = more capacity)."),
    )
    machine_name = models.CharField(_("Machine name"), max_length=200, blank=True, default="")
    machine_guid = models.UUIDField(
        _("Machine GUID"),
        unique=True,
        db_index=True,
        help_text=_("Stable OS/machine identifier reported by the agent host."),
    )
    version = models.CharField(_("Agent version"), max_length=50, blank=True, default="")
    update_channel = models.CharField(
        _("Update channel"),
        max_length=32,
        choices=ReleaseChannel.choices,
        default=ReleaseChannel.PRODUCTION,
        db_index=True,
        help_text=_("Milestone 16 release channel subscription."),
    )
    operating_system = models.CharField(
        _("Operating system"),
        max_length=200,
        blank=True,
        default="",
    )
    status = models.CharField(
        _("Lifecycle status"),
        max_length=20,
        choices=AgentLifecycleStatus.choices,
        default=AgentLifecycleStatus.REGISTERED,
        db_index=True,
        help_text=_(
            "Lifecycle (REGISTERED / ENROLLED / ACTIVE / MAINTENANCE / DRAINING / "
            "OFFLINE / RECOVERING / RETIRED / DELETED / DISABLED / REVOKED). "
            "Connectivity online/offline is still derived from last_heartbeat_at."
        ),
    )
    agent_secret_hash = models.CharField(
        _("Agent secret hash"),
        max_length=128,
        blank=True,
        default="",
        help_text=_("Hashed long-lived agent secret. Never store the plaintext secret."),
    )
    agent_secret_rotated_at = models.DateTimeField(
        _("Agent secret rotated at"),
        null=True,
        blank=True,
    )
    enrollment_token_hash = models.CharField(
        _("Enrollment token hash"),
        max_length=128,
        blank=True,
        default="",
        help_text=_("Optional one-time enrollment token hash used during registration."),
    )
    access_token_hash = models.CharField(
        _("Access token hash"),
        max_length=128,
        blank=True,
        default="",
        help_text=_("Hashed revocable access token. Never store the plaintext token."),
    )
    access_token_expires_at = models.DateTimeField(
        _("Access token expires at"),
        null=True,
        blank=True,
    )
    access_token_issued_at = models.DateTimeField(
        _("Access token issued at"),
        null=True,
        blank=True,
    )
    bootstrap_required = models.BooleanField(
        _("Bootstrap required"),
        default=True,
        help_text=_("When set, heartbeat returns bootstrap_required until a successful bootstrap."),
    )
    restart_required = models.BooleanField(
        _("Restart required"),
        default=False,
        help_text=_("When set, heartbeat returns restart_required."),
    )
    upgrade_required = models.BooleanField(
        _("Upgrade required"),
        default=False,
        help_text=_("When set, heartbeat returns upgrade_required."),
    )
    is_active = models.BooleanField(
        _("Active"),
        default=True,
        db_index=True,
        help_text=_(
            "Convenience flag. Prefer status=DISABLED/REVOKED for durable lifecycle control."
        ),
    )

    # Denormalized runtime summaries (updated by heartbeat service; not config).
    last_heartbeat_at = models.DateTimeField(_("Last heartbeat at"), null=True, blank=True)
    last_seen_at = models.DateTimeField(_("Last seen at"), null=True, blank=True)
    last_boot_at = models.DateTimeField(_("Last boot at"), null=True, blank=True)
    last_reported_configuration_version = models.PositiveIntegerField(
        _("Last reported configuration version"),
        null=True,
        blank=True,
        help_text=_(
            "configuration_version reported by the agent via heartbeat. "
            "Used to detect BootstrapRequired without returning full config."
        ),
    )
    last_reported_schema_version = models.PositiveIntegerField(
        _("Last reported schema version"),
        null=True,
        blank=True,
        help_text=_(
            "Bootstrap document schema_version reported by the agent via heartbeat. "
            "Mismatch may require agent upgrade or bootstrap refresh."
        ),
    )

    # Milestone 12 — device identity & secure communications
    device_id = models.UUIDField(
        _("Device ID"),
        null=True,
        blank=True,
        db_index=True,
        help_text=_("Stable device identity (normally equals machine_guid)."),
    )
    device_public_key = models.TextField(
        _("Device public key (PEM/Base64)"),
        blank=True,
        default="",
    )
    certificate_thumbprint = models.CharField(
        _("Certificate thumbprint"),
        max_length=128,
        blank=True,
        default="",
        db_index=True,
    )
    certificate_pem = models.TextField(_("Certificate PEM"), blank=True, default="")
    certificate_expires_at = models.DateTimeField(
        _("Certificate expires at"),
        null=True,
        blank=True,
    )
    certificate_revoked_at = models.DateTimeField(
        _("Certificate revoked at"),
        null=True,
        blank=True,
    )
    signing_secret_hash = models.CharField(
        _("Request signing secret hash"),
        max_length=128,
        blank=True,
        default="",
        help_text=_("Hashed HMAC signing secret. Never store plaintext."),
    )
    api_key_hash = models.CharField(
        _("API key hash"),
        max_length=128,
        blank=True,
        default="",
    )
    api_key_rotated_at = models.DateTimeField(_("API key rotated at"), null=True, blank=True)
    security_version = models.PositiveIntegerField(_("Security version"), default=1)
    signing_required = models.BooleanField(
        _("Require request signatures"),
        default=False,
        help_text=_("When true, control-plane requests must include X-DSA-Signature headers."),
    )
    security_registration_status = models.CharField(
        _("Security registration status"),
        max_length=32,
        blank=True,
        default="UNREGISTERED",
        help_text=_("UNREGISTERED | REGISTERED | RENEWAL_PENDING | REVOKED"),
    )

    registered_at = models.DateTimeField(_("Registered at"), auto_now_add=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("Department Sync Agent")
        verbose_name_plural = _("Department Sync Agents")
        ordering = ["-registered_at"]
        indexes = [
            models.Index(fields=["department", "is_active"]),
            models.Index(fields=["equipment", "is_active"]),
            models.Index(fields=["status", "last_heartbeat_at"]),
            models.Index(fields=["certificate_thumbprint"]),
            models.Index(fields=["device_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.agent_name} ({self.machine_name or self.agent_uuid})"


class DeviceCertificate(models.Model):
    """Certificate lifecycle history for a Department Sync Agent (Milestone 12)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sync_agent = models.ForeignKey(
        DepartmentSyncAgent,
        on_delete=models.CASCADE,
        related_name="device_certificates",
        verbose_name=_("Sync agent"),
    )
    thumbprint = models.CharField(_("Thumbprint"), max_length=128, db_index=True)
    public_key = models.TextField(_("Public key"), blank=True, default="")
    certificate_pem = models.TextField(_("Certificate PEM"), blank=True, default="")
    issued_at = models.DateTimeField(_("Issued at"))
    expires_at = models.DateTimeField(_("Expires at"), null=True, blank=True)
    revoked_at = models.DateTimeField(_("Revoked at"), null=True, blank=True)
    is_current = models.BooleanField(_("Current"), default=True, db_index=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("Device Certificate")
        verbose_name_plural = _("Device Certificates")
        ordering = ["-issued_at"]
        indexes = [
            models.Index(fields=["sync_agent", "-issued_at"]),
            models.Index(fields=["thumbprint", "is_current"]),
        ]

    def __str__(self) -> str:
        return f"{self.thumbprint[:16]}… ({self.sync_agent_id})"


class AgentApiKey(models.Model):
    """Rotatable API keys for a Department Sync Agent (Milestone 12)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sync_agent = models.ForeignKey(
        DepartmentSyncAgent,
        on_delete=models.CASCADE,
        related_name="api_keys",
        verbose_name=_("Sync agent"),
    )
    key_id = models.CharField(_("Key ID"), max_length=64, db_index=True)
    key_hash = models.CharField(_("Key hash"), max_length=128)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)
    expires_at = models.DateTimeField(_("Expires at"), null=True, blank=True)
    revoked_at = models.DateTimeField(_("Revoked at"), null=True, blank=True)
    is_active = models.BooleanField(_("Active"), default=True, db_index=True)
    rotated_from = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rotations",
        verbose_name=_("Rotated from"),
    )

    class Meta:
        verbose_name = _("Agent API Key")
        verbose_name_plural = _("Agent API Keys")
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["sync_agent", "key_id"], name="sync_agentapikey_unique_key_id"),
        ]

    def __str__(self) -> str:
        return f"{self.key_id} ({self.sync_agent_id})"


class SecurityAuditEvent(models.Model):
    """Dedicated security audit trail (Milestone 12)."""

    id = models.BigAutoField(primary_key=True)
    sync_agent = models.ForeignKey(
        DepartmentSyncAgent,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="security_audit_events",
        verbose_name=_("Sync agent"),
    )
    event_code = models.CharField(_("Event code"), max_length=32, db_index=True)
    message = models.CharField(_("Message"), max_length=500)
    device_id = models.UUIDField(_("Device ID"), null=True, blank=True, db_index=True)
    agent_uuid = models.UUIDField(_("Agent UUID"), null=True, blank=True, db_index=True)
    correlation_id = models.UUIDField(_("Correlation ID"), null=True, blank=True, db_index=True)
    user_name = models.CharField(_("User"), max_length=200, blank=True, default="")
    ip_address = models.GenericIPAddressField(_("IP address"), null=True, blank=True)
    details = models.JSONField(_("Details"), default=dict, blank=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Security Audit Event")
        verbose_name_plural = _("Security Audit Events")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["event_code", "-created_at"]),
            models.Index(fields=["sync_agent", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.event_code}: {self.message}"


class AgentRecoveryEvent(models.Model):
    """Agent offline / disaster-recovery audit events (Milestone 13)."""

    id = models.BigAutoField(primary_key=True)
    sync_agent = models.ForeignKey(
        DepartmentSyncAgent,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recovery_events",
        verbose_name=_("Sync agent"),
    )
    event_code = models.CharField(_("Event code"), max_length=32, db_index=True)
    component = models.CharField(_("Component"), max_length=64, blank=True, default="")
    from_state = models.CharField(_("From state"), max_length=32, blank=True, default="")
    to_state = models.CharField(_("To state"), max_length=32, blank=True, default="")
    message = models.CharField(_("Message"), max_length=500)
    device_id = models.UUIDField(_("Device ID"), null=True, blank=True, db_index=True)
    agent_uuid = models.UUIDField(_("Agent UUID"), null=True, blank=True, db_index=True)
    correlation_id = models.UUIDField(_("Correlation ID"), null=True, blank=True, db_index=True)
    details = models.JSONField(_("Details"), default=dict, blank=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Agent Recovery Event")
        verbose_name_plural = _("Agent Recovery Events")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["event_code", "-created_at"]),
            models.Index(fields=["sync_agent", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.event_code}: {self.message}"


class AgentConflictResolution(models.Model):
    """Deterministic conflict resolutions reported by agents (Milestone 13)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sync_agent = models.ForeignKey(
        DepartmentSyncAgent,
        on_delete=models.CASCADE,
        related_name="conflict_resolutions",
        verbose_name=_("Sync agent"),
    )
    conflict_type = models.CharField(_("Conflict type"), max_length=64, db_index=True)
    resolution = models.CharField(_("Resolution"), max_length=64)
    upload_id = models.UUIDField(_("Upload ID"), null=True, blank=True, db_index=True)
    processing_id = models.UUIDField(_("Processing ID"), null=True, blank=True)
    correlation_id = models.UUIDField(_("Correlation ID"), null=True, blank=True, db_index=True)
    details = models.JSONField(_("Details"), default=dict, blank=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Agent Conflict Resolution")
        verbose_name_plural = _("Agent Conflict Resolutions")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["sync_agent", "conflict_type", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.conflict_type} → {self.resolution}"


class SyncAgentGroup(models.Model):
    """Named agent grouping for enterprise filters (Milestone 14)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    department = models.ForeignKey(
        "users.Department",
        on_delete=models.CASCADE,
        related_name="sync_agent_groups",
        verbose_name=_("Department"),
    )
    name = models.CharField(_("Name"), max_length=200)
    code = models.CharField(_("Code"), max_length=64, blank=True, default="")
    description = models.TextField(_("Description"), blank=True, default="")
    # Filter criteria: department/building/lab/equipment_type/capability/os/version/status/tags
    filter_criteria = models.JSONField(_("Filter criteria"), default=dict, blank=True)
    custom_tags = models.JSONField(_("Custom tags"), default=list, blank=True)
    is_active = models.BooleanField(_("Active"), default=True, db_index=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("Sync Agent Group")
        verbose_name_plural = _("Sync Agent Groups")
        ordering = ["department__name", "name"]
        indexes = [models.Index(fields=["department", "is_active"])]

    def __str__(self) -> str:
        return self.name


class SyncAgentAssignment(models.Model):
    """
    Enterprise assignment record (building / equipment / group / policy).

    Complements profile-level AgentAssignment with auditable enterprise history.
    """

    class AssignmentType(models.TextChoices):
        AUTOMATIC = "AUTOMATIC", _("Automatic")
        MANUAL = "MANUAL", _("Manual")
        PRIORITY = "PRIORITY", _("Priority")
        BUILDING = "BUILDING", _("Building")
        EQUIPMENT = "EQUIPMENT", _("Equipment")
        DEPARTMENT = "DEPARTMENT", _("Department")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sync_agent = models.ForeignKey(
        DepartmentSyncAgent,
        on_delete=models.CASCADE,
        related_name="enterprise_assignments",
        verbose_name=_("Sync agent"),
    )
    department = models.ForeignKey(
        "users.Department",
        on_delete=models.PROTECT,
        related_name="enterprise_agent_assignments",
        verbose_name=_("Department"),
    )
    building = models.ForeignKey(
        Building,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="enterprise_assignments",
        verbose_name=_("Building"),
    )
    laboratory = models.ForeignKey(
        Laboratory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="enterprise_assignments",
        verbose_name=_("Laboratory"),
    )
    equipment = models.ForeignKey(
        "equipment.Equipment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="enterprise_agent_assignments",
        verbose_name=_("Equipment"),
    )
    group = models.ForeignKey(
        SyncAgentGroup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assignments",
        verbose_name=_("Agent group"),
    )
    assignment_type = models.CharField(
        _("Assignment type"),
        max_length=32,
        choices=AssignmentType.choices,
        default=AssignmentType.MANUAL,
        db_index=True,
    )
    priority = models.PositiveIntegerField(_("Priority"), default=100)
    is_active = models.BooleanField(_("Active"), default=True, db_index=True)
    notes = models.TextField(_("Notes"), blank=True, default="")
    assigned_by = models.CharField(_("Assigned by"), max_length=200, blank=True, default="")
    correlation_id = models.UUIDField(_("Correlation ID"), null=True, blank=True, db_index=True)
    assigned_at = models.DateTimeField(_("Assigned at"), auto_now_add=True)
    unassigned_at = models.DateTimeField(_("Unassigned at"), null=True, blank=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("Sync Agent Enterprise Assignment")
        verbose_name_plural = _("Sync Agent Enterprise Assignments")
        ordering = ["-assigned_at"]
        indexes = [
            models.Index(fields=["department", "is_active"]),
            models.Index(fields=["sync_agent", "is_active"]),
            models.Index(fields=["assignment_type", "-assigned_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.assignment_type}: {self.sync_agent_id}"


class DepartmentTopology(models.Model):
    """Cached department topology snapshot for dashboards (Milestone 14)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    department = models.ForeignKey(
        "users.Department",
        on_delete=models.CASCADE,
        related_name="sync_topologies",
        verbose_name=_("Department"),
    )
    version = models.PositiveIntegerField(_("Version"), default=1)
    snapshot = models.JSONField(_("Snapshot"), default=dict, blank=True)
    building_count = models.PositiveIntegerField(_("Building count"), default=0)
    agent_count = models.PositiveIntegerField(_("Agent count"), default=0)
    equipment_count = models.PositiveIntegerField(_("Equipment count"), default=0)
    generated_at = models.DateTimeField(_("Generated at"), auto_now=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("Department Topology")
        verbose_name_plural = _("Department Topologies")
        ordering = ["-generated_at"]
        indexes = [models.Index(fields=["department", "-generated_at"])]

    def __str__(self) -> str:
        return f"Topology {self.department_id} v{self.version}"


class AgentCapability(models.Model):
    """Historical agent capability snapshot (Milestone 14)."""

    id = models.BigAutoField(primary_key=True)
    sync_agent = models.ForeignKey(
        DepartmentSyncAgent,
        on_delete=models.CASCADE,
        related_name="capability_snapshots",
        verbose_name=_("Sync agent"),
    )
    reported_at = models.DateTimeField(_("Reported at"), db_index=True)
    supported_plugins = models.JSONField(_("Supported plugins"), default=list, blank=True)
    plugin_versions = models.JSONField(_("Plugin versions"), default=dict, blank=True)
    storage_free_bytes = models.BigIntegerField(_("Storage free bytes"), null=True, blank=True)
    storage_total_bytes = models.BigIntegerField(_("Storage total bytes"), null=True, blank=True)
    cpu_percent = models.FloatField(_("CPU percent"), null=True, blank=True)
    memory_percent = models.FloatField(_("Memory percent"), null=True, blank=True)
    network_summary = models.CharField(_("Network summary"), max_length=200, blank=True, default="")
    windows_version = models.CharField(_("Windows version"), max_length=100, blank=True, default="")
    schema_version = models.PositiveIntegerField(_("Schema version"), null=True, blank=True)
    recovery_version = models.PositiveIntegerField(_("Recovery version"), null=True, blank=True)
    security_version = models.PositiveIntegerField(_("Security version"), null=True, blank=True)
    processing_capacity = models.PositiveIntegerField(_("Processing capacity"), null=True, blank=True)
    max_parallel_uploads = models.PositiveIntegerField(_("Max parallel uploads"), null=True, blank=True)
    max_parallel_processing = models.PositiveIntegerField(_("Max parallel processing"), null=True, blank=True)
    capabilities = models.JSONField(_("Capabilities"), default=dict, blank=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("Agent Capability")
        verbose_name_plural = _("Agent Capabilities")
        ordering = ["-reported_at"]
        indexes = [models.Index(fields=["sync_agent", "-reported_at"])]

    def __str__(self) -> str:
        return f"Capability {self.sync_agent_id} @ {self.reported_at}"


class AgentStatistics(models.Model):
    """Aggregated agent/department/building statistics (Milestone 14)."""

    id = models.BigAutoField(primary_key=True)
    department = models.ForeignKey(
        "users.Department",
        on_delete=models.CASCADE,
        related_name="agent_statistics",
        verbose_name=_("Department"),
        null=True,
        blank=True,
    )
    building = models.ForeignKey(
        Building,
        on_delete=models.CASCADE,
        related_name="agent_statistics",
        verbose_name=_("Building"),
        null=True,
        blank=True,
    )
    sync_agent = models.ForeignKey(
        DepartmentSyncAgent,
        on_delete=models.CASCADE,
        related_name="statistics",
        verbose_name=_("Sync agent"),
        null=True,
        blank=True,
    )
    period_start = models.DateTimeField(_("Period start"), db_index=True)
    period_end = models.DateTimeField(_("Period end"), db_index=True)
    metrics = models.JSONField(_("Metrics"), default=dict, blank=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("Agent Statistics")
        verbose_name_plural = _("Agent Statistics")
        ordering = ["-period_end"]
        indexes = [
            models.Index(fields=["department", "-period_end"]),
            models.Index(fields=["sync_agent", "-period_end"]),
        ]

    def __str__(self) -> str:
        return f"Stats {self.period_start}–{self.period_end}"


class EnterpriseAuditEvent(models.Model):
    """Enterprise topology / assignment audit trail (Milestone 14)."""

    id = models.BigAutoField(primary_key=True)
    event_code = models.CharField(_("Event code"), max_length=32, db_index=True)
    message = models.CharField(_("Message"), max_length=500)
    department_id = models.CharField(_("Department ID"), max_length=64, blank=True, default="", db_index=True)
    building_id = models.CharField(_("Building ID"), max_length=64, blank=True, default="", db_index=True)
    agent_id = models.CharField(_("Agent ID"), max_length=64, blank=True, default="", db_index=True)
    correlation_id = models.UUIDField(_("Correlation ID"), null=True, blank=True, db_index=True)
    user_name = models.CharField(_("User"), max_length=200, blank=True, default="")
    details = models.JSONField(_("Details"), default=dict, blank=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Enterprise Audit Event")
        verbose_name_plural = _("Enterprise Audit Events")
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["event_code", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.event_code}: {self.message}"


class AgentHealthSnapshot(models.Model):
    """Point-in-time agent health sample (Milestone 15). Read-only observability."""

    id = models.BigAutoField(primary_key=True)
    sync_agent = models.ForeignKey(
        DepartmentSyncAgent,
        on_delete=models.CASCADE,
        related_name="health_snapshots",
        verbose_name=_("Sync agent"),
    )
    department = models.ForeignKey(
        "users.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_health_snapshots",
        verbose_name=_("Department"),
    )
    building = models.ForeignKey(
        Building,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_health_snapshots",
        verbose_name=_("Building"),
    )
    reported_at = models.DateTimeField(_("Reported at"), db_index=True)
    overall_status = models.CharField(_("Overall status"), max_length=32, blank=True, default="")
    overall_severity = models.CharField(_("Overall severity"), max_length=32, blank=True, default="")
    cpu_percent = models.FloatField(_("CPU percent"), null=True, blank=True)
    memory_mb = models.FloatField(_("Memory MB"), null=True, blank=True)
    memory_percent = models.FloatField(_("Memory percent"), null=True, blank=True)
    disk_used_percent = models.FloatField(_("Disk used percent"), null=True, blank=True)
    disk_free_bytes = models.BigIntegerField(_("Disk free bytes"), null=True, blank=True)
    sqlite_size_bytes = models.BigIntegerField(_("SQLite size bytes"), null=True, blank=True)
    upload_queue_size = models.PositiveIntegerField(_("Upload queue size"), default=0)
    processing_queue_size = models.PositiveIntegerField(_("Processing queue size"), default=0)
    discovery_queue_size = models.PositiveIntegerField(_("Discovery queue size"), default=0)
    heartbeat_latency_ms = models.FloatField(_("Heartbeat latency ms"), null=True, blank=True)
    portal_latency_ms = models.FloatField(_("Portal latency ms"), null=True, blank=True)
    upload_rate = models.FloatField(_("Upload rate"), null=True, blank=True)
    processing_rate = models.FloatField(_("Processing rate"), null=True, blank=True)
    recovery_state = models.CharField(_("Recovery state"), max_length=64, blank=True, default="")
    security_status = models.CharField(_("Security status"), max_length=64, blank=True, default="")
    plugin_status = models.CharField(_("Plugin status"), max_length=64, blank=True, default="")
    network_available = models.BooleanField(_("Network available"), null=True, blank=True)
    running_workers = models.PositiveIntegerField(_("Running workers"), default=0)
    uptime_seconds = models.FloatField(_("Uptime seconds"), null=True, blank=True)
    agent_version = models.CharField(_("Agent version"), max_length=64, blank=True, default="")
    schema_version = models.PositiveIntegerField(_("Schema version"), null=True, blank=True)
    metrics = models.JSONField(_("Metrics"), default=dict, blank=True)
    correlation_id = models.UUIDField(_("Correlation ID"), null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("Agent Health Snapshot")
        verbose_name_plural = _("Agent Health Snapshots")
        ordering = ["-reported_at"]
        indexes = [
            models.Index(fields=["sync_agent", "-reported_at"]),
            models.Index(fields=["department", "-reported_at"]),
            models.Index(fields=["-reported_at"]),
            models.Index(fields=["overall_severity", "-reported_at"]),
        ]

    def __str__(self) -> str:
        return f"Health {self.sync_agent_id} @ {self.reported_at}"


class AgentPerformanceMetric(models.Model):
    """Named performance sample from an agent (Milestone 15)."""

    id = models.BigAutoField(primary_key=True)
    sync_agent = models.ForeignKey(
        DepartmentSyncAgent,
        on_delete=models.CASCADE,
        related_name="performance_metrics",
        verbose_name=_("Sync agent"),
    )
    department = models.ForeignKey(
        "users.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_performance_metrics",
        verbose_name=_("Department"),
    )
    category = models.CharField(_("Category"), max_length=64, db_index=True)
    name = models.CharField(_("Name"), max_length=128, db_index=True)
    value = models.FloatField(_("Value"), null=True, blank=True)
    unit = models.CharField(_("Unit"), max_length=32, blank=True, default="")
    reported_at = models.DateTimeField(_("Reported at"), db_index=True)
    details = models.JSONField(_("Details"), default=dict, blank=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("Agent Performance Metric")
        verbose_name_plural = _("Agent Performance Metrics")
        ordering = ["-reported_at"]
        indexes = [
            models.Index(fields=["sync_agent", "category", "-reported_at"]),
            models.Index(fields=["name", "-reported_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.category}.{self.name}={self.value}"


class AlertEvent(models.Model):
    """Intelligent alert with full lifecycle (Milestone 15)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    correlation_id = models.UUIDField(_("Correlation ID"), null=True, blank=True, db_index=True)
    department = models.ForeignKey(
        "users.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="alert_events",
        verbose_name=_("Department"),
    )
    building = models.ForeignKey(
        Building,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="alert_events",
        verbose_name=_("Building"),
    )
    sync_agent = models.ForeignKey(
        DepartmentSyncAgent,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="alert_events",
        verbose_name=_("Sync agent"),
    )
    equipment = models.ForeignKey(
        "equipment.Equipment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="alert_events",
        verbose_name=_("Equipment"),
    )
    rule_code = models.CharField(_("Rule code"), max_length=64, db_index=True)
    category = models.CharField(_("Category"), max_length=64, db_index=True)
    severity = models.CharField(
        _("Severity"),
        max_length=16,
        choices=AlertSeverity.choices,
        default=AlertSeverity.WARNING,
        db_index=True,
    )
    status = models.CharField(
        _("Status"),
        max_length=16,
        choices=AlertLifecycleStatus.choices,
        default=AlertLifecycleStatus.NEW,
        db_index=True,
    )
    title = models.CharField(_("Title"), max_length=200)
    message = models.CharField(_("Message"), max_length=1000)
    fingerprint = models.CharField(_("Fingerprint"), max_length=128, db_index=True)
    details = models.JSONField(_("Details"), default=dict, blank=True)
    resolution = models.CharField(_("Resolution"), max_length=500, blank=True, default="")
    acknowledged_by = models.CharField(_("Acknowledged by"), max_length=200, blank=True, default="")
    acknowledged_at = models.DateTimeField(_("Acknowledged at"), null=True, blank=True)
    resolved_by = models.CharField(_("Resolved by"), max_length=200, blank=True, default="")
    resolved_at = models.DateTimeField(_("Resolved at"), null=True, blank=True)
    expires_at = models.DateTimeField(_("Expires at"), null=True, blank=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("Alert Event")
        verbose_name_plural = _("Alert Events")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "severity", "-created_at"]),
            models.Index(fields=["sync_agent", "status", "-created_at"]),
            models.Index(fields=["fingerprint", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.severity} {self.rule_code}: {self.title}"


class SystemCapacitySnapshot(models.Model):
    """Enterprise / department capacity sample (Milestone 15)."""

    id = models.BigAutoField(primary_key=True)
    department = models.ForeignKey(
        "users.Department",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="capacity_snapshots",
        verbose_name=_("Department"),
    )
    building = models.ForeignKey(
        Building,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="capacity_snapshots",
        verbose_name=_("Building"),
    )
    sync_agent = models.ForeignKey(
        DepartmentSyncAgent,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="capacity_snapshots",
        verbose_name=_("Sync agent"),
    )
    reported_at = models.DateTimeField(_("Reported at"), db_index=True)
    storage_used_bytes = models.BigIntegerField(_("Storage used bytes"), null=True, blank=True)
    database_size_bytes = models.BigIntegerField(_("Database size bytes"), null=True, blank=True)
    upload_volume = models.PositiveIntegerField(_("Upload volume"), default=0)
    processing_volume = models.PositiveIntegerField(_("Processing volume"), default=0)
    plugin_count = models.PositiveIntegerField(_("Plugin count"), default=0)
    equipment_count = models.PositiveIntegerField(_("Equipment count"), default=0)
    agent_count = models.PositiveIntegerField(_("Agent count"), default=0)
    average_upload_size_bytes = models.FloatField(_("Average upload size bytes"), null=True, blank=True)
    peak_processing = models.PositiveIntegerField(_("Peak processing"), default=0)
    peak_queue = models.PositiveIntegerField(_("Peak queue"), default=0)
    metrics = models.JSONField(_("Metrics"), default=dict, blank=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("System Capacity Snapshot")
        verbose_name_plural = _("System Capacity Snapshots")
        ordering = ["-reported_at"]
        indexes = [
            models.Index(fields=["department", "-reported_at"]),
            models.Index(fields=["sync_agent", "-reported_at"]),
        ]

    def __str__(self) -> str:
        return f"Capacity @ {self.reported_at}"


class HistoricalMetric(models.Model):
    """Time-bucketed aggregated metrics (Milestone 15)."""

    id = models.BigAutoField(primary_key=True)
    department = models.ForeignKey(
        "users.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="historical_metrics",
        verbose_name=_("Department"),
    )
    building = models.ForeignKey(
        Building,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="historical_metrics",
        verbose_name=_("Building"),
    )
    sync_agent = models.ForeignKey(
        DepartmentSyncAgent,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="historical_metrics",
        verbose_name=_("Sync agent"),
    )
    period = models.CharField(
        _("Period"),
        max_length=16,
        choices=HistoricalMetricPeriod.choices,
        db_index=True,
    )
    metric_name = models.CharField(_("Metric name"), max_length=128, db_index=True)
    period_start = models.DateTimeField(_("Period start"), db_index=True)
    period_end = models.DateTimeField(_("Period end"), db_index=True)
    sample_count = models.PositiveIntegerField(_("Sample count"), default=0)
    min_value = models.FloatField(_("Min"), null=True, blank=True)
    max_value = models.FloatField(_("Max"), null=True, blank=True)
    avg_value = models.FloatField(_("Average"), null=True, blank=True)
    sum_value = models.FloatField(_("Sum"), null=True, blank=True)
    last_value = models.FloatField(_("Last"), null=True, blank=True)
    details = models.JSONField(_("Details"), default=dict, blank=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("Historical Metric")
        verbose_name_plural = _("Historical Metrics")
        ordering = ["-period_end"]
        indexes = [
            models.Index(fields=["period", "metric_name", "-period_end"]),
            models.Index(fields=["sync_agent", "period", "-period_end"]),
            models.Index(fields=["department", "period", "-period_end"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["sync_agent", "period", "metric_name", "period_start"],
                name="uniq_historical_metric_agent_bucket",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.metric_name} [{self.period}] {self.period_start}"


class ReleasePackage(models.Model):
    """Signed software/config package metadata (Milestone 16)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    package_type = models.CharField(
        _("Package type"),
        max_length=32,
        choices=ReleasePackageType.choices,
        db_index=True,
    )
    channel = models.CharField(
        _("Channel"),
        max_length=32,
        choices=ReleaseChannel.choices,
        default=ReleaseChannel.PRODUCTION,
        db_index=True,
    )
    version = models.CharField(_("Version"), max_length=64, db_index=True)
    display_name = models.CharField(_("Display name"), max_length=200)
    description = models.TextField(_("Description"), blank=True, default="")
    status = models.CharField(
        _("Status"),
        max_length=16,
        choices=ReleasePackageStatus.choices,
        default=ReleasePackageStatus.DRAFT,
        db_index=True,
    )
    download_url = models.URLField(_("Download URL"), max_length=1000, blank=True, default="")
    package_size_bytes = models.BigIntegerField(_("Package size bytes"), default=0)
    sha256 = models.CharField(_("SHA-256"), max_length=64, blank=True, default="")
    signature = models.TextField(_("Digital signature"), blank=True, default="")
    publisher = models.CharField(_("Trusted publisher"), max_length=200, blank=True, default="IIC Portal")
    min_agent_version = models.CharField(_("Minimum agent version"), max_length=64, blank=True, default="")
    min_schema_version = models.PositiveIntegerField(_("Minimum schema version"), null=True, blank=True)
    security_version = models.PositiveIntegerField(_("Security version"), null=True, blank=True)
    recovery_version = models.PositiveIntegerField(_("Recovery version"), null=True, blank=True)
    api_version = models.CharField(_("API version"), max_length=32, blank=True, default="")
    compatibility = models.JSONField(_("Compatibility matrix"), default=dict, blank=True)
    dependencies = models.JSONField(_("Dependencies"), default=list, blank=True)
    plugin_id = models.CharField(_("Plugin ID"), max_length=128, blank=True, default="")
    department = models.ForeignKey(
        "users.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="release_packages",
        verbose_name=_("Department scope"),
    )
    created_by = models.CharField(_("Created by"), max_length=200, blank=True, default="")
    published_at = models.DateTimeField(_("Published at"), null=True, blank=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("Release Package")
        verbose_name_plural = _("Release Packages")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["package_type", "channel", "status"]),
            models.Index(fields=["version", "package_type"]),
        ]

    def __str__(self) -> str:
        return f"{self.package_type} {self.version} ({self.channel})"


class ReleaseManifest(models.Model):
    """Published manifest document for a release package (Milestone 16)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    package = models.ForeignKey(
        ReleasePackage,
        on_delete=models.CASCADE,
        related_name="manifests",
        verbose_name=_("Package"),
    )
    manifest_version = models.PositiveIntegerField(_("Manifest version"), default=1)
    document = models.JSONField(_("Manifest document"), default=dict, blank=True)
    document_sha256 = models.CharField(_("Document SHA-256"), max_length=64, blank=True, default="")
    signature = models.TextField(_("Manifest signature"), blank=True, default="")
    is_active = models.BooleanField(_("Is active"), default=True, db_index=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("Release Manifest")
        verbose_name_plural = _("Release Manifests")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Manifest {self.package_id} v{self.manifest_version}"


class UpdateDeployment(models.Model):
    """Staged rollout / deployment of a release (Milestone 16)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    package = models.ForeignKey(
        ReleasePackage,
        on_delete=models.CASCADE,
        related_name="deployments",
        verbose_name=_("Package"),
    )
    strategy = models.CharField(
        _("Rollout strategy"),
        max_length=32,
        choices=RolloutStrategy.choices,
        default=RolloutStrategy.MANUAL,
    )
    status = models.CharField(
        _("Status"),
        max_length=16,
        choices=UpdateDeploymentStatus.choices,
        default=UpdateDeploymentStatus.PENDING,
        db_index=True,
    )
    channel = models.CharField(
        _("Channel"),
        max_length=32,
        choices=ReleaseChannel.choices,
        default=ReleaseChannel.PRODUCTION,
    )
    percentage = models.PositiveIntegerField(_("Percentage"), default=100)
    department = models.ForeignKey(
        "users.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="update_deployments",
        verbose_name=_("Department"),
    )
    building = models.ForeignKey(
        Building,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="update_deployments",
        verbose_name=_("Building"),
    )
    agent_group = models.ForeignKey(
        SyncAgentGroup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="update_deployments",
        verbose_name=_("Agent group"),
    )
    scheduled_at = models.DateTimeField(_("Scheduled at"), null=True, blank=True)
    maintenance_window_start = models.DateTimeField(_("Maintenance window start"), null=True, blank=True)
    maintenance_window_end = models.DateTimeField(_("Maintenance window end"), null=True, blank=True)
    requires_approval = models.BooleanField(_("Requires approval"), default=False)
    approved_by = models.CharField(_("Approved by"), max_length=200, blank=True, default="")
    approved_at = models.DateTimeField(_("Approved at"), null=True, blank=True)
    target_agent_ids = models.JSONField(_("Target agent IDs"), default=list, blank=True)
    progress = models.JSONField(_("Progress"), default=dict, blank=True)
    correlation_id = models.UUIDField(_("Correlation ID"), null=True, blank=True, db_index=True)
    created_by = models.CharField(_("Created by"), max_length=200, blank=True, default="")
    started_at = models.DateTimeField(_("Started at"), null=True, blank=True)
    completed_at = models.DateTimeField(_("Completed at"), null=True, blank=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("Update Deployment")
        verbose_name_plural = _("Update Deployments")
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "-created_at"])]

    def __str__(self) -> str:
        return f"Deploy {self.package_id} [{self.status}]"


class UpdateHistory(models.Model):
    """Per-agent update attempt history (Milestone 16)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sync_agent = models.ForeignKey(
        DepartmentSyncAgent,
        on_delete=models.CASCADE,
        related_name="update_history",
        verbose_name=_("Sync agent"),
    )
    package = models.ForeignKey(
        ReleasePackage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="update_history",
        verbose_name=_("Package"),
    )
    deployment = models.ForeignKey(
        UpdateDeployment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="history",
        verbose_name=_("Deployment"),
    )
    department = models.ForeignKey(
        "users.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="update_history",
        verbose_name=_("Department"),
    )
    building = models.ForeignKey(
        Building,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="update_history",
        verbose_name=_("Building"),
    )
    from_version = models.CharField(_("From version"), max_length=64, blank=True, default="")
    to_version = models.CharField(_("To version"), max_length=64, blank=True, default="")
    state = models.CharField(
        _("State"),
        max_length=32,
        choices=UpdateLifecycleState.choices,
        default=UpdateLifecycleState.AVAILABLE,
        db_index=True,
    )
    package_type = models.CharField(_("Package type"), max_length=32, blank=True, default="")
    message = models.CharField(_("Message"), max_length=500, blank=True, default="")
    download_bytes = models.BigIntegerField(_("Download bytes"), default=0)
    download_ms = models.PositiveIntegerField(_("Download ms"), null=True, blank=True)
    install_ms = models.PositiveIntegerField(_("Install ms"), null=True, blank=True)
    validation_ms = models.PositiveIntegerField(_("Validation ms"), null=True, blank=True)
    details = models.JSONField(_("Details"), default=dict, blank=True)
    correlation_id = models.UUIDField(_("Correlation ID"), null=True, blank=True, db_index=True)
    started_at = models.DateTimeField(_("Started at"), auto_now_add=True, db_index=True)
    completed_at = models.DateTimeField(_("Completed at"), null=True, blank=True)

    class Meta:
        verbose_name = _("Update History")
        verbose_name_plural = _("Update Histories")
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["sync_agent", "-started_at"]),
            models.Index(fields=["state", "-started_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.sync_agent_id} {self.from_version}→{self.to_version} [{self.state}]"


class RollbackHistory(models.Model):
    """Rollback events for agent/plugin/config updates (Milestone 16)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sync_agent = models.ForeignKey(
        DepartmentSyncAgent,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rollback_history",
        verbose_name=_("Sync agent"),
    )
    package = models.ForeignKey(
        ReleasePackage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rollback_history",
        verbose_name=_("Package"),
    )
    update_history = models.ForeignKey(
        UpdateHistory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rollbacks",
        verbose_name=_("Update history"),
    )
    department = models.ForeignKey(
        "users.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rollback_history",
        verbose_name=_("Department"),
    )
    from_version = models.CharField(_("From version"), max_length=64, blank=True, default="")
    to_version = models.CharField(_("To version"), max_length=64, blank=True, default="")
    reason = models.CharField(_("Reason"), max_length=500)
    automatic = models.BooleanField(_("Automatic"), default=False)
    validated = models.BooleanField(_("Validated"), default=False)
    details = models.JSONField(_("Details"), default=dict, blank=True)
    correlation_id = models.UUIDField(_("Correlation ID"), null=True, blank=True, db_index=True)
    created_by = models.CharField(_("Created by"), max_length=200, blank=True, default="")
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Rollback History")
        verbose_name_plural = _("Rollback Histories")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Rollback {self.from_version}→{self.to_version}"


class ConfigurationVersion(models.Model):
    """Portal-published configuration release (Milestone 16). Distinct from agent-local SQLite entity."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    version_label = models.CharField(_("Version label"), max_length=64, db_index=True)
    department = models.ForeignKey(
        "users.Department",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="configuration_versions",
        verbose_name=_("Department"),
    )
    package = models.ForeignKey(
        ReleasePackage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="configuration_versions",
        verbose_name=_("Release package"),
    )
    content_hash = models.CharField(_("Content hash"), max_length=64, blank=True, default="")
    content = models.JSONField(_("Content"), default=dict, blank=True)
    is_active = models.BooleanField(_("Is active"), default=False, db_index=True)
    created_by = models.CharField(_("Created by"), max_length=200, blank=True, default="")
    published_at = models.DateTimeField(_("Published at"), null=True, blank=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("Configuration Version")
        verbose_name_plural = _("Configuration Versions")
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["department", "-created_at"])]

    def __str__(self) -> str:
        return f"Config {self.version_label}"


class PluginRelease(models.Model):
    """Plugin-specific release metadata (Milestone 16)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    package = models.OneToOneField(
        ReleasePackage,
        on_delete=models.CASCADE,
        related_name="plugin_release",
        verbose_name=_("Package"),
    )
    plugin_id = models.CharField(_("Plugin ID"), max_length=128, db_index=True)
    plugin_name = models.CharField(_("Plugin name"), max_length=200, blank=True, default="")
    plugin_version = models.CharField(_("Plugin version"), max_length=64)
    supports_hot_reload = models.BooleanField(_("Supports hot reload"), default=True)
    requires_agent_restart = models.BooleanField(_("Requires agent restart"), default=False)
    min_agent_version = models.CharField(_("Minimum agent version"), max_length=64, blank=True, default="")
    compatibility = models.JSONField(_("Compatibility"), default=dict, blank=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("Plugin Release")
        verbose_name_plural = _("Plugin Releases")
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["plugin_id", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.plugin_id}@{self.plugin_version}"


class ExperimentSessionStatus(models.TextChoices):
    """Experiment lifecycle (Milestone 18)."""

    SCHEDULED = "SCHEDULED", _("Scheduled")
    PREPARING = "PREPARING", _("Preparing")
    RUNNING = "RUNNING", _("Running")
    PAUSED = "PAUSED", _("Paused")
    COMPLETED = "COMPLETED", _("Completed")
    CANCELLED = "CANCELLED", _("Cancelled")
    FAILED = "FAILED", _("Failed")


class InstrumentPluginCatalog(models.Model):
    """Discoverable instrument plugin catalog entry (Milestone 18)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plugin_id = models.CharField(_("Plugin ID"), max_length=128, unique=True, db_index=True)
    display_name = models.CharField(_("Display name"), max_length=200)
    instrument_type = models.CharField(_("Instrument type"), max_length=64, db_index=True)
    version = models.CharField(_("Version"), max_length=64, blank=True, default="1.0.0")
    description = models.TextField(_("Description"), blank=True, default="")
    capabilities = models.JSONField(_("Capabilities"), default=dict, blank=True)
    supported_task_types = models.JSONField(_("Supported task types"), default=list, blank=True)
    is_active = models.BooleanField(_("Is active"), default=True, db_index=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("Instrument Plugin Catalog")
        verbose_name_plural = _("Instrument Plugin Catalog")
        ordering = ["display_name"]

    def __str__(self) -> str:
        return f"{self.display_name} ({self.plugin_id})"


class ExperimentSession(models.Model):
    """Portal-visible experiment session for live/ops monitoring (Milestone 18)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    experiment_id = models.UUIDField(_("Experiment ID"), unique=True, db_index=True)
    sync_agent = models.ForeignKey(
        DepartmentSyncAgent,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="experiment_sessions",
        verbose_name=_("Sync agent"),
    )
    department = models.ForeignKey(
        "users.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="experiment_sessions",
        verbose_name=_("Department"),
    )
    equipment = models.ForeignKey(
        "equipment.Equipment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="experiment_sessions",
        verbose_name=_("Equipment"),
    )
    booking_id = models.CharField(_("Booking ID"), max_length=64, blank=True, default="", db_index=True)
    workspace_path = models.CharField(_("Workspace"), max_length=1000, blank=True, default="")
    operator_name = models.CharField(_("Operator"), max_length=200, blank=True, default="")
    plugin_id = models.CharField(_("Plugin"), max_length=128, db_index=True)
    plugin_version = models.CharField(_("Plugin version"), max_length=64, blank=True, default="")
    status = models.CharField(
        _("Status"),
        max_length=16,
        choices=ExperimentSessionStatus.choices,
        default=ExperimentSessionStatus.SCHEDULED,
        db_index=True,
    )
    current_step = models.CharField(_("Current step"), max_length=200, blank=True, default="")
    session_start = models.DateTimeField(_("Session start"), null=True, blank=True)
    session_end = models.DateTimeField(_("Session end"), null=True, blank=True)
    metadata = models.JSONField(_("Metadata"), default=dict, blank=True)
    execution_history = models.JSONField(_("Execution history"), default=list, blank=True)
    correlation_id = models.UUIDField(_("Correlation ID"), null=True, blank=True, db_index=True)
    last_error = models.CharField(_("Last error"), max_length=1000, blank=True, default="")
    duration_ms = models.PositiveIntegerField(_("Duration ms"), null=True, blank=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("Experiment Session")
        verbose_name_plural = _("Experiment Sessions")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["sync_agent", "-created_at"]),
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["plugin_id", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"Experiment {self.experiment_id} [{self.status}]"


class ExperimentTelemetrySnapshot(models.Model):
    """Aggregated experiment/plugin telemetry from agents (Milestone 18)."""

    id = models.BigAutoField(primary_key=True)
    sync_agent = models.ForeignKey(
        DepartmentSyncAgent,
        on_delete=models.CASCADE,
        related_name="experiment_telemetry",
        verbose_name=_("Sync agent"),
    )
    department = models.ForeignKey(
        "users.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="experiment_telemetry",
        verbose_name=_("Department"),
    )
    reported_at = models.DateTimeField(_("Reported at"), db_index=True)
    experiments_completed = models.PositiveIntegerField(default=0)
    experiments_failed = models.PositiveIntegerField(default=0)
    recovery_count = models.PositiveIntegerField(default=0)
    total_duration_ms = models.FloatField(default=0)
    total_plugin_execution_ms = models.FloatField(default=0)
    instrument_availability = models.JSONField(default=dict, blank=True)
    plugin_versions = models.JSONField(default=dict, blank=True)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Experiment Telemetry Snapshot")
        verbose_name_plural = _("Experiment Telemetry Snapshots")
        ordering = ["-reported_at"]
        indexes = [models.Index(fields=["sync_agent", "-reported_at"])]


class EquipmentSyncProfile(models.Model):
    """
    Long-lived DSA configuration for one Equipment.

    Stores static sync settings only. Do not put online status, CPU, memory,
    queue size, or last-upload here — those belong on AgentHeartbeat.

    Versioning:
    - configuration_version: admin changed sync settings → BootstrapRequired
    - schema_version: bootstrap document shape changed → upgrade / bootstrap refresh
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    equipment = models.OneToOneField(
        "equipment.Equipment",
        on_delete=models.CASCADE,
        related_name="sync_profile",
        verbose_name=_("Equipment"),
    )
    building = models.ForeignKey(
        Building,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="equipment_sync_profiles",
        verbose_name=_("Building"),
    )
    primary_agent = models.ForeignKey(
        DepartmentSyncAgent,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="primary_equipment_profiles",
        verbose_name=_("Primary agent"),
    )
    backup_agent = models.ForeignKey(
        DepartmentSyncAgent,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="backup_equipment_profiles",
        verbose_name=_("Backup agent"),
        help_text=_("Reserved for future HA failover."),
    )
    ownership_history = models.JSONField(
        _("Ownership history"),
        default=list,
        blank=True,
        help_text=_("Append-only ownership change records."),
    )
    hostname = models.CharField(
        _("Instrument hostname"),
        max_length=200,
        blank=True,
        default="",
        help_text=_("Instrument PC hostname. Replacing the PC updates this profile, not Equipment."),
    )
    ip_address = models.CharField(
        _("Instrument IP address"),
        max_length=64,
        blank=True,
        default="",
    )
    share_name = models.CharField(
        _("Share name"),
        max_length=200,
        blank=True,
        default="",
        help_text=_("SMB share name on the instrument PC (e.g. Results)."),
    )
    unc_path = models.CharField(
        _("UNC path"),
        max_length=500,
        blank=True,
        default="",
        help_text=_(r"Full UNC path, e.g. \\192.168.1.2\Results."),
    )
    watch_folder = models.CharField(
        _("Watch folder"),
        max_length=500,
        blank=True,
        default="",
        help_text=_("Relative or absolute folder path monitored for new result files."),
    )
    sync_interval_seconds = models.PositiveIntegerField(
        _("Synchronization interval (seconds)"),
        default=300,
        validators=[MinValueValidator(30)],
    )
    sync_enabled = models.BooleanField(_("Sync enabled"), default=True)
    # Temporary compatibility flags — prefer enabled_features for new capabilities.
    watch_enabled = models.BooleanField(
        _("Watch folder enabled"),
        default=True,
        help_text=_("Legacy flag. Prefer enabled_features['watcher']."),
    )
    upload_enabled = models.BooleanField(
        _("Upload enabled"),
        default=True,
        help_text=_("Legacy flag. Prefer enabled_features['upload']."),
    )
    enabled_features = models.JSONField(
        _("Enabled features"),
        default=default_enabled_features,
        blank=True,
        help_text=_(
            "Flexible capability map (watcher, upload, analysis, diagnostics, "
            "remote_execution, …). Prefer this over new Boolean columns."
        ),
    )
    configuration_version = models.PositiveIntegerField(
        _("Configuration version"),
        default=1,
        validators=[MinValueValidator(1)],
        help_text=_(
            "Increment whenever sync configuration changes. Heartbeat mismatch "
            "triggers BootstrapRequired."
        ),
    )
    schema_version = models.PositiveIntegerField(
        _("Bootstrap schema version"),
        default=1,
        validators=[MinValueValidator(1)],
        help_text=_(
            "Version of the bootstrap document structure this profile conforms to. "
            "Heartbeat mismatch may require agent upgrade or bootstrap refresh."
        ),
    )
    smb_credential_reference = models.CharField(
        _("SMB credential reference"),
        max_length=255,
        blank=True,
        default="",
        help_text=_(
            "Preferred secret handle (vault key, Windows credential name, etc.). "
            "Do not store raw SMB passwords in PostgreSQL."
        ),
    )
    smb_username = models.CharField(
        _("SMB username"),
        max_length=200,
        blank=True,
        default="",
        help_text=_("Optional username hint. Pair with credential reference, not a DB password."),
    )
    notes = models.TextField(_("Notes"), blank=True, default="")
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("Equipment Sync Profile")
        verbose_name_plural = _("Equipment Sync Profiles")
        ordering = ["equipment__code"]
        indexes = [
            models.Index(fields=["configuration_version"]),
            models.Index(fields=["schema_version"]),
            models.Index(fields=["sync_enabled", "watch_enabled"]),
        ]

    def __str__(self) -> str:
        return f"Sync profile for {self.equipment}"

    def bump_configuration_version(self, *, save: bool = True) -> int:
        """Increment configuration_version after an admin config change."""
        self.configuration_version = (self.configuration_version or 0) + 1
        if save:
            self.save(update_fields=["configuration_version", "updated_at"])
        return self.configuration_version


class AgentAssignment(models.Model):
    """
    Binding between a replaceable sync agent and an equipment sync profile.

    One agent may monitor many instruments. A profile should have at most one
    active assignment at a time to keep bootstrap unambiguous.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sync_agent = models.ForeignKey(
        DepartmentSyncAgent,
        on_delete=models.CASCADE,
        related_name="assignments",
        verbose_name=_("Sync agent"),
    )
    sync_profile = models.ForeignKey(
        EquipmentSyncProfile,
        on_delete=models.CASCADE,
        related_name="assignments",
        verbose_name=_("Equipment sync profile"),
    )
    is_active = models.BooleanField(_("Active"), default=True, db_index=True)
    assigned_at = models.DateTimeField(_("Assigned at"), auto_now_add=True)
    unassigned_at = models.DateTimeField(_("Unassigned at"), null=True, blank=True)
    notes = models.TextField(_("Notes"), blank=True, default="")
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("Agent Assignment")
        verbose_name_plural = _("Agent Assignments")
        ordering = ["-assigned_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["sync_profile"],
                condition=models.Q(is_active=True),
                name="sync_agentassignment_one_active_per_profile",
            ),
            models.UniqueConstraint(
                fields=["sync_agent", "sync_profile"],
                condition=models.Q(is_active=True),
                name="sync_agentassignment_unique_active_pair",
            ),
        ]
        indexes = [
            models.Index(fields=["sync_agent", "is_active"]),
            models.Index(fields=["sync_profile", "is_active"]),
        ]

    def __str__(self) -> str:
        state = "active" if self.is_active else "inactive"
        return f"{self.sync_agent} → {self.sync_profile} ({state})"


class AgentHeartbeat(models.Model):
    """
    Runtime telemetry and deployment diagnostics for a Department Sync Agent.

    Reports configuration_version and schema_version currently loaded by the
    agent so the portal can return BootstrapRequired / upgrade commands without
    shipping full configuration on the heartbeat path.
    """

    id = models.BigAutoField(primary_key=True)
    sync_agent = models.ForeignKey(
        DepartmentSyncAgent,
        on_delete=models.CASCADE,
        related_name="heartbeats",
        verbose_name=_("Sync agent"),
    )
    reported_at = models.DateTimeField(_("Reported at"), db_index=True)
    cpu_percent = models.FloatField(_("CPU percent"), null=True, blank=True)
    memory_percent = models.FloatField(_("Memory percent"), null=True, blank=True)
    disk_percent = models.FloatField(_("Disk percent"), null=True, blank=True)
    queue_size = models.PositiveIntegerField(_("Queue size"), null=True, blank=True)
    active_workers = models.PositiveIntegerField(_("Active workers"), null=True, blank=True)
    last_upload_at = models.DateTimeField(_("Last upload at"), null=True, blank=True)
    agent_uptime_seconds = models.PositiveBigIntegerField(
        _("Agent uptime (seconds)"),
        null=True,
        blank=True,
        help_text=_("Seconds since the agent process/service started."),
    )
    service_version = models.CharField(
        _("Service version"),
        max_length=50,
        blank=True,
        default="",
        help_text=_("Installed Department Sync Agent service/package version."),
    )
    sqlite_schema_version = models.CharField(
        _("SQLite schema version"),
        max_length=50,
        blank=True,
        default="",
        help_text=_("Local agent SQLite schema/migration version."),
    )
    windows_build = models.CharField(
        _("Windows build"),
        max_length=100,
        blank=True,
        default="",
        help_text=_("Host Windows build string (e.g. 10.0.26200)."),
    )
    hostname = models.CharField(
        _("Hostname"),
        max_length=200,
        blank=True,
        default="",
        help_text=_("Host machine hostname at heartbeat time."),
    )
    reported_configuration_version = models.PositiveIntegerField(
        _("Reported configuration version"),
        null=True,
        blank=True,
        help_text=_("configuration_version currently loaded by the agent."),
    )
    reported_schema_version = models.PositiveIntegerField(
        _("Reported schema version"),
        null=True,
        blank=True,
        help_text=_("Bootstrap schema_version currently understood by the agent."),
    )
    status_message = models.CharField(_("Status message"), max_length=500, blank=True, default="")
    details = models.JSONField(
        _("Details"),
        default=dict,
        blank=True,
        help_text=_("Optional structured runtime counters."),
    )
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("Agent Heartbeat")
        verbose_name_plural = _("Agent Heartbeats")
        ordering = ["-reported_at"]
        indexes = [
            models.Index(fields=["sync_agent", "-reported_at"]),
            models.Index(fields=["reported_configuration_version"]),
            models.Index(fields=["reported_schema_version"]),
            models.Index(fields=["hostname"]),
        ]

    def __str__(self) -> str:
        return f"Heartbeat {self.sync_agent_id} @ {self.reported_at}"


class SyncLog(models.Model):
    """
    Enterprise event log for Department Sync Agent operations.

    Prefer stable event_code values (e.g. SYNC-1001, UPLOAD-2001) over free-form
    categories alone so support and diagnostics can filter reliably.
    """

    id = models.BigAutoField(primary_key=True)
    sync_agent = models.ForeignKey(
        DepartmentSyncAgent,
        on_delete=models.CASCADE,
        related_name="sync_logs",
        verbose_name=_("Sync agent"),
    )
    equipment = models.ForeignKey(
        "equipment.Equipment",
        on_delete=models.SET_NULL,
        related_name="sync_logs",
        null=True,
        blank=True,
        verbose_name=_("Equipment"),
    )
    event_code = models.CharField(
        _("Event code"),
        max_length=32,
        db_index=True,
        help_text=_("Stable code such as SYNC-1001, UPLOAD-2001, BOOTSTRAP-3001."),
    )
    severity = models.CharField(
        _("Severity"),
        max_length=20,
        choices=SyncLogSeverity.choices,
        default=SyncLogSeverity.INFO,
        db_index=True,
    )
    category = models.CharField(
        _("Category"),
        max_length=32,
        choices=SyncLogCategory.choices,
        default=SyncLogCategory.OTHER,
        db_index=True,
    )
    message = models.TextField(_("Message"))
    json_payload = models.JSONField(
        _("JSON payload"),
        default=dict,
        blank=True,
        help_text=_("Structured event context for diagnostics and support."),
    )
    correlation_id = models.UUIDField(
        _("Correlation ID"),
        null=True,
        blank=True,
        db_index=True,
        help_text=_("Links related events across heartbeat, bootstrap, and upload flows."),
    )
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Sync Log")
        verbose_name_plural = _("Sync Logs")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["sync_agent", "-created_at"]),
            models.Index(fields=["equipment", "-created_at"]),
            models.Index(fields=["event_code", "severity"]),
            models.Index(fields=["category", "severity"]),
            models.Index(fields=["correlation_id", "-created_at"]),
            models.Index(fields=["severity", "-created_at"]),
            models.Index(fields=["event_code", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"[{self.severity}] {self.event_code}: {self.message[:80]}"


class SyncOperationsConsole(DepartmentSyncAgent):
    """
    Proxy model used only to surface the Operations Console in Django Admin.

    No extra table — change list redirects to the operations dashboard.
    """

    class Meta:
        proxy = True
        verbose_name = _("Department Sync Operations")
        verbose_name_plural = _("Department Sync Operations")


class AgentCommandStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    ACKNOWLEDGED = "ACKNOWLEDGED", _("Acknowledged")
    RUNNING = "RUNNING", _("Running")
    COMPLETED = "COMPLETED", _("Completed")
    FAILED = "FAILED", _("Failed")
    CANCELLED = "CANCELLED", _("Cancelled")


class AgentCommandPriority(models.TextChoices):
    LOW = "LOW", _("Low")
    NORMAL = "NORMAL", _("Normal")
    HIGH = "HIGH", _("High")
    CRITICAL = "CRITICAL", _("Critical")


class AgentCommandType:
    """
    Extensible command-type vocabulary.

    Stored as free-form CharField so new types require no migration.
    """

    BOOTSTRAP_REQUIRED = "BOOTSTRAP_REQUIRED"
    RESTART_AGENT = "RESTART_AGENT"
    RESTART_PLUGIN = "RESTART_PLUGIN"
    REFRESH_CONFIGURATION = "REFRESH_CONFIGURATION"
    CREATE_WORKSPACE = "CREATE_WORKSPACE"
    REBUILD_WORKSPACE = "REBUILD_WORKSPACE"
    RETRY_UPLOAD = "RETRY_UPLOAD"
    UPLOAD_RESULTS = "UPLOAD_RESULTS"
    RESCAN_FOLDER = "RESCAN_FOLDER"
    RUN_DIAGNOSTICS = "RUN_DIAGNOSTICS"
    COLLECT_LOGS = "COLLECT_LOGS"
    EXECUTE_LICENSED_SOFTWARE = "EXECUTE_LICENSED_SOFTWARE"
    CLEANUP_WORKSPACE = "CLEANUP_WORKSPACE"
    DELETE_TEMPORARY_FILES = "DELETE_TEMPORARY_FILES"
    SYNCHRONIZE_BOOKINGS = "SYNCHRONIZE_BOOKINGS"


class AgentCommand(models.Model):
    """
    Generic portal → agent command queue.

    Payload / result_payload are JSON so future capabilities need only new
    command_type values and service logic — not schema redesign.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sync_agent = models.ForeignKey(
        DepartmentSyncAgent,
        on_delete=models.CASCADE,
        related_name="commands",
        verbose_name=_("Sync agent"),
    )
    equipment = models.ForeignKey(
        "equipment.Equipment",
        on_delete=models.SET_NULL,
        related_name="sync_commands",
        null=True,
        blank=True,
        verbose_name=_("Equipment"),
    )
    booking = models.ForeignKey(
        "equipment.Booking",
        on_delete=models.SET_NULL,
        related_name="sync_commands",
        null=True,
        blank=True,
        verbose_name=_("Booking"),
    )
    command_type = models.CharField(
        _("Command type"),
        max_length=64,
        db_index=True,
        help_text=_("Extensible type key, e.g. CREATE_WORKSPACE, RETRY_UPLOAD."),
    )
    priority = models.CharField(
        _("Priority"),
        max_length=16,
        choices=AgentCommandPriority.choices,
        default=AgentCommandPriority.NORMAL,
        db_index=True,
    )
    status = models.CharField(
        _("Status"),
        max_length=20,
        choices=AgentCommandStatus.choices,
        default=AgentCommandStatus.PENDING,
        db_index=True,
    )
    payload = models.JSONField(_("Payload"), default=dict, blank=True)
    result_payload = models.JSONField(_("Result payload"), default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="created_sync_commands",
        null=True,
        blank=True,
        verbose_name=_("Created by"),
    )
    scheduled_at = models.DateTimeField(_("Scheduled at"), null=True, blank=True, db_index=True)
    started_at = models.DateTimeField(_("Started at"), null=True, blank=True)
    completed_at = models.DateTimeField(_("Completed at"), null=True, blank=True)
    retry_count = models.PositiveIntegerField(_("Retry count"), default=0)
    last_error = models.TextField(_("Last error"), blank=True, default="")
    correlation_id = models.UUIDField(_("Correlation ID"), null=True, blank=True, db_index=True)
    version = models.PositiveIntegerField(_("Row version"), default=1)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(_("Updated at"), auto_now=True, db_index=True)

    class Meta:
        verbose_name = _("Agent Command")
        verbose_name_plural = _("Agent Commands")
        ordering = ["-priority", "created_at"]
        indexes = [
            models.Index(fields=["sync_agent", "status", "-created_at"]),
            models.Index(fields=["sync_agent", "command_type", "status"]),
            models.Index(fields=["sync_agent", "updated_at"]),
            models.Index(fields=["status", "priority", "scheduled_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.command_type} ({self.status}) → {self.sync_agent_id}"

    def bump_version(self, *, save: bool = False) -> int:
        self.version = (self.version or 0) + 1
        if save:
            self.save(update_fields=["version", "updated_at"])
        return self.version


class BookingWorkspaceStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    READY = "READY", _("Ready")
    ACTIVE = "ACTIVE", _("Active")
    ARCHIVED = "ARCHIVED", _("Archived")
    FAILED = "FAILED", _("Failed")


class BookingWorkspace(models.Model):
    """
    Portal-side workspace descriptor for a booking under a sync agent.

    Idempotent: one workspace per (booking, sync_agent). Does not alter Booking.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sync_agent = models.ForeignKey(
        DepartmentSyncAgent,
        on_delete=models.CASCADE,
        related_name="workspaces",
        verbose_name=_("Sync agent"),
    )
    booking = models.ForeignKey(
        "equipment.Booking",
        on_delete=models.CASCADE,
        related_name="sync_workspaces",
        verbose_name=_("Booking"),
    )
    equipment = models.ForeignKey(
        "equipment.Equipment",
        on_delete=models.PROTECT,
        related_name="sync_workspaces",
        verbose_name=_("Equipment"),
    )
    workspace_name = models.CharField(_("Workspace name"), max_length=255)
    relative_folder = models.CharField(_("Relative folder"), max_length=500)
    expected_result_folder = models.CharField(
        _("Expected result folder"),
        max_length=500,
        blank=True,
        default="",
    )
    sample_folder = models.CharField(_("Sample folder"), max_length=500, blank=True, default="")
    status = models.CharField(
        _("Status"),
        max_length=20,
        choices=BookingWorkspaceStatus.choices,
        default=BookingWorkspaceStatus.READY,
        db_index=True,
    )
    configuration_version = models.PositiveIntegerField(_("Configuration version"), default=1)
    version = models.PositiveIntegerField(_("Row version"), default=1)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated at"), auto_now=True, db_index=True)

    class Meta:
        verbose_name = _("Booking Workspace")
        verbose_name_plural = _("Booking Workspaces")
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["booking", "sync_agent"],
                name="sync_bookingworkspace_unique_booking_agent",
            ),
        ]
        indexes = [
            models.Index(fields=["sync_agent", "updated_at"]),
            models.Index(fields=["equipment", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.workspace_name} ({self.booking_id})"


class AgentUploadSessionStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    RECEIVING = "RECEIVING", _("Receiving")
    VERIFYING = "VERIFYING", _("Verifying")
    COMPLETED = "COMPLETED", _("Completed")
    FAILED = "FAILED", _("Failed")
    EXPIRED = "EXPIRED", _("Expired")
    CANCELLED = "CANCELLED", _("Cancelled")
    REJECTED = "REJECTED", _("Rejected")


class AgentUploadSession(models.Model):
    """
    Portal-side resumable upload session for Department Sync Agent transport.

    Manages file transport only — does not mark bookings complete or process results.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sync_agent = models.ForeignKey(
        DepartmentSyncAgent,
        on_delete=models.CASCADE,
        related_name="upload_sessions",
        verbose_name=_("Sync agent"),
    )
    agent_upload_id = models.UUIDField(
        _("Agent upload ID"),
        db_index=True,
        help_text=_("UploadId from the agent's local UploadQueue."),
    )
    equipment = models.ForeignKey(
        "equipment.Equipment",
        on_delete=models.SET_NULL,
        related_name="sync_upload_sessions",
        null=True,
        blank=True,
        verbose_name=_("Equipment"),
    )
    booking = models.ForeignKey(
        "equipment.Booking",
        on_delete=models.SET_NULL,
        related_name="sync_upload_sessions",
        null=True,
        blank=True,
        verbose_name=_("Booking"),
    )
    workspace = models.ForeignKey(
        BookingWorkspace,
        on_delete=models.SET_NULL,
        related_name="upload_sessions",
        null=True,
        blank=True,
        verbose_name=_("Workspace"),
    )
    file_name = models.CharField(_("File name"), max_length=500)
    relative_path = models.CharField(_("Relative path"), max_length=1000, blank=True, default="")
    expected_size = models.BigIntegerField(_("Expected size (bytes)"), default=0)
    expected_chunk_count = models.PositiveIntegerField(_("Expected chunk count"), default=0)
    chunk_size = models.PositiveIntegerField(_("Chunk size (bytes)"))
    resume_token = models.CharField(_("Resume token"), max_length=64, db_index=True)
    server_path = models.CharField(_("Server path"), max_length=1000)
    bytes_received = models.BigIntegerField(_("Bytes received"), default=0)
    chunks_received = models.PositiveIntegerField(_("Chunks received"), default=0)
    status = models.CharField(
        _("Status"),
        max_length=20,
        choices=AgentUploadSessionStatus.choices,
        default=AgentUploadSessionStatus.PENDING,
        db_index=True,
    )
    expires_at = models.DateTimeField(_("Expires at"), db_index=True)
    correlation_id = models.UUIDField(_("Correlation ID"), null=True, blank=True, db_index=True)
    last_error = models.TextField(_("Last error"), blank=True, default="")
    completed_at = models.DateTimeField(_("Completed at"), null=True, blank=True)
    version = models.PositiveIntegerField(_("Row version"), default=1)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(_("Updated at"), auto_now=True, db_index=True)

    class Meta:
        verbose_name = _("Agent Upload Session")
        verbose_name_plural = _("Agent Upload Sessions")
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["sync_agent", "agent_upload_id"],
                name="sync_uploadsession_unique_agent_upload",
            ),
        ]
        indexes = [
            models.Index(fields=["sync_agent", "status", "-created_at"]),
            models.Index(fields=["status", "expires_at"]),
            models.Index(fields=["resume_token"]),
        ]

    def __str__(self) -> str:
        return f"{self.file_name} ({self.status}) ← {self.sync_agent_id}"


class AgentUploadChunk(models.Model):
    """Metadata for a received chunk (bytes live on disk under server_path)."""

    id = models.BigAutoField(primary_key=True)
    session = models.ForeignKey(
        AgentUploadSession,
        on_delete=models.CASCADE,
        related_name="chunks",
        verbose_name=_("Upload session"),
    )
    chunk_index = models.PositiveIntegerField(_("Chunk index"))
    size = models.PositiveIntegerField(_("Size (bytes)"))
    # Reserved for future integrity (SHA-256 / signatures).
    checksum = models.CharField(_("Checksum"), max_length=128, blank=True, default="")
    received_at = models.DateTimeField(_("Received at"), auto_now_add=True)

    class Meta:
        verbose_name = _("Agent Upload Chunk")
        verbose_name_plural = _("Agent Upload Chunks")
        ordering = ["chunk_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["session", "chunk_index"],
                name="sync_uploadchunk_unique_session_index",
            ),
        ]

    def __str__(self) -> str:
        return f"chunk {self.chunk_index} ({self.size} bytes)"


class ResultProcessingStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    VALIDATING = "VALIDATING", _("Validating")
    PARSING = "PARSING", _("Parsing")
    IMPORTING = "IMPORTING", _("Importing")
    CREATING_RESULTS = "CREATING_RESULTS", _("Creating results")
    LINKING_ATTACHMENTS = "LINKING_ATTACHMENTS", _("Linking attachments")
    FINALIZING_BOOKING = "FINALIZING_BOOKING", _("Finalizing booking")
    COMPLETED = "COMPLETED", _("Completed")
    FAILED = "FAILED", _("Failed")
    RETRYING = "RETRYING", _("Retrying")
    CANCELLED = "CANCELLED", _("Cancelled")


class ResultProcessingQueue(models.Model):
    """Portal-side durable queue for post-upload result processing (Milestone 10)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sync_agent = models.ForeignKey(
        DepartmentSyncAgent,
        on_delete=models.CASCADE,
        related_name="result_processing_jobs",
        verbose_name=_("Sync agent"),
    )
    upload_session = models.ForeignKey(
        AgentUploadSession,
        on_delete=models.SET_NULL,
        related_name="processing_jobs",
        null=True,
        blank=True,
        verbose_name=_("Upload session"),
    )
    agent_upload_id = models.UUIDField(_("Agent upload queue ID"), db_index=True)
    booking = models.ForeignKey(
        "equipment.Booking",
        on_delete=models.SET_NULL,
        related_name="sync_result_processing",
        null=True,
        blank=True,
        verbose_name=_("Booking"),
    )
    equipment = models.ForeignKey(
        "equipment.Equipment",
        on_delete=models.SET_NULL,
        related_name="sync_result_processing",
        null=True,
        blank=True,
        verbose_name=_("Equipment"),
    )
    status = models.CharField(
        _("Status"),
        max_length=32,
        choices=ResultProcessingStatus.choices,
        default=ResultProcessingStatus.PENDING,
        db_index=True,
    )
    retry_count = models.PositiveIntegerField(_("Retry count"), default=0)
    started_at = models.DateTimeField(_("Started at"), null=True, blank=True)
    completed_at = models.DateTimeField(_("Completed at"), null=True, blank=True)
    error_message = models.TextField(_("Error message"), blank=True, default="")
    parser_used = models.CharField(_("Parser used"), max_length=64, blank=True, default="")
    metadata = models.JSONField(_("Metadata"), default=dict, blank=True)
    correlation_id = models.UUIDField(_("Correlation ID"), null=True, blank=True, db_index=True)
    version = models.PositiveIntegerField(_("Row version"), default=1)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(_("Updated at"), auto_now=True, db_index=True)

    class Meta:
        verbose_name = _("Result Processing Queue")
        verbose_name_plural = _("Result Processing Queues")
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["sync_agent", "agent_upload_id"],
                name="sync_resultprocessing_unique_agent_upload",
            ),
        ]
        indexes = [
            models.Index(fields=["sync_agent", "status", "-created_at"]),
            models.Index(fields=["status", "updated_at"]),
        ]

    def __str__(self) -> str:
        return f"process {self.agent_upload_id} ({self.status})"


class EquipmentResult(models.Model):
    """Imported equipment test result originating from DSA upload processing."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sync_agent = models.ForeignKey(
        DepartmentSyncAgent,
        on_delete=models.CASCADE,
        related_name="equipment_results",
        verbose_name=_("Sync agent"),
    )
    booking = models.ForeignKey(
        "equipment.Booking",
        on_delete=models.CASCADE,
        related_name="equipment_results",
        verbose_name=_("Booking"),
    )
    equipment = models.ForeignKey(
        "equipment.Equipment",
        on_delete=models.PROTECT,
        related_name="equipment_results",
        verbose_name=_("Equipment"),
    )
    upload_session = models.ForeignKey(
        AgentUploadSession,
        on_delete=models.SET_NULL,
        related_name="equipment_results",
        null=True,
        blank=True,
        verbose_name=_("Upload session"),
    )
    processing_job = models.ForeignKey(
        ResultProcessingQueue,
        on_delete=models.SET_NULL,
        related_name="equipment_results",
        null=True,
        blank=True,
        verbose_name=_("Processing job"),
    )
    agent_upload_id = models.UUIDField(_("Agent upload ID"), db_index=True)
    parser_used = models.CharField(_("Parser used"), max_length=64, blank=True, default="")
    source_file_name = models.CharField(_("Source file name"), max_length=500, blank=True, default="")
    metadata = models.JSONField(_("Metadata"), default=dict, blank=True)
    processed_by = models.CharField(_("Processed by"), max_length=200, blank=True, default="DepartmentSyncAgent")
    processing_duration_ms = models.PositiveIntegerField(_("Processing duration (ms)"), null=True, blank=True)
    correlation_id = models.UUIDField(_("Correlation ID"), null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("Equipment Result")
        verbose_name_plural = _("Equipment Results")
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["sync_agent", "agent_upload_id"],
                name="sync_equipmentresult_unique_agent_upload",
            ),
        ]
        indexes = [
            models.Index(fields=["booking", "-created_at"]),
            models.Index(fields=["equipment", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"result {self.id} booking={self.booking_id}"


class EquipmentMeasurement(models.Model):
    """Single measurement row belonging to an EquipmentResult."""

    id = models.BigAutoField(primary_key=True)
    result = models.ForeignKey(
        EquipmentResult,
        on_delete=models.CASCADE,
        related_name="measurements",
        verbose_name=_("Equipment result"),
    )
    name = models.CharField(_("Measurement name"), max_length=255)
    value = models.CharField(_("Value"), max_length=255, blank=True, default="")
    unit = models.CharField(_("Unit"), max_length=64, blank=True, default="")
    pass_fail = models.CharField(_("Pass/Fail"), max_length=32, blank=True, default="")
    tolerance = models.CharField(_("Tolerance"), max_length=128, blank=True, default="")
    timestamp = models.DateTimeField(_("Timestamp"), null=True, blank=True)
    channel = models.CharField(_("Channel"), max_length=128, blank=True, default="")
    remarks = models.TextField(_("Remarks"), blank=True, default="")
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("Equipment Measurement")
        verbose_name_plural = _("Equipment Measurements")
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.name}={self.value}"


class ResultAttachment(models.Model):
    """File artifact linked to an imported equipment result."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    result = models.ForeignKey(
        EquipmentResult,
        on_delete=models.CASCADE,
        related_name="attachments",
        verbose_name=_("Equipment result"),
    )
    upload_session = models.ForeignKey(
        AgentUploadSession,
        on_delete=models.SET_NULL,
        related_name="result_attachments",
        null=True,
        blank=True,
        verbose_name=_("Upload session"),
    )
    file_name = models.CharField(_("File name"), max_length=500)
    relative_path = models.CharField(_("Relative path"), max_length=1000, blank=True, default="")
    content_type = models.CharField(_("Content type"), max_length=128, blank=True, default="")
    size_bytes = models.BigIntegerField(_("Size (bytes)"), default=0)
    sha256 = models.CharField(_("SHA-256"), max_length=64, blank=True, default="")
    storage_path = models.CharField(_("Storage path"), max_length=1000, blank=True, default="")
    s3_key = models.CharField(
        _("S3 object key"),
        max_length=1000,
        blank=True,
        default="",
        help_text=_("When set, file bytes live in S3 (Results/{virtual_booking_id}/...). Local sync_uploads copy may be removed."),
    )
    attachment_kind = models.CharField(
        _("Attachment kind"),
        max_length=32,
        default="primary",
        help_text=_("primary | pdf | zip | other"),
    )
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("Result Attachment")
        verbose_name_plural = _("Result Attachments")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.file_name

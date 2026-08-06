"""Device Provisioning domain models — one framework for all device types."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class DeviceType(models.TextChoices):
    DSA = "dsa", _("Department Sync Agent")
    EQUIPMENT_PC = "equipment_pc", _("Equipment PC")
    REMOTE_ANALYSIS = "remote_analysis", _("Remote Analysis Workstation")
    GPU_WORKSTATION = "gpu_workstation", _("GPU Workstation")
    AI_ASSISTANT = "ai_assistant", _("AI Assistant")
    IOT_GATEWAY = "iot_gateway", _("IoT Gateway")
    OTHER = "other", _("Other")


class DeviceLifecycle(models.TextChoices):
    """Canonical device lifecycle (portal is source of truth)."""

    UNKNOWN = "unknown", _("Unknown Device")
    PENDING_APPROVAL = "pending_approval", _("Pending Approval")
    PROVISIONING = "provisioning", _("Provisioning")
    ACTIVE = "active", _("Active")
    SUSPENDED = "suspended", _("Suspended")
    REVOKED = "revoked", _("Revoked")
    RETIRED = "retired", _("Retired")


class ProvisioningSessionStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    APPROVED = "approved", _("Approved")
    REJECTED = "rejected", _("Rejected")
    CLAIMED = "claimed", _("Claimed")
    EXPIRED = "expired", _("Expired")
    CANCELLED = "cancelled", _("Cancelled")


class AuditAction(models.TextChoices):
    CREATED = "created", _("Created")
    APPROVED = "approved", _("Approved")
    REJECTED = "rejected", _("Rejected")
    PROVISIONED = "provisioned", _("Provisioned")
    REPROVISIONED = "reprovisioned", _("Reprovisioned")
    SUSPENDED = "suspended", _("Suspended")
    REVOKED = "revoked", _("Revoked")
    RETIRED = "retired", _("Retired")
    RENAMED = "renamed", _("Renamed")
    ASSIGNED = "assigned", _("Assigned")
    HEARTBEAT = "heartbeat", _("Heartbeat")
    POLICY_UPDATED = "policy_updated", _("Policy Updated")
    # Phase R.2.3 — Trusted Department Auto-Approve
    PROVISIONING_STARTED = "provisioning_started", _("Provisioning Started")
    AUTO_APPROVED = "auto_approved", _("Auto Approved")
    AUTO_APPROVE_DENIED = "auto_approve_denied", _("Auto Approve Denied")
    POLICY_USED = "policy_used", _("Policy Used")
    # Phase R.2.4 — Equipment PC Zero-Touch
    EQUIPMENT_SELECTED = "equipment_selected", _("Equipment Selected")
    EQUIPMENT_ASSIGNED = "equipment_assigned", _("Equipment Assigned")
    PROVISION_COMPLETED = "provision_completed", _("Provision Completed")
    PROVISION_FAILED = "provision_failed", _("Provision Failed")
    DEVICE_REPLACED = "device_replaced", _("Device Replaced")


class ProvisioningMode(models.TextChoices):
    MANUAL_APPROVAL = "manual_approval", _("Manual Approval")
    TRUSTED_AUTO_APPROVE = "trusted_auto_approve", _("Trusted Auto-Approve")
    RESTRICTED_AUTO_APPROVE = "restricted_auto_approve", _("Restricted Auto-Approve")
    DEVICE_CODE = "device_code", _("Device Code Approval")


class DepartmentProvisioningPolicy(models.Model):
    """Per-department device provisioning policy (Phase R.2.3+). DSA + Equipment PC + RAA."""

    department = models.OneToOneField(
        "users.Department",
        on_delete=models.CASCADE,
        related_name="provisioning_policy",
    )
    provisioning_mode = models.CharField(
        max_length=32,
        choices=ProvisioningMode.choices,
        default=ProvisioningMode.TRUSTED_AUTO_APPROVE,
        db_index=True,
    )
    # List of CIDR strings, e.g. ["10.1.0.0/16", "172.16.5.0/24"]
    allowed_networks = models.JSONField(default=list, blank=True)
    require_mfa = models.BooleanField(default=False)
    require_device_fingerprint = models.BooleanField(default=True)
    maximum_pending_lifetime_hours = models.PositiveIntegerField(default=24)
    auto_approve_existing_reinstalls = models.BooleanField(default=True)
    audit_enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Department provisioning policies"

    def __str__(self) -> str:
        return f"{self.department_id}:{self.provisioning_mode}"


class ProvisionedDevice(models.Model):
    """Authoritative device record shared by DSA, Equipment PC, RAA, and future types."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    device_type = models.CharField(max_length=32, choices=DeviceType.choices, db_index=True)
    lifecycle = models.CharField(
        max_length=32,
        choices=DeviceLifecycle.choices,
        default=DeviceLifecycle.PENDING_APPROVAL,
        db_index=True,
    )
    display_name = models.CharField(max_length=255, blank=True, default="")
    hostname = models.CharField(max_length=255, blank=True, default="", db_index=True)
    machine_guid = models.CharField(max_length=128, blank=True, default="", db_index=True)
    fingerprint = models.CharField(max_length=128, blank=True, default="", db_index=True)
    application_version = models.CharField(max_length=64, blank=True, default="")
    windows_version = models.CharField(max_length=128, blank=True, default="")
    inventory_snapshot = models.JSONField(default=dict, blank=True)
    department = models.ForeignKey(
        "users.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="provisioned_devices",
    )
    # Hashed long-lived bearer; plaintext never stored.
    access_token_hash = models.CharField(max_length=128, blank=True, default="")
    access_token_prefix = models.CharField(max_length=16, blank=True, default="")
    access_token_issued_at = models.DateTimeField(null=True, blank=True)
    access_token_expires_at = models.DateTimeField(null=True, blank=True)
    last_heartbeat_at = models.DateTimeField(null=True, blank=True)
    last_seen_ip = models.GenericIPAddressField(null=True, blank=True)
    bootstrap_public_key = models.TextField(blank=True, default="")
    # Optional bridges to legacy product tables (populated by later installer work).
    legacy_dsa_id = models.UUIDField(null=True, blank=True, db_index=True)
    legacy_workstation_id = models.UUIDField(null=True, blank=True, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    provisioned_at = models.DateTimeField(null=True, blank=True)
    retired_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["device_type", "lifecycle"]),
            models.Index(fields=["hostname", "device_type"]),
        ]

    def __str__(self) -> str:
        return f"{self.device_type}:{self.display_name or self.hostname or self.id}"


class ProvisioningSession(models.Model):
    """Installer-initiated registration waiting for admin approval and claim."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    device_type = models.CharField(max_length=32, choices=DeviceType.choices, db_index=True)
    status = models.CharField(
        max_length=24,
        choices=ProvisioningSessionStatus.choices,
        default=ProvisioningSessionStatus.PENDING,
        db_index=True,
    )
    # Proof returned once to installer for poll/claim (hashed at rest).
    session_proof_hash = models.CharField(max_length=128, blank=True, default="")
    session_proof_prefix = models.CharField(max_length=16, blank=True, default="")
    hostname = models.CharField(max_length=255, blank=True, default="")
    machine_guid = models.CharField(max_length=128, blank=True, default="", db_index=True)
    fingerprint = models.CharField(max_length=128, blank=True, default="", db_index=True)
    windows_version = models.CharField(max_length=128, blank=True, default="")
    application_version = models.CharField(max_length=64, blank=True, default="")
    cpu = models.CharField(max_length=255, blank=True, default="")
    ram_gb = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    mac_addresses = models.JSONField(default=list, blank=True)
    local_ips = models.JSONField(default=list, blank=True)
    bootstrap_public_key = models.TextField(blank=True, default="")
    inventory = models.JSONField(default=dict, blank=True)
    display_name = models.CharField(max_length=255, blank=True, default="")
    department = models.ForeignKey(
        "users.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="provisioning_sessions",
    )
    # Product-specific intent captured at approve time (before claim).
    requested_equipment_id = models.PositiveIntegerField(null=True, blank=True)
    requested_workstation_role = models.CharField(max_length=64, blank=True, default="")
    device = models.ForeignKey(
        ProvisionedDevice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sessions",
    )
    client_ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True, default="")
    expires_at = models.DateTimeField(db_index=True)
    # Phase R.2.3 — authenticated installer / device-code flow
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requested_provisioning_sessions",
    )
    auto_approved = models.BooleanField(default=False)
    auto_approve_reason = models.CharField(max_length=255, blank=True, default="")
    device_code = models.CharField(max_length=16, blank=True, default="", db_index=True)
    device_code_expires_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_provisioning_sessions",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rejected_provisioning_sessions",
    )
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.CharField(max_length=500, blank=True, default="")
    claimed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "device_type"]),
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"session:{self.id} ({self.device_type}/{self.status})"


class DeviceBootstrapToken(models.Model):
    """Single-use, short-lived bootstrap token bound to a provisioning session."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        ProvisioningSession,
        on_delete=models.CASCADE,
        related_name="bootstrap_tokens",
    )
    device = models.ForeignKey(
        ProvisionedDevice,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="bootstrap_tokens",
    )
    token_hash = models.CharField(max_length=128)
    token_prefix = models.CharField(max_length=16, blank=True, default="")
    expires_at = models.DateTimeField(db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def is_used(self) -> bool:
        return self.used_at is not None


class DeviceCertificate(models.Model):
    """Future-ready device certificate record (issuance in a later phase)."""

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        ISSUED = "issued", _("Issued")
        REVOKED = "revoked", _("Revoked")
        EXPIRED = "expired", _("Expired")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    device = models.ForeignKey(
        ProvisionedDevice,
        on_delete=models.CASCADE,
        related_name="certificates",
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    fingerprint_sha256 = models.CharField(max_length=64, blank=True, default="")
    subject = models.CharField(max_length=255, blank=True, default="")
    not_before = models.DateTimeField(null=True, blank=True)
    not_after = models.DateTimeField(null=True, blank=True)
    pem_public = models.TextField(blank=True, default="")
    # Private key material must never be stored on the portal.
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class DevicePolicy(models.Model):
    """Policy document attached to a device type and/or specific device."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    device_type = models.CharField(max_length=32, choices=DeviceType.choices, blank=True, default="")
    device = models.ForeignKey(
        ProvisionedDevice,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="policies",
    )
    name = models.CharField(max_length=128)
    version = models.PositiveIntegerField(default=1)
    document = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [models.Index(fields=["device_type", "is_active"])]


class DeviceInventory(models.Model):
    """Point-in-time inventory snapshot for a provisioned device."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    device = models.ForeignKey(
        ProvisionedDevice,
        on_delete=models.CASCADE,
        related_name="inventory_records",
    )
    payload = models.JSONField(default=dict, blank=True)
    reported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-reported_at"]


class DeviceHeartbeat(models.Model):
    """Heartbeat history for provisioned devices."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    device = models.ForeignKey(
        ProvisionedDevice,
        on_delete=models.CASCADE,
        related_name="heartbeats",
    )
    status = models.CharField(max_length=32, blank=True, default="ok")
    payload = models.JSONField(default=dict, blank=True)
    client_ip = models.GenericIPAddressField(null=True, blank=True)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-received_at"]


class DeviceAssignment(models.Model):
    """Department / equipment / role binding for a provisioned device."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    device = models.OneToOneField(
        ProvisionedDevice,
        on_delete=models.CASCADE,
        related_name="assignment",
    )
    department = models.ForeignKey(
        "users.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="device_assignments",
    )
    equipment = models.ForeignKey(
        "equipment.Equipment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="device_assignments",
    )
    workstation_role = models.CharField(max_length=64, blank=True, default="")
    notes = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["equipment"],
                condition=models.Q(equipment__isnull=False),
                name="uniq_device_assignment_equipment",
            )
        ]


class DeviceAuditLog(models.Model):
    """Immutable audit trail for provisioning actions (no secrets)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    action = models.CharField(max_length=32, choices=AuditAction.choices, db_index=True)
    device = models.ForeignKey(
        ProvisionedDevice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
    )
    session = models.ForeignKey(
        ProvisioningSession,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="device_provisioning_audit_logs",
    )
    message = models.CharField(max_length=500, blank=True, default="")
    detail = models.JSONField(default=dict, blank=True)
    client_ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["action", "created_at"])]


# Alias for docs / API naming clarity (Pending Installations = open sessions).
PendingDevice = ProvisioningSession

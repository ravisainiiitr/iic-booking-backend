"""Provisioning services — shared lifecycle for all device types."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Any

from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.utils import timezone

from iic_booking.device_provisioning.models import (
    AuditAction,
    DeviceAssignment,
    DeviceBootstrapToken,
    DeviceHeartbeat,
    DeviceInventory,
    DeviceLifecycle,
    DevicePolicy,
    DeviceAuditLog,
    DeviceType,
    ProvisionedDevice,
    ProvisioningSession,
    ProvisioningSessionStatus,
)

SESSION_TTL = timedelta(hours=24)
BOOTSTRAP_TTL = timedelta(minutes=10)
ACCESS_TOKEN_TTL = timedelta(days=365)


def _hash(value: str) -> str:
    return make_password(value)


def _verify(plaintext: str, hashed: str) -> bool:
    if not plaintext or not hashed:
        return False
    return check_password(plaintext, hashed)


def _prefix(value: str, n: int = 8) -> str:
    return (value or "")[:n]


def _client_ip(request) -> str | None:
    if request is None:
        return None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def compute_fingerprint(
    *,
    machine_guid: str,
    hostname: str,
    mac_addresses: list | None,
    device_type: str,
) -> str:
    macs = ",".join(sorted(str(m).lower() for m in (mac_addresses or []) if m))
    raw = f"{device_type}|{machine_guid}|{hostname}|{macs}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def write_audit(
    *,
    action: str,
    message: str = "",
    device: ProvisionedDevice | None = None,
    session: ProvisioningSession | None = None,
    actor=None,
    detail: dict | None = None,
    client_ip: str | None = None,
) -> DeviceAuditLog:
    # Never accept secret-bearing keys into audit detail.
    safe = {
        k: v
        for k, v in (detail or {}).items()
        if k
        not in {
            "token",
            "access_token",
            "bootstrap_token",
            "session_proof",
            "enrollment_secret",
            "enrollment_key",
            "password",
            "certificate_private_key",
        }
    }
    return DeviceAuditLog.objects.create(
        action=action,
        message=message[:500],
        device=device,
        session=session,
        actor=actor,
        detail=safe,
        client_ip=client_ip,
    )


def default_policies_for(device_type: str) -> list[dict[str, Any]]:
    """Return active type-level policies merged into the bootstrap pack."""
    qs = DevicePolicy.objects.filter(is_active=True, device__isnull=True).filter(
        models_q_device_type(device_type)
    )
    return [
        {
            "id": str(p.id),
            "name": p.name,
            "version": p.version,
            "document": p.document,
        }
        for p in qs.order_by("name")
    ]


def models_q_device_type(device_type: str):
    from django.db.models import Q

    return Q(device_type="") | Q(device_type=device_type)


@transaction.atomic
def create_session(*, payload: dict, request=None, actor=None) -> tuple[ProvisioningSession, str]:
    """
    Installer registration. Returns (session, session_proof_plaintext).
    Proof is returned once; only the hash is stored.

    Phase R.2.3: when an authenticated administrator creates a DSA session and the
    department policy permits, the session is auto-approved in the same request.
    """
    from iic_booking.device_provisioning import policy as policy_mod
    from iic_booking.users.models import Department

    device_type = str(payload.get("device_type") or payload.get("product_type") or "").strip().lower()
    if device_type not in {c.value for c in DeviceType}:
        raise ValueError("unsupported_device_type")

    machine_guid = str(payload.get("machine_guid") or "").strip()
    hostname = str(payload.get("hostname") or "").strip()
    macs = payload.get("mac_addresses") or []
    if not isinstance(macs, list):
        macs = []
    local_ips = payload.get("local_ips") or payload.get("local_ip") or []
    if isinstance(local_ips, str):
        local_ips = [local_ips]
    if not isinstance(local_ips, list):
        local_ips = []

    fingerprint = str(payload.get("fingerprint") or "").strip() or compute_fingerprint(
        machine_guid=machine_guid,
        hostname=hostname,
        mac_addresses=macs,
        device_type=device_type,
    )

    # Resolve department: explicit payload, else authenticated admin's department.
    department = None
    raw_dept = payload.get("department_id")
    if raw_dept is not None and str(raw_dept).strip() != "":
        try:
            department = Department.objects.filter(pk=int(raw_dept)).first()
        except (TypeError, ValueError):
            raise ValueError("department_not_found") from None
        if department is None:
            raise ValueError("department_not_found")
    elif actor is not None and getattr(actor, "department_id", None):
        department = getattr(actor, "department", None)

    dept_policy = policy_mod.get_policy(department) if department else None
    client_ip = _client_ip(request)

    # Hard reject duplicate active identity (before creating a pending row).
    blocking = policy_mod.find_blocking_device(fingerprint=fingerprint, device_type=device_type)
    if blocking is not None:
        write_audit(
            action=AuditAction.REJECTED,
            message="Duplicate active device identity rejected",
            actor=actor if getattr(actor, "is_authenticated", False) else None,
            detail={
                "reason": "duplicate_active_device",
                "fingerprint": fingerprint,
                "existing_device_id": str(blocking.id),
                "lifecycle": blocking.lifecycle,
                "device_type": device_type,
            },
            client_ip=client_ip,
        )
        raise ValueError("duplicate_active_device")

    # Normalize equipment intent for Equipment PC (R.2.4).
    requested_equipment_id = None
    raw_eq = payload.get("equipment_id") or payload.get("requested_equipment_id")
    if raw_eq is not None and str(raw_eq).strip() != "":
        try:
            requested_equipment_id = int(raw_eq)
        except (TypeError, ValueError):
            raise ValueError("equipment_not_found") from None

    proof = secrets.token_urlsafe(32)
    now = timezone.now()
    ttl = policy_mod.pending_ttl_from_policy(dept_policy) if dept_policy else SESSION_TTL

    session = ProvisioningSession.objects.create(
        device_type=device_type,
        status=ProvisioningSessionStatus.PENDING,
        session_proof_hash=_hash(proof),
        session_proof_prefix=_prefix(proof),
        hostname=hostname,
        machine_guid=machine_guid,
        fingerprint=fingerprint,
        windows_version=str(payload.get("windows_version") or "")[:128],
        application_version=str(payload.get("application_version") or "")[:64],
        cpu=str(payload.get("cpu") or "")[:255],
        ram_gb=payload.get("ram_gb"),
        mac_addresses=macs,
        local_ips=local_ips,
        bootstrap_public_key=str(payload.get("bootstrap_public_key") or ""),
        inventory=payload.get("inventory") if isinstance(payload.get("inventory"), dict) else {},
        display_name=str(payload.get("display_name") or hostname or machine_guid)[:255],
        department=department,
        requested_equipment_id=requested_equipment_id,
        client_ip=client_ip,
        user_agent=str((request.META.get("HTTP_USER_AGENT") if request else "") or "")[:512],
        expires_at=now + ttl,
        requested_by=actor if getattr(actor, "is_authenticated", False) else None,
    )

    write_audit(
        action=AuditAction.PROVISIONING_STARTED,
        message="Provisioning started",
        session=session,
        actor=session.requested_by,
        detail={
            "device_type": device_type,
            "hostname": hostname,
            "fingerprint": fingerprint,
            "department_id": department.id if department else None,
            "equipment_id": requested_equipment_id,
            "policy_mode": dept_policy.provisioning_mode if dept_policy else "manual_approval",
        },
        client_ip=session.client_ip,
    )
    write_audit(
        action=AuditAction.CREATED,
        message="Provisioning session created",
        session=session,
        actor=session.requested_by,
        detail={"device_type": device_type, "hostname": hostname, "fingerprint": fingerprint},
        client_ip=session.client_ip,
    )

    if requested_equipment_id and device_type == DeviceType.EQUIPMENT_PC:
        write_audit(
            action=AuditAction.EQUIPMENT_SELECTED,
            message="Equipment selected for provisioning",
            session=session,
            actor=session.requested_by,
            detail={"equipment_id": requested_equipment_id, "department_id": department.id if department else None},
            client_ip=client_ip,
        )

    # Auto-approve / device-code for DSA (R.2.3), Equipment PC (R.2.4), RAA (R.2.5).
    if device_type not in {DeviceType.DSA, DeviceType.EQUIPMENT_PC, DeviceType.REMOTE_ANALYSIS}:
        return session, proof

    # MFA is a server-side gate only. Client-supplied flags are ignored.
    # Until portal MFA is wired, require_mfa=True always falls back to Pending Approval.
    mfa_satisfied = bool(getattr(actor, "provisioning_mfa_satisfied", False)) if actor else False

    decision = policy_mod.evaluate_auto_approve(
        user=actor if getattr(actor, "is_authenticated", False) else None,
        department=department,
        policy=dept_policy,
        fingerprint=fingerprint,
        client_ip=client_ip,
        local_ips=local_ips,
        device_type=device_type,
        mfa_satisfied=mfa_satisfied,
    )

    if dept_policy is not None and dept_policy.audit_enabled:
        write_audit(
            action=AuditAction.POLICY_USED,
            message="Department provisioning policy evaluated",
            session=session,
            actor=session.requested_by,
            detail={
                "policy_id": decision.policy_id,
                "mode": decision.mode,
                "allow": decision.allow,
                "reason": decision.reason,
                "department_id": decision.department_id,
                "fingerprint": fingerprint,
                "administrator": getattr(actor, "email", None) if actor else None,
            },
            client_ip=client_ip,
        )

    if decision.mode == ProvisioningMode.DEVICE_CODE:
        code = policy_mod.generate_device_code()
        session.device_code = code
        session.device_code_expires_at = now + policy_mod.device_code_ttl()
        session.auto_approve_reason = decision.reason
        session.save(update_fields=["device_code", "device_code_expires_at", "auto_approve_reason", "updated_at"])
        return session, proof

    if decision.allow:
        # Equipment PC auto-approve requires equipment selection.
        if device_type == DeviceType.EQUIPMENT_PC and not requested_equipment_id:
            session.auto_approve_reason = "equipment_required"
            session.save(update_fields=["auto_approve_reason", "updated_at"])
            write_audit(
                action=AuditAction.AUTO_APPROVE_DENIED,
                message="Auto-approve deferred — select equipment first",
                session=session,
                actor=session.requested_by,
                detail={"reason": "equipment_required"},
                client_ip=client_ip,
            )
            return session, proof

        session, _device, _bootstrap = approve_session(
            session=session,
            actor=actor,
            department_id=department.id if department else None,
            display_name=session.display_name,
            equipment_id=requested_equipment_id if device_type == DeviceType.EQUIPMENT_PC else None,
            workstation_role=str(payload.get("workstation_role") or "")[:64] or None,
        )
        session.auto_approved = True
        session.auto_approve_reason = decision.reason
        session.save(update_fields=["auto_approved", "auto_approve_reason", "updated_at"])
        if requested_equipment_id:
            write_audit(
                action=AuditAction.EQUIPMENT_ASSIGNED,
                message="Equipment assigned during auto-approve",
                session=session,
                device=session.device,
                actor=actor,
                detail={"equipment_id": requested_equipment_id},
                client_ip=client_ip,
            )
        write_audit(
            action=AuditAction.AUTO_APPROVED,
            message="Provisioning auto-approved by department policy",
            session=session,
            device=session.device,
            actor=actor,
            detail={
                "reason": decision.reason,
                "mode": decision.mode,
                "policy_id": decision.policy_id,
                "department_id": decision.department_id,
                "fingerprint": fingerprint,
                "administrator": getattr(actor, "email", None) or getattr(actor, "username", None),
                **(decision.detail or {}),
            },
            client_ip=client_ip,
        )
    elif decision.reason not in {"manual_approval_required", "administrator_not_authenticated"}:
        session.auto_approve_reason = decision.reason
        session.save(update_fields=["auto_approve_reason", "updated_at"])
        write_audit(
            action=AuditAction.AUTO_APPROVE_DENIED,
            message="Auto-approve denied — pending approval",
            session=session,
            actor=session.requested_by,
            detail={
                "reason": decision.reason,
                "mode": decision.mode,
                "policy_id": decision.policy_id,
                "department_id": decision.department_id,
                "fingerprint": fingerprint,
                **(decision.detail or {}),
            },
            client_ip=client_ip,
        )

    return session, proof


def get_session_for_proof(session_id, proof: str) -> ProvisioningSession | None:
    try:
        session = ProvisioningSession.objects.select_related("device", "department").get(id=session_id)
    except ProvisioningSession.DoesNotExist:
        return None
    if not _verify(proof, session.session_proof_hash):
        return None
    return session


def expire_if_needed(session: ProvisioningSession) -> ProvisioningSession:
    if session.status in {
        ProvisioningSessionStatus.PENDING,
        ProvisioningSessionStatus.APPROVED,
    } and session.expires_at <= timezone.now():
        session.status = ProvisioningSessionStatus.EXPIRED
        session.save(update_fields=["status", "updated_at"])
        write_audit(
            action=AuditAction.REJECTED,
            message="Provisioning session expired",
            session=session,
            device=session.device,
            detail={"reason": "expired"},
        )
    return session


@transaction.atomic
def approve_session(
    *,
    session: ProvisioningSession,
    actor,
    display_name: str | None = None,
    department_id: int | None = None,
    equipment_id: int | None = None,
    workstation_role: str | None = None,
) -> tuple[ProvisioningSession, ProvisionedDevice, str]:
    """
    Admin approval. Creates ProvisionedDevice in PROVISIONING and a one-shot bootstrap token.
    Returns (session, device, bootstrap_token_plaintext) — plaintext for claim path only;
    admin UI must never display it (installer obtains via claim after approval using session proof).
    """
    session = expire_if_needed(session)
    if session.status != ProvisioningSessionStatus.PENDING:
        raise ValueError("session_not_pending")

    if session.device_type == DeviceType.DSA:
        resolved_dept = department_id if department_id is not None else session.department_id
        if not resolved_dept:
            raise ValueError("department_required_for_dsa")

    if session.device_type == DeviceType.EQUIPMENT_PC and not equipment_id and not session.requested_equipment_id:
        # Equipment may be assigned later by wizard; allow approve without equipment.
        pass

    if equipment_id or session.requested_equipment_id:
        eq_id = equipment_id or session.requested_equipment_id
        conflict = (
            DeviceAssignment.objects.select_related("device")
            .filter(equipment_id=eq_id)
            .exclude(device__lifecycle=DeviceLifecycle.RETIRED)
            .exclude(device__lifecycle=DeviceLifecycle.REVOKED)
            .first()
        )
        if conflict:
            raise ValueError("equipment_already_assigned")

    from iic_booking.users.models import Department

    department = None
    if department_id is not None:
        department = Department.objects.filter(pk=department_id).first()
        if department is None:
            raise ValueError("department_not_found")
    elif session.department_id:
        department = session.department

    name = (display_name or session.display_name or session.hostname or "Device").strip()[:255]
    device = ProvisionedDevice.objects.create(
        device_type=session.device_type,
        lifecycle=DeviceLifecycle.PROVISIONING,
        display_name=name,
        hostname=session.hostname,
        machine_guid=session.machine_guid,
        fingerprint=session.fingerprint,
        application_version=session.application_version,
        windows_version=session.windows_version,
        inventory_snapshot={
            "cpu": session.cpu,
            "ram_gb": str(session.ram_gb) if session.ram_gb is not None else None,
            "mac_addresses": session.mac_addresses,
            "local_ips": session.local_ips,
            **(session.inventory or {}),
        },
        department=department,
        bootstrap_public_key=session.bootstrap_public_key,
        last_seen_ip=session.client_ip,
    )

    assignment_kwargs: dict[str, Any] = {
        "device": device,
        "department": department,
        "workstation_role": (workstation_role or session.requested_workstation_role or "")[:64],
    }
    eq_id = equipment_id or session.requested_equipment_id
    if eq_id and session.device_type == DeviceType.EQUIPMENT_PC:
        from iic_booking.equipment.models import Equipment

        equipment = Equipment.objects.filter(pk=eq_id).first()
        if equipment is None:
            raise ValueError("equipment_not_found")
        assignment_kwargs["equipment"] = equipment
    DeviceAssignment.objects.create(**assignment_kwargs)

    bootstrap = secrets.token_urlsafe(48)
    DeviceBootstrapToken.objects.create(
        session=session,
        device=device,
        token_hash=_hash(bootstrap),
        token_prefix=_prefix(bootstrap),
        expires_at=timezone.now() + BOOTSTRAP_TTL,
    )

    session.status = ProvisioningSessionStatus.APPROVED
    session.device = device
    session.display_name = name
    session.department = department
    if equipment_id:
        session.requested_equipment_id = equipment_id
    if workstation_role:
        session.requested_workstation_role = workstation_role[:64]
    session.approved_by = actor
    session.approved_at = timezone.now()
    # Extend window slightly for claim.
    session.expires_at = timezone.now() + SESSION_TTL
    session.save()

    write_audit(
        action=AuditAction.APPROVED,
        message="Provisioning session approved",
        session=session,
        device=device,
        actor=actor,
        detail={
            "device_type": device.device_type,
            "display_name": device.display_name,
            "department_id": department.id if department else None,
            "equipment_id": eq_id,
        },
    )
    # Bootstrap plaintext is NOT returned to admin callers via API serializers.
    # It is stored hashed; installer claims using session_proof after approval.
    # We keep plaintext only in-memory for tests via return value — views discard it.
    return session, device, bootstrap


@transaction.atomic
def reject_session(*, session: ProvisioningSession, actor, reason: str = "") -> ProvisioningSession:
    session = expire_if_needed(session)
    if session.status != ProvisioningSessionStatus.PENDING:
        raise ValueError("session_not_pending")
    session.status = ProvisioningSessionStatus.REJECTED
    session.rejected_by = actor
    session.rejected_at = timezone.now()
    session.rejection_reason = (reason or "")[:500]
    session.save()
    write_audit(
        action=AuditAction.REJECTED,
        message="Provisioning session rejected",
        session=session,
        actor=actor,
        detail={"reason": session.rejection_reason},
    )
    return session


@transaction.atomic
def update_pending_session(
    *,
    session: ProvisioningSession,
    actor,
    display_name: str | None = None,
    department_id: int | None = ...,
    equipment_id: int | None = ...,
    workstation_role: str | None = None,
) -> ProvisioningSession:
    if session.status != ProvisioningSessionStatus.PENDING:
        raise ValueError("session_not_pending")
    fields = ["updated_at"]
    if display_name is not None:
        session.display_name = display_name.strip()[:255]
        fields.append("display_name")
    if department_id is not ...:
        if department_id is None:
            session.department = None
        else:
            from iic_booking.users.models import Department

            dept = Department.objects.filter(pk=department_id).first()
            if dept is None:
                raise ValueError("department_not_found")
            session.department = dept
        fields.append("department")
    if equipment_id is not ...:
        session.requested_equipment_id = equipment_id
        fields.append("requested_equipment_id")
    if workstation_role is not None:
        session.requested_workstation_role = workstation_role[:64]
        fields.append("requested_workstation_role")
    session.save(update_fields=fields)
    write_audit(
        action=AuditAction.RENAMED if display_name is not None else AuditAction.ASSIGNED,
        message="Pending installation updated",
        session=session,
        actor=actor,
        detail={
            "display_name": session.display_name,
            "department_id": session.department_id,
            "equipment_id": session.requested_equipment_id,
            "workstation_role": session.requested_workstation_role,
        },
    )
    return session


@transaction.atomic
def claim_session(
    *,
    session: ProvisioningSession,
    session_proof: str,
    bootstrap_token: str | None = None,
    request=None,
) -> dict[str, Any]:
    """
    Installer claim after approval.
    Issues bearer access token once. Bootstrap token optional if still unused.
    """
    if not _verify(session_proof, session.session_proof_hash):
        raise ValueError("invalid_session_proof")
    session = expire_if_needed(session)
    if session.status != ProvisioningSessionStatus.APPROVED:
        raise ValueError("session_not_approved")
    if session.device is None:
        raise ValueError("device_missing")

    token_row = (
        DeviceBootstrapToken.objects.filter(session=session, used_at__isnull=True)
        .order_by("-created_at")
        .first()
    )
    if token_row is None:
        raise ValueError("bootstrap_token_missing")
    if token_row.expires_at <= timezone.now():
        raise ValueError("bootstrap_token_expired")
    # If installer supplies bootstrap_token, verify; otherwise session_proof after
    # approval is sufficient (token remains one-shot gate via used_at).
    if bootstrap_token and not _verify(bootstrap_token, token_row.token_hash):
        raise ValueError("invalid_bootstrap_token")

    device = session.device
    access = secrets.token_urlsafe(48)
    now = timezone.now()
    device.access_token_hash = _hash(access)
    device.access_token_prefix = _prefix(access)
    device.access_token_issued_at = now
    device.access_token_expires_at = now + ACCESS_TOKEN_TTL
    device.lifecycle = DeviceLifecycle.ACTIVE
    device.provisioned_at = now
    device.last_seen_ip = _client_ip(request)
    device.save()

    token_row.used_at = now
    token_row.save(update_fields=["used_at"])

    session.status = ProvisioningSessionStatus.CLAIMED
    session.claimed_at = now
    session.save(update_fields=["status", "claimed_at", "updated_at"])

    DeviceInventory.objects.create(device=device, payload=device.inventory_snapshot or {})

    policies = default_policies_for(device.device_type)
    device_policies = [
        {
            "id": str(p.id),
            "name": p.name,
            "version": p.version,
            "document": p.document,
        }
        for p in DevicePolicy.objects.filter(device=device, is_active=True)
    ]
    assignment = getattr(device, "assignment", None)

    configuration = _configuration_for(device, assignment)

    if device.device_type == DeviceType.DSA:
        dsa_id = _bridge_dsa_agent(device=device, session=session, access_token_hash=device.access_token_hash)
        device.legacy_dsa_id = dsa_id
        device.save(update_fields=["legacy_dsa_id", "updated_at"])
        configuration["agent_uuid"] = str(device.id)
        configuration["legacy_dsa_id"] = str(dsa_id)
        # Embed equipment tree so the installer never needs legacy UUID+secret APIs.
        from iic_booking.sync.installer.services import build_equipment_tree_for_department

        tree = build_equipment_tree_for_department(device.department_id or session.department_id)
        configuration["equipment_list"] = tree.get("departments") or []
        configuration["equipment_tree"] = tree

    if device.device_type == DeviceType.REMOTE_ANALYSIS:
        # Bridge issues an AnalysisWorkstation token the agent already understands.
        ws, ra_token = _bridge_raa_workstation(device=device, session=session)
        access = ra_token
        device.access_token_hash = _hash(access)
        device.access_token_prefix = _prefix(access)
        import uuid as uuid_mod

        try:
            device.legacy_workstation_id = uuid_mod.UUID(str(ws.id))
        except Exception:
            device.legacy_workstation_id = None
        device.save(
            update_fields=[
                "access_token_hash",
                "access_token_prefix",
                "legacy_workstation_id",
                "updated_at",
            ]
        )
        configuration["agent_id"] = ws.agent_id
        configuration["workstation_id"] = str(ws.id)
        configuration["legacy_workstation_id"] = str(ws.id)

    write_audit(
        action=AuditAction.PROVISIONED,
        message="Device provisioned (claim completed)",
        session=session,
        device=device,
        detail={"device_type": device.device_type},
        client_ip=_client_ip(request),
    )

    if device.device_type == DeviceType.EQUIPMENT_PC:
        finalize_equipment_pc_claim(device=device, session=session, request=request)

    if device.device_type == DeviceType.REMOTE_ANALYSIS:
        write_audit(
            action=AuditAction.PROVISION_COMPLETED,
            message="Remote Analysis Agent provision completed",
            session=session,
            device=device,
            detail={
                "workstation_id": configuration.get("workstation_id"),
                "agent_id": configuration.get("agent_id"),
                "department_id": device.department_id,
            },
            client_ip=_client_ip(request),
        )

    return {
        "device_uuid": str(device.id),
        "device_type": device.device_type,
        "display_name": device.display_name,
        "lifecycle": device.lifecycle,
        "access_token": access,
        "access_token_expires_at": device.access_token_expires_at.isoformat() if device.access_token_expires_at else None,
        "policies": policies + device_policies,
        "configuration": configuration,
        "certificates": [],  # future
        "assignment": _serialize_assignment(assignment),
        "agent_uuid": str(device.id) if device.device_type == DeviceType.DSA else None,
        "agent_id": configuration.get("agent_id") if device.device_type == DeviceType.REMOTE_ANALYSIS else None,
        "workstation_id": configuration.get("workstation_id") if device.device_type == DeviceType.REMOTE_ANALYSIS else None,
        # Top-level convenience for DSA installer (same payload as configuration.equipment_tree).
        "equipment_tree": configuration.get("equipment_tree") if device.device_type == DeviceType.DSA else None,
    }


def _parse_machine_guid(raw: str) -> "uuid.UUID":
    import uuid as uuid_mod

    try:
        return uuid_mod.UUID(str(raw).strip())
    except Exception:
        return uuid_mod.uuid5(uuid_mod.NAMESPACE_DNS, (raw or "unknown-machine").strip() or "unknown-machine")


def _bridge_dsa_agent(*, device: ProvisionedDevice, session: ProvisioningSession, access_token_hash: str):
    """
    Create/update legacy DepartmentSyncAgent so /sync/installer/* and heartbeats
    continue to work with the zero-touch device UUID + bearer.
    """
    import uuid as uuid_mod

    from iic_booking.sync.models import AgentLifecycleStatus, DepartmentSyncAgent

    department = device.department or session.department
    if department is None:
        raise ValueError("department_required_for_dsa")

    agent_uuid = device.id if isinstance(device.id, uuid_mod.UUID) else uuid_mod.UUID(str(device.id))
    machine_guid = _parse_machine_guid(session.machine_guid or device.machine_guid or str(device.id))

    agent = DepartmentSyncAgent.objects.filter(agent_uuid=agent_uuid).first()
    if agent is None:
        # Reuse row for same machine if present (re-enrollment after revoke).
        agent = DepartmentSyncAgent.objects.filter(machine_guid=machine_guid).first()

    name = (device.display_name or session.hostname or "DSA")[:200]
    now = timezone.now()
    if agent is None:
        agent = DepartmentSyncAgent(
            agent_uuid=agent_uuid,
            agent_name=name,
            department=department,
            machine_guid=machine_guid,
            machine_name=(session.hostname or "")[:200],
            operating_system=(session.windows_version or "")[:200],
            version=(session.application_version or "")[:50],
            status=AgentLifecycleStatus.ENROLLED,
            access_token_hash=access_token_hash,
            access_token_issued_at=now,
            access_token_expires_at=device.access_token_expires_at,
            bootstrap_required=True,
            is_active=True,
        )
        agent.save()
    else:
        agent.agent_uuid = agent_uuid
        agent.agent_name = name
        agent.department = department
        agent.machine_guid = machine_guid
        agent.machine_name = (session.hostname or agent.machine_name or "")[:200]
        agent.operating_system = (session.windows_version or agent.operating_system or "")[:200]
        agent.version = (session.application_version or agent.version or "")[:50]
        agent.status = AgentLifecycleStatus.ENROLLED
        agent.access_token_hash = access_token_hash
        agent.access_token_issued_at = now
        agent.access_token_expires_at = device.access_token_expires_at
        agent.enrollment_token_hash = ""
        agent.bootstrap_required = True
        agent.is_active = True
        agent.save()

    return agent.id


def _serialize_assignment(assignment: DeviceAssignment | None) -> dict | None:
    if assignment is None:
        return None
    return {
        "department_id": assignment.department_id,
        "department_name": assignment.department.name if assignment.department_id else None,
        "equipment_id": assignment.equipment_id,
        "equipment_name": getattr(assignment.equipment, "name", None) if assignment.equipment_id else None,
        "workstation_role": assignment.workstation_role,
    }


def _configuration_for(device: ProvisionedDevice, assignment: DeviceAssignment | None) -> dict:
    """Product-shaped configuration pack without secrets."""
    base = {
        "device_uuid": str(device.id),
        "device_type": device.device_type,
        "display_name": device.display_name,
        "heartbeat_interval_seconds": 60,
    }
    if device.device_type == DeviceType.DSA:
        base.update(
            {
                "folder_policy": {},
                "department_id": assignment.department_id if assignment else device.department_id,
                "equipment_list": [],
                "version_policy": {"min_application_version": None},
            }
        )
    elif device.device_type == DeviceType.EQUIPMENT_PC:
        base.update(_equipment_pc_configuration(device, assignment))
    elif device.device_type == DeviceType.REMOTE_ANALYSIS:
        role = (assignment.workstation_role if assignment else "") or "analysis"
        base.update(
            {
                "workstation_role": role,
                "requires_windows_password": True,
                "department_id": assignment.department_id if assignment else device.department_id,
                "heartbeat_interval_seconds": 30,
                "inventory_interval_minutes": 360,
                "tunnel_policy": {"enabled": True, "mode": "reverse"},
                "health_policy": {
                    "require_portal_reachable": True,
                    "require_heartbeat": True,
                    "require_windows_service": True,
                    "require_software_scan": True,
                },
                "version_policy": {"min_agent_version": "1.0.0"},
                "remote_analysis_settings": {
                    "supports_rdp": True,
                    "supports_clipboard": True,
                    "supports_file_transfer": True,
                    "local_health_port": 5088,
                },
            }
        )
    return base


def _bridge_raa_workstation(*, device: ProvisionedDevice, session: ProvisioningSession):
    """
    Create/update AnalysisWorkstation so legacy /api/v1/analysis/* continues to work
    with zero-touch (agent_id + AgentToken).
    """
    from iic_booking.remote_analysis.constants import WorkstationStatus
    from iic_booking.remote_analysis.models import AnalysisWorkstation, WorkstationInventory
    from iic_booking.remote_analysis.services.registration import _transition, _upsert_capabilities
    from iic_booking.remote_analysis.services.tokens import issue_agent_token, revoke_all_tokens

    inv = session.inventory if isinstance(session.inventory, dict) else {}
    agent_id = str(
        inv.get("agent_id") or inv.get("agentId") or f"raa-{str(device.id).replace('-', '')[:16]}"
    )
    fingerprint = (session.fingerprint or device.fingerprint or "").strip()
    machine_guid = (session.machine_guid or device.machine_guid or "").strip()

    ws = AnalysisWorkstation.objects.filter(agent_id=agent_id).first()
    if ws is None and fingerprint:
        ws = AnalysisWorkstation.objects.filter(machine_fingerprint=fingerprint).first()
    if ws is None and machine_guid:
        ws = AnalysisWorkstation.objects.filter(machine_guid=machine_guid).first()

    created = ws is None
    if ws is None:
        ws = AnalysisWorkstation(agent_id=agent_id)
    else:
        ws.agent_id = agent_id

    department = device.department or session.department
    ws.hostname = (session.hostname or device.hostname or ws.hostname or "")[:255]
    ws.display_name = (device.display_name or session.display_name or ws.hostname or agent_id)[:255]
    ws.operating_system = (session.windows_version or device.windows_version or ws.operating_system or "")[:255]
    ws.windows_version = (session.windows_version or "")[:128]
    ws.cpu = (session.cpu or "")[:255]
    if session.ram_gb is not None:
        try:
            ws.memory_gb = float(session.ram_gb)
        except (TypeError, ValueError):
            pass
    ws.machine_guid = machine_guid[:64]
    ws.machine_fingerprint = fingerprint[:128]
    ws.agent_version = (session.application_version or device.application_version or "")[:64]
    ws.ip_address = None
    raw_ip = str(session.client_ip or device.last_seen_ip or "").strip()
    if raw_ip:
        ws.ip_address = raw_ip[:64]
    macs = session.mac_addresses if isinstance(session.mac_addresses, list) else []
    if macs:
        ws.mac_address = str(macs[0])[:64]
    ws.supports_rdp = True
    ws.supports_clipboard = True
    ws.supports_file_transfer = True
    if department is not None:
        ws.department = department
        ws.department_name = department.name

    now = timezone.now()
    if created:
        ws.registration_date = now
        _transition(ws, WorkstationStatus.REGISTERING, "Zero-touch provisioning")
        ws.save()
        WorkstationInventory.objects.get_or_create(workstation=ws)
        _upsert_capabilities(ws)
        _transition(ws, WorkstationStatus.ONLINE, "Provisioned")
        ws.save(update_fields=["status", "updated_at"])
        _transition(ws, WorkstationStatus.AVAILABLE, "Ready")
        ws.save(update_fields=["status", "updated_at"])
    else:
        if ws.status in {WorkstationStatus.OFFLINE, WorkstationStatus.ERROR, WorkstationStatus.UNKNOWN}:
            _transition(ws, WorkstationStatus.ONLINE, "Re-provision contact")
        ws.save()
        _upsert_capabilities(ws)

    revoke_all_tokens(ws)
    _token_row, plaintext = issue_agent_token(ws)
    return ws, plaintext


def _equipment_pc_configuration(device: ProvisionedDevice, assignment: DeviceAssignment | None) -> dict:
    """Full Equipment PC config pack issued at claim (no secrets)."""
    equipment = getattr(assignment, "equipment", None) if assignment else None
    code = (getattr(equipment, "code", None) or getattr(equipment, "equipment_code", None) or "EQ").strip()
    safe_code = "".join(c if c.isalnum() or c in "-_" else "_" for c in code)[:64] or "EQ"
    root = rf"C:\ProgramData\DepartmentSyncAgent\Equipment\{safe_code}"
    share = (getattr(equipment, "dsa_share_name", None) or "Results").strip() or "Results"
    folders = {
        "raw": rf"{root}\Raw",
        "results": rf"{root}\Results",
        "temp": rf"{root}\Temp",
        "logs": rf"{root}\Logs",
    }
    return {
        "equipment_id": assignment.equipment_id if assignment else None,
        "equipment_code": getattr(equipment, "code", None) if equipment else None,
        "equipment_name": getattr(equipment, "name", None) if equipment else device.display_name,
        "department_id": assignment.department_id if assignment else device.department_id,
        "network_mode": "dhcp",
        "folders": folders,
        "local_paths": folders,
        "share_name": share,
        "folder_policy": {
            "create_missing": True,
            "result_folder": folders["results"],
            "raw_folder": folders["raw"],
        },
        "share_policy": {
            "enabled": True,
            "share_name": share,
            "path": folders["results"],
            "grant": "Everyone,FULL",
        },
        "health_policy": {
            "require_dsa_reachable": True,
            "require_portal_heartbeat": True,
            "require_result_write": True,
            "require_share_access": True,
        },
        "version_policy": {
            "min_wizard_version": "1.0.0",
            "min_application_version": None,
        },
        "windows_user": {"username": "EquipmentPC"},
        # One-time passwords are never included in portal claim packs.
    }


def list_unassigned_equipment(*, department_id: int | None, actor=None) -> list[dict]:
    """
    Equipment eligible for Equipment PC zero-touch assignment.
    Excludes instruments already bound to a non-revoked/non-retired provisioned device.
    """
    from iic_booking.equipment.models import Equipment

    blocking = (
        DeviceAssignment.objects.filter(equipment_id__isnull=False)
        .exclude(device__lifecycle__in={DeviceLifecycle.REVOKED, DeviceLifecycle.RETIRED})
        .values_list("equipment_id", flat=True)
    )

    qs = Equipment.objects.filter(dsa_enabled=True).exclude(id__in=blocking)
    if department_id is not None:
        qs = qs.filter(internal_department_id=department_id)
    elif actor is not None and getattr(actor, "department_id", None):
        user_type = str(getattr(actor, "user_type", "") or "").lower()
        from iic_booking.users.models.user_type import UserType

        if user_type != UserType.ADMIN and not getattr(actor, "is_superuser", False):
            qs = qs.filter(internal_department_id=actor.department_id)

    qs = qs.select_related("internal_department").order_by("name")[:500]
    results = []
    for eq in qs:
        results.append(
            {
                "id": eq.id,
                "equipment_id": eq.id,
                "code": getattr(eq, "code", None) or "",
                "name": eq.name,
                "department_id": eq.internal_department_id,
                "department_name": eq.internal_department.name if eq.internal_department_id else None,
                "share_name": getattr(eq, "dsa_share_name", None) or "Results",
                "is_assigned": False,
            }
        )
    return results


@transaction.atomic
def replace_equipment_pc(*, device: ProvisionedDevice, actor=None) -> ProvisionedDevice:
    """
    Release equipment binding and revoke the old Equipment PC so a new PC can claim it.
    Existing software may remain; upgrade/re-provision uses a fresh session.
    """
    if device.device_type != DeviceType.EQUIPMENT_PC:
        raise ValueError("not_equipment_pc")
    old_equipment_id = None
    assignment = getattr(device, "assignment", None)
    if assignment is not None:
        old_equipment_id = assignment.equipment_id
        assignment.equipment = None
        assignment.save(update_fields=["equipment", "updated_at"])
    device = set_lifecycle(
        device=device,
        lifecycle=DeviceLifecycle.REVOKED,
        actor=actor,
        message="Equipment PC replaced",
    )
    write_audit(
        action=AuditAction.DEVICE_REPLACED,
        message="Equipment PC replaced — equipment released for re-assignment",
        device=device,
        actor=actor,
        detail={"released_equipment_id": old_equipment_id},
    )
    return device


@transaction.atomic
def replace_remote_analysis(*, device: ProvisionedDevice, actor=None) -> ProvisionedDevice:
    """
    Revoke a Remote Analysis provisioned device so a replacement PC can zero-touch enroll.
    Legacy AnalysisWorkstation tokens are revoked; the workstation row remains for history.
    """
    if device.device_type != DeviceType.REMOTE_ANALYSIS:
        raise ValueError("not_remote_analysis")

    if device.legacy_workstation_id:
        try:
            from iic_booking.remote_analysis.models import AnalysisWorkstation
            from iic_booking.remote_analysis.services.tokens import revoke_all_tokens

            ws = AnalysisWorkstation.objects.filter(id=device.legacy_workstation_id).first()
            if ws is not None:
                revoke_all_tokens(ws)
        except Exception:
            pass

    device = set_lifecycle(
        device=device,
        lifecycle=DeviceLifecycle.REVOKED,
        actor=actor,
        message="Remote Analysis workstation replaced",
    )
    write_audit(
        action=AuditAction.DEVICE_REPLACED,
        message="Remote Analysis workstation replaced — re-run installer on replacement PC",
        device=device,
        actor=actor,
        detail={"legacy_workstation_id": str(device.legacy_workstation_id or "")},
    )
    return device


def replace_device(*, device: ProvisionedDevice, actor=None) -> ProvisionedDevice:
    """Dispatch Replace Existing Device by type (R.2.4 Equipment PC, R.2.5 RAA)."""
    if device.device_type == DeviceType.EQUIPMENT_PC:
        return replace_equipment_pc(device=device, actor=actor)
    if device.device_type == DeviceType.REMOTE_ANALYSIS:
        return replace_remote_analysis(device=device, actor=actor)
    raise ValueError("replace_not_supported")


def finalize_equipment_pc_claim(*, device: ProvisionedDevice, session: ProvisioningSession, request=None) -> None:
    """Side effects after Equipment PC claim (portal equipment DSA fields, audit)."""
    assignment = getattr(device, "assignment", None)
    equipment = getattr(assignment, "equipment", None) if assignment else None
    if equipment is not None:
        # Soft-publish observed IP so DSA sync can pick it up; never write secrets.
        ip = device.last_seen_ip or session.client_ip
        fields = []
        if ip and not (equipment.dsa_ip_address or "").strip():
            equipment.dsa_ip_address = str(ip)[:64]
            fields.append("dsa_ip_address")
        share = (equipment.dsa_share_name or "").strip() or "Results"
        if not (equipment.dsa_share_name or "").strip():
            equipment.dsa_share_name = share
            fields.append("dsa_share_name")
        if fields:
            equipment.save(update_fields=fields)
    write_audit(
        action=AuditAction.PROVISION_COMPLETED,
        message="Equipment PC provision completed",
        session=session,
        device=device,
        detail={
            "equipment_id": assignment.equipment_id if assignment else None,
            "department_id": device.department_id,
        },
        client_ip=_client_ip(request),
    )


@transaction.atomic
def set_lifecycle(*, device: ProvisionedDevice, lifecycle: str, actor=None, message: str = "") -> ProvisionedDevice:
    if lifecycle not in {c.value for c in DeviceLifecycle}:
        raise ValueError("invalid_lifecycle")
    if device.lifecycle == DeviceLifecycle.RETIRED and lifecycle != DeviceLifecycle.RETIRED:
        raise ValueError("device_retired")
    device.lifecycle = lifecycle
    fields = ["lifecycle", "updated_at"]
    if lifecycle == DeviceLifecycle.RETIRED:
        device.retired_at = timezone.now()
        fields.append("retired_at")
    if lifecycle in {DeviceLifecycle.REVOKED, DeviceLifecycle.RETIRED, DeviceLifecycle.SUSPENDED}:
        device.access_token_hash = ""
        device.access_token_prefix = ""
        fields.extend(["access_token_hash", "access_token_prefix"])
    device.save(update_fields=fields)
    # Free equipment for re-assignment when Device is permanently removed.
    if lifecycle in {DeviceLifecycle.REVOKED, DeviceLifecycle.RETIRED}:
        assignment = getattr(device, "assignment", None)
        if assignment is not None and assignment.equipment_id:
            assignment.equipment = None
            assignment.save(update_fields=["equipment", "updated_at"])
    action = {
        DeviceLifecycle.SUSPENDED: AuditAction.SUSPENDED,
        DeviceLifecycle.REVOKED: AuditAction.REVOKED,
        DeviceLifecycle.RETIRED: AuditAction.RETIRED,
        DeviceLifecycle.ACTIVE: AuditAction.REPROVISIONED,
    }.get(lifecycle, AuditAction.PROVISIONED)
    write_audit(action=action, message=message or f"Lifecycle → {lifecycle}", device=device, actor=actor)
    return device


def authenticate_device_token(raw_token: str) -> ProvisionedDevice | None:
    if not raw_token:
        return None
    prefix = _prefix(raw_token)
    candidates = ProvisionedDevice.objects.filter(
        access_token_prefix=prefix,
        lifecycle=DeviceLifecycle.ACTIVE,
    )
    for device in candidates:
        if _verify(raw_token, device.access_token_hash):
            return device
    return None


@transaction.atomic
def record_heartbeat(*, device: ProvisionedDevice, payload: dict | None = None, request=None) -> DeviceHeartbeat:
    hb = DeviceHeartbeat.objects.create(
        device=device,
        status=str((payload or {}).get("status") or "ok")[:32],
        payload=payload or {},
        client_ip=_client_ip(request),
    )
    device.last_heartbeat_at = hb.received_at
    device.last_seen_ip = hb.client_ip
    device.save(update_fields=["last_heartbeat_at", "last_seen_ip", "updated_at"])
    return hb

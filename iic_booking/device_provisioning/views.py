"""REST API for unified Device Provisioning (Phase R.2.1)."""

from __future__ import annotations

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.authentication import TokenAuthentication

from django.utils import timezone

from iic_booking.device_provisioning.models import (
    AuditAction,
    DepartmentProvisioningPolicy,
    DeviceAuditLog,
    DeviceLifecycle,
    DeviceType,
    ProvisionedDevice,
    ProvisioningMode,
    ProvisioningSession,
    ProvisioningSessionStatus,
)
from iic_booking.device_provisioning import policy as policy_mod
from iic_booking.device_provisioning import services
from iic_booking.platform_compat.manifest import build_capabilities_payload
from iic_booking.platform_compat.semver import compare_installer, traffic_light
from iic_booking.platform_compat.views import deployment_self_test as provisioning_self_test
from iic_booking.sync.permissions import CanManageDepartmentSync

_MANAGE = [IsAuthenticated, CanManageDepartmentSync]


class ProvisioningSessionThrottle(AnonRateThrottle):
    scope = "provisioning_session"
    rate = "30/hour"


def _ser_session(session: ProvisioningSession, *, include_admin: bool = False) -> dict:
    data = {
        "id": str(session.id),
        "device_type": session.device_type,
        "status": session.status,
        "display_name": session.display_name,
        "hostname": session.hostname,
        "machine_guid": session.machine_guid,
        "fingerprint": session.fingerprint,
        "windows_version": session.windows_version,
        "application_version": session.application_version,
        "cpu": session.cpu,
        "ram_gb": str(session.ram_gb) if session.ram_gb is not None else None,
        "mac_addresses": session.mac_addresses,
        "local_ips": session.local_ips,
        "department_id": session.department_id,
        "department_name": session.department.name if session.department_id else None,
        "requested_equipment_id": session.requested_equipment_id,
        "requested_workstation_role": session.requested_workstation_role,
        "device_uuid": str(session.device_id) if session.device_id else None,
        "expires_at": session.expires_at.isoformat() if session.expires_at else None,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "first_seen": session.created_at.isoformat() if session.created_at else None,
        "approved_at": session.approved_at.isoformat() if session.approved_at else None,
        "rejected_at": session.rejected_at.isoformat() if session.rejected_at else None,
        "claimed_at": session.claimed_at.isoformat() if session.claimed_at else None,
        "rejection_reason": session.rejection_reason,
        "auto_approved": bool(session.auto_approved),
        "auto_approve_reason": session.auto_approve_reason or None,
        "device_code": session.device_code or None,
        "device_code_expires_at": (
            session.device_code_expires_at.isoformat() if session.device_code_expires_at else None
        ),
    }
    if include_admin:
        data["client_ip"] = session.client_ip
        data["approved_by"] = getattr(session.approved_by, "email", None) or getattr(
            session.approved_by, "username", None
        )
        data["rejected_by"] = getattr(session.rejected_by, "email", None) or getattr(
            session.rejected_by, "username", None
        )
        data["requested_by"] = getattr(session.requested_by, "email", None) or getattr(
            session.requested_by, "username", None
        )
    return data


def _ser_policy(policy: DepartmentProvisioningPolicy) -> dict:
    return {
        "id": policy.id,
        "department_id": policy.department_id,
        "department_name": policy.department.name if policy.department_id else None,
        "provisioning_mode": policy.provisioning_mode,
        "allowed_networks": list(policy.allowed_networks or []),
        "require_mfa": bool(policy.require_mfa),
        "require_device_fingerprint": bool(policy.require_device_fingerprint),
        "maximum_pending_lifetime_hours": policy.maximum_pending_lifetime_hours,
        "auto_approve_existing_reinstalls": bool(policy.auto_approve_existing_reinstalls),
        "audit_enabled": bool(policy.audit_enabled),
        "updated_at": policy.updated_at.isoformat() if policy.updated_at else None,
        "modes": [{"value": c.value, "label": c.label} for c in ProvisioningMode],
    }


def _ser_device(device: ProvisionedDevice) -> dict:
    assignment = getattr(device, "assignment", None)
    return {
        "id": str(device.id),
        "device_type": device.device_type,
        "lifecycle": device.lifecycle,
        "display_name": device.display_name,
        "hostname": device.hostname,
        "machine_guid": device.machine_guid,
        "fingerprint": device.fingerprint,
        "application_version": device.application_version,
        "windows_version": device.windows_version,
        "department_id": device.department_id,
        "department_name": device.department.name if device.department_id else None,
        "last_heartbeat_at": device.last_heartbeat_at.isoformat() if device.last_heartbeat_at else None,
        "last_seen_ip": device.last_seen_ip,
        "provisioned_at": device.provisioned_at.isoformat() if device.provisioned_at else None,
        "retired_at": device.retired_at.isoformat() if device.retired_at else None,
        "created_at": device.created_at.isoformat() if device.created_at else None,
        "updated_at": device.updated_at.isoformat() if device.updated_at else None,
        "assignment": services._serialize_assignment(assignment),
        "access_token_prefix": device.access_token_prefix or None,
        # Never include access_token_hash or plaintext tokens.
    }


def _ser_audit(row: DeviceAuditLog) -> dict:
    return {
        "id": str(row.id),
        "action": row.action,
        "message": row.message,
        "detail": row.detail,
        "device_id": str(row.device_id) if row.device_id else None,
        "session_id": str(row.session_id) if row.session_id else None,
        "actor": getattr(row.actor, "email", None) or getattr(row.actor, "username", None),
        "client_ip": row.client_ip,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def capabilities(request):
    """
    GET /api/v1/provisioning/capabilities/

    Public capability discovery for installers (Phase R.2.6).
    Replaces the static /iic-device-provisioning.json marker.
    """
    payload = build_capabilities_payload()
    product = (request.query_params.get("product") or "").strip()
    installer_version = (request.query_params.get("installer_version") or "").strip()
    if product:
        compat = compare_installer(
            product, installer_version or "0", payload.get("supported_installers") or {}
        )
        compat["traffic_light"] = traffic_light(compat["status"])
        payload["installer_compatibility"] = compat
    return Response(payload)


@api_view(["GET"])
@permission_classes(_MANAGE)
def console_summary(request):
    """Deployment console hub counts."""
    pending = ProvisioningSession.objects.filter(status=ProvisioningSessionStatus.PENDING).count()
    active = ProvisionedDevice.objects.filter(lifecycle=DeviceLifecycle.ACTIVE).count()
    suspended = ProvisionedDevice.objects.filter(lifecycle=DeviceLifecycle.SUSPENDED).count()
    revoked = ProvisionedDevice.objects.filter(lifecycle=DeviceLifecycle.REVOKED).count()
    retired = ProvisionedDevice.objects.filter(lifecycle=DeviceLifecycle.RETIRED).count()
    provisioning = ProvisionedDevice.objects.filter(lifecycle=DeviceLifecycle.PROVISIONING).count()
    return Response(
        {
            "pending_installations": pending,
            "provisioning": provisioning,
            "active": active,
            "suspended": suspended,
            "revoked": revoked,
            "retired": retired,
            "device_types": [{"value": c.value, "label": c.label} for c in DeviceType],
            "links": {
                "pending": "/api/v1/provisioning/pending/",
                "devices": "/api/v1/provisioning/devices/",
                "retired": "/api/v1/provisioning/devices/retired/",
                "audit": "/api/v1/provisioning/audit/",
            },
        }
    )


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([ProvisioningSessionThrottle])
def sessions_create(request):
    """
    Installer: register unknown device.

    Unauthenticated → Pending Approval (manual / device-code path).
    Authenticated Department Admin+ with TRUSTED/RESTRICTED policy → may Auto Approve (DSA).
    """
    actor = request.user if getattr(request.user, "is_authenticated", False) else None
    try:
        session, proof = services.create_session(
            payload=request.data or {},
            request=request,
            actor=actor,
        )
    except ValueError as exc:
        code = str(exc)
        http = status.HTTP_409_CONFLICT if code == "duplicate_active_device" else status.HTTP_400_BAD_REQUEST
        return Response(
            {"error": {"code": code, "message": "Invalid registration payload."}},
            status=http,
        )
    body = _ser_session(session)
    body["session_proof"] = proof  # once; installer stores DPAPI — never log secrets
    if session.status == ProvisioningSessionStatus.APPROVED:
        body["message"] = "Approved"
    elif session.device_code:
        body["message"] = "DeviceCode"
    else:
        body["message"] = "Pending"
    return Response(body, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([AllowAny])
def session_detail(request, session_id):
    """Installer poll (session_proof) or admin (manage permission)."""
    session = get_object_or_404(ProvisioningSession.objects.select_related("department", "device"), id=session_id)
    session = services.expire_if_needed(session)
    proof = request.headers.get("X-Provisioning-Session-Proof") or request.query_params.get("session_proof")
    is_admin = (
        request.user
        and request.user.is_authenticated
        and CanManageDepartmentSync().has_permission(request, None)
    )
    if not is_admin:
        if not proof or not services._verify(proof, session.session_proof_hash):
            return Response({"detail": "Authentication credentials were not provided."}, status=401)
    return Response(_ser_session(session, include_admin=bool(is_admin)))


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([ProvisioningSessionThrottle])
def session_claim(request, session_id):
    """Installer claim after approval — returns bootstrap pack once (tokens in body only)."""
    session = get_object_or_404(ProvisioningSession, id=session_id)
    proof = (
        request.data.get("session_proof")
        or request.headers.get("X-Provisioning-Session-Proof")
        or ""
    )
    bootstrap = request.data.get("bootstrap_token") or None
    try:
        pack = services.claim_session(
            session=session,
            session_proof=proof,
            bootstrap_token=bootstrap,
            request=request,
        )
    except ValueError as exc:
        return Response(
            {"error": {"code": str(exc), "message": "Claim failed."}},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return Response(pack, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes(_MANAGE)
def pending_list(request):
    qs = (
        ProvisioningSession.objects.filter(status=ProvisioningSessionStatus.PENDING)
        .select_related("department", "approved_by", "rejected_by")
        .order_by("-created_at")[:200]
    )
    # Also expire stale while listing.
    for s in list(qs):
        services.expire_if_needed(s)
    qs = ProvisioningSession.objects.filter(status=ProvisioningSessionStatus.PENDING).select_related(
        "department"
    ).order_by("-created_at")[:200]
    return Response({"count": qs.count(), "results": [_ser_session(s, include_admin=True) for s in qs]})


@api_view(["POST"])
@permission_classes(_MANAGE)
def pending_approve(request, session_id):
    session = get_object_or_404(ProvisioningSession.objects.select_related("department"), id=session_id)
    try:
        session, device, _bootstrap = services.approve_session(
            session=session,
            actor=request.user,
            display_name=request.data.get("display_name"),
            department_id=request.data.get("department_id"),
            equipment_id=request.data.get("equipment_id"),
            workstation_role=request.data.get("workstation_role"),
        )
    except ValueError as exc:
        return Response({"error": {"code": str(exc), "message": "Approve failed."}}, status=400)
    # Intentionally do NOT return bootstrap_token or access_token to the admin UI.
    return Response(
        {
            "session": _ser_session(session, include_admin=True),
            "device": _ser_device(device),
            "message": "Approved. Installer will complete claim automatically.",
        }
    )


@api_view(["POST"])
@permission_classes(_MANAGE)
def pending_approve_by_code(request):
    """DEVICE_CODE mode: administrator approves from another browser using the displayed code."""
    code = str(request.data.get("device_code") or "").strip().upper()
    if not code:
        return Response({"error": {"code": "device_code_required", "message": "Device code required."}}, status=400)
    session = (
        ProvisioningSession.objects.select_related("department")
        .filter(device_code__iexact=code, status=ProvisioningSessionStatus.PENDING)
        .order_by("-created_at")
        .first()
    )
    if session is None:
        return Response({"error": {"code": "device_code_not_found", "message": "Unknown device code."}}, status=404)
    if session.device_code_expires_at and session.device_code_expires_at <= timezone.now():
        return Response({"error": {"code": "device_code_expired", "message": "Device code expired."}}, status=400)
    try:
        session, device, _bootstrap = services.approve_session(
            session=session,
            actor=request.user,
            display_name=request.data.get("display_name"),
            department_id=request.data.get("department_id") or session.department_id,
            equipment_id=request.data.get("equipment_id"),
            workstation_role=request.data.get("workstation_role"),
        )
    except ValueError as exc:
        return Response({"error": {"code": str(exc), "message": "Approve failed."}}, status=400)
    services.write_audit(
        action=AuditAction.APPROVED,
        message="Provisioning approved via device code",
        session=session,
        device=device,
        actor=request.user,
        detail={"device_code_prefix": code[:4], "via": "device_code"},
        client_ip=services._client_ip(request),
    )
    return Response(
        {
            "session": _ser_session(session, include_admin=True),
            "device": _ser_device(device),
            "message": "Approved via device code.",
        }
    )


@api_view(["GET", "PUT", "PATCH"])
@permission_classes(_MANAGE)
def department_policy_detail(request, department_id: int):
    """Department Settings → Device Provisioning policy."""
    from iic_booking.users.models import Department

    department = get_object_or_404(Department, pk=department_id)
    # Existing departments without a row stay MANUAL until explicitly created/updated.
    policy = policy_mod.get_policy(department)
    if request.method == "GET":
        if policy is None:
            return Response(
                {
                    "department_id": department.id,
                    "department_name": department.name,
                    "provisioning_mode": ProvisioningMode.MANUAL_APPROVAL,
                    "allowed_networks": [],
                    "require_mfa": False,
                    "require_device_fingerprint": True,
                    "maximum_pending_lifetime_hours": 24,
                    "auto_approve_existing_reinstalls": True,
                    "audit_enabled": True,
                    "inherited_default": False,
                    "exists": False,
                    "modes": [{"value": c.value, "label": c.label} for c in ProvisioningMode],
                    "note": "No policy row — Manual Approval (backward compatible).",
                }
            )
        body = _ser_policy(policy)
        body["exists"] = True
        return Response(body)

    data = request.data or {}
    mode = str(data.get("provisioning_mode") or "").strip().lower()
    valid_modes = {c.value for c in ProvisioningMode}
    if mode and mode not in valid_modes:
        return Response({"error": {"code": "invalid_mode", "message": "Unknown provisioning mode."}}, status=400)

    if policy is None:
        policy = DepartmentProvisioningPolicy(department=department)

    if mode:
        policy.provisioning_mode = mode
    if "allowed_networks" in data:
        nets = data.get("allowed_networks") or []
        if isinstance(nets, str):
            nets = [n.strip() for n in nets.replace(";", "\n").splitlines() if n.strip()]
        if not isinstance(nets, list):
            return Response({"error": {"code": "invalid_networks", "message": "CIDR list required."}}, status=400)
        policy.allowed_networks = [str(n).strip() for n in nets if str(n).strip()]
    for bool_field in (
        "require_mfa",
        "require_device_fingerprint",
        "auto_approve_existing_reinstalls",
        "audit_enabled",
    ):
        if bool_field in data:
            setattr(policy, bool_field, bool(data.get(bool_field)))
    if "maximum_pending_lifetime_hours" in data:
        try:
            policy.maximum_pending_lifetime_hours = max(1, int(data.get("maximum_pending_lifetime_hours")))
        except (TypeError, ValueError):
            return Response(
                {"error": {"code": "invalid_lifetime", "message": "Lifetime must be a positive integer."}},
                status=400,
            )

    policy.save()
    services.write_audit(
        action=AuditAction.POLICY_UPDATED,
        message="Department provisioning policy updated",
        actor=request.user,
        detail={
            "department_id": department.id,
            "provisioning_mode": policy.provisioning_mode,
            "allowed_networks": policy.allowed_networks,
        },
        client_ip=services._client_ip(request),
    )
    body = _ser_policy(policy)
    body["exists"] = True
    return Response(body)


@api_view(["GET"])
@permission_classes(_MANAGE)
def department_policy_list(request):
    qs = DepartmentProvisioningPolicy.objects.select_related("department").order_by("department__name")
    return Response({"count": qs.count(), "results": [_ser_policy(p) for p in qs]})


@api_view(["POST"])
@permission_classes(_MANAGE)
def pending_reject(request, session_id):
    session = get_object_or_404(ProvisioningSession, id=session_id)
    try:
        session = services.reject_session(
            session=session,
            actor=request.user,
            reason=str(request.data.get("reason") or ""),
        )
    except ValueError as exc:
        return Response({"error": {"code": str(exc), "message": "Reject failed."}}, status=400)
    return Response(_ser_session(session, include_admin=True))


@api_view(["PATCH"])
@permission_classes(_MANAGE)
def pending_update(request, session_id):
    session = get_object_or_404(ProvisioningSession, id=session_id)
    data = request.data or {}
    kwargs = {}
    if "display_name" in data:
        kwargs["display_name"] = data.get("display_name")
    if "department_id" in data:
        kwargs["department_id"] = data.get("department_id")
    if "equipment_id" in data:
        kwargs["equipment_id"] = data.get("equipment_id")
    if "workstation_role" in data:
        kwargs["workstation_role"] = data.get("workstation_role")
    try:
        session = services.update_pending_session(session=session, actor=request.user, **kwargs)
    except ValueError as exc:
        return Response({"error": {"code": str(exc), "message": "Update failed."}}, status=400)
    return Response(_ser_session(session, include_admin=True))


@api_view(["GET"])
@permission_classes(_MANAGE)
def devices_list(request):
    lifecycle = request.query_params.get("lifecycle")
    device_type = request.query_params.get("device_type")
    qs = ProvisionedDevice.objects.select_related("department", "assignment", "assignment__equipment").exclude(
        lifecycle=DeviceLifecycle.RETIRED
    )
    if lifecycle:
        qs = qs.filter(lifecycle=lifecycle)
    if device_type:
        qs = qs.filter(device_type=device_type)
    qs = qs.order_by("-updated_at")[:500]
    return Response({"count": qs.count(), "results": [_ser_device(d) for d in qs]})


@api_view(["GET"])
@permission_classes(_MANAGE)
def devices_retired(request):
    qs = (
        ProvisionedDevice.objects.filter(lifecycle=DeviceLifecycle.RETIRED)
        .select_related("department", "assignment")
        .order_by("-retired_at")[:500]
    )
    return Response({"count": qs.count(), "results": [_ser_device(d) for d in qs]})


@api_view(["GET"])
@permission_classes(_MANAGE)
def device_detail(request, device_id):
    device = get_object_or_404(
        ProvisionedDevice.objects.select_related("department", "assignment", "assignment__equipment"),
        id=device_id,
    )
    return Response(_ser_device(device))


def _lifecycle_action(request, device_id, lifecycle: str, message: str):
    device = get_object_or_404(ProvisionedDevice, id=device_id)
    try:
        device = services.set_lifecycle(
            device=device,
            lifecycle=lifecycle,
            actor=request.user,
            message=message,
        )
    except ValueError as exc:
        return Response({"error": {"code": str(exc), "message": "Lifecycle change failed."}}, status=400)
    return Response(_ser_device(device))


@api_view(["POST"])
@permission_classes(_MANAGE)
def device_suspend(request, device_id):
    return _lifecycle_action(request, device_id, DeviceLifecycle.SUSPENDED, "Device suspended")


@api_view(["POST"])
@permission_classes(_MANAGE)
def device_revoke(request, device_id):
    return _lifecycle_action(request, device_id, DeviceLifecycle.REVOKED, "Device revoked")


@api_view(["POST"])
@permission_classes(_MANAGE)
def device_retire(request, device_id):
    return _lifecycle_action(request, device_id, DeviceLifecycle.RETIRED, "Device retired")


@api_view(["GET"])
@permission_classes([AllowAny])
@authentication_classes([])
def dsa_equipment_tree(request):
    """
    DSA installer equipment tree authorized by Device Provisioning credentials.

    Auth (any one):
    - Authorization: Bearer <claim access_token>  (or X-Agent-Access-Token)
    - Authorization: Token <portal admin token>   (department-scoped)

    Does not require Agent UUID or enrollment secret.
    """
    from iic_booking.device_provisioning import policy as policy_mod
    from iic_booking.sync.installer.services import build_equipment_tree_for_department, resolve_installer_agent
    from iic_booking.users.models.user_type import UserType

    # 1) Claim / device bearer (preferred for post-claim installer)
    ok, err, agent = resolve_installer_agent(request, allow_access_token=True)
    if ok and agent is not None:
        tree = build_equipment_tree_for_department(getattr(agent, "department_id", None))
        tree["agent_uuid"] = str(agent.agent_uuid)
        tree["department_id"] = str(agent.department_id) if agent.department_id else None
        tree["auth"] = "device_bearer"
        return Response(tree)

    # 2) Portal admin Token (pre-claim / upgrade discovery)
    auth_header = request.META.get("HTTP_AUTHORIZATION") or ""
    if auth_header.lower().startswith("token "):
        try:
            result = TokenAuthentication().authenticate(request)
        except Exception:
            result = None
        if result:
            user, _ = result
            request.user = user
            dept_id = None
            raw_dept = request.query_params.get("department_id")
            if raw_dept:
                try:
                    dept_id = int(raw_dept)
                except (TypeError, ValueError):
                    return Response({"detail": "Invalid department_id."}, status=400)
            elif getattr(user, "department_id", None):
                dept_id = int(user.department_id)

            is_main = getattr(user, "is_superuser", False) or str(getattr(user, "user_type", "")).lower() == UserType.ADMIN
            if dept_id is not None:
                from iic_booking.users.models import Department

                department = Department.objects.filter(pk=dept_id).first()
                if department is None:
                    return Response({"detail": "Department not found."}, status=404)
                if not is_main and not policy_mod.user_has_provisioning_rights(user, department):
                    return Response({"detail": "You do not have provisioning rights for this department."}, status=403)
            elif not is_main:
                return Response({"detail": "department_id is required."}, status=400)

            tree = build_equipment_tree_for_department(dept_id)
            tree["auth"] = "portal_token"
            return Response(tree)

    return Response(
        {"detail": err or "Provisioning Bearer token or portal Token is required."},
        status=status.HTTP_403_FORBIDDEN,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def unassigned_equipment(request):
    """
    Equipment PC Wizard: list instruments not already mapped to an active provisioned device.
    Requires Department Admin+ (or Main Admin). Scoped to caller's department unless Main Admin.
    """
    from iic_booking.device_provisioning import policy as policy_mod
    from iic_booking.users.models import Department

    user = request.user
    raw_dept = request.query_params.get("department_id")
    department = None
    if raw_dept:
        try:
            department = Department.objects.filter(pk=int(raw_dept)).first()
        except (TypeError, ValueError):
            return Response({"error": {"code": "department_not_found"}}, status=400)
        if department is None:
            return Response({"error": {"code": "department_not_found"}}, status=404)
    elif getattr(user, "department_id", None):
        department = getattr(user, "department", None)

    if department is None and not (getattr(user, "is_superuser", False) or str(getattr(user, "user_type", "")).lower() == "admin"):
        return Response(
            {"error": {"code": "department_required", "message": "Department context required."}},
            status=400,
        )

    if department is not None and not policy_mod.user_has_provisioning_rights(user, department):
        return Response({"detail": "You do not have provisioning rights for this department."}, status=403)
    if department is None:
        # Main Admin listing all — still require admin-level type
        if not (getattr(user, "is_superuser", False) or str(getattr(user, "user_type", "")).lower() == "admin"):
            return Response({"detail": "Permission denied."}, status=403)

    rows = services.list_unassigned_equipment(
        department_id=department.id if department else None,
        actor=user,
    )
    return Response({"count": len(rows), "results": rows})


@api_view(["POST"])
@permission_classes(_MANAGE)
def device_replace(request, device_id):
    """Replace Existing Device — revoke old binding (Equipment PC or Remote Analysis)."""
    device = get_object_or_404(ProvisionedDevice, id=device_id)
    try:
        device = services.replace_device(device=device, actor=request.user)
    except ValueError as exc:
        return Response({"error": {"code": str(exc), "message": "Replace failed."}}, status=400)
    msg = (
        "Previous Remote Analysis workstation revoked. Re-run the agent installer on the replacement PC."
        if device.device_type == DeviceType.REMOTE_ANALYSIS
        else "Previous Equipment PC revoked. Re-run the wizard to assign this equipment to a new PC."
    )
    return Response(
        {
            "device": _ser_device(device),
            "message": msg,
        }
    )


@api_view(["GET"])
@permission_classes(_MANAGE)
def audit_list(request):
    qs = DeviceAuditLog.objects.select_related("actor", "device", "session").order_by("-created_at")[:500]
    action = request.query_params.get("action")
    if action:
        qs = qs.filter(action=action)[:500]
    return Response({"count": qs.count(), "results": [_ser_audit(r) for r in qs]})


@api_view(["POST"])
@permission_classes([AllowAny])
def device_heartbeat(request, device_id):
    """Bearer device heartbeat (Authorization: Bearer <access_token>)."""
    auth = request.headers.get("Authorization") or ""
    raw = ""
    if auth.lower().startswith("bearer "):
        raw = auth[7:].strip()
    device = services.authenticate_device_token(raw)
    if device is None or str(device.id) != str(device_id):
        return Response({"detail": "Invalid credentials."}, status=401)
    hb = services.record_heartbeat(device=device, payload=request.data if isinstance(request.data, dict) else {}, request=request)
    return Response({"accepted": True, "received_at": hb.received_at.isoformat()})

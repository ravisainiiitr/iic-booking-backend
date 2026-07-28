"""Portal API for Department Sync Agent registration and token issuance."""

from __future__ import annotations

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from iic_booking.users.models.department import Department
from iic_booking.users.models.sync_agent import SyncAgent, _generate_token


def _iso(dt):
    if dt is None:
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.utc)
    return dt.isoformat()


def _registration_payload(agent: SyncAgent, *, success: bool, message: str, control_plane: dict | None = None) -> dict:
    payload = {
        "agentId": str(agent.id),
        "registrationToken": agent.registration_token,
        "registeredAt": _iso(agent.registered_at),
        "success": success,
        "message": message,
    }
    if control_plane:
        payload.update(control_plane)
    return payload


def _login_payload(agent: SyncAgent, *, success: bool, message: str, control_plane: dict | None = None) -> dict:
    payload = {
        "accessToken": agent.access_token if success else "",
        "refreshToken": agent.refresh_token if success else None,
        "expiresAt": _iso(agent.access_token_expires_at) if success else None,
        "success": success,
        "message": message,
    }
    if control_plane:
        payload.update(control_plane)
    return payload


def _bridge_control_plane(agent: SyncAgent, *, issue_token: bool = False) -> dict:
    """Milestone 11 — expose Track B identity alongside Track A credentials."""
    try:
        from iic_booking.sync.services.agent_identity_bridge import ensure_department_sync_agent

        dsa, sync_token = ensure_department_sync_agent(agent, issue_token=issue_token)
        result = {
            "agentUuid": str(dsa.agent_uuid),
            "controlPlaneAgentId": str(dsa.id),
        }
        if sync_token:
            result["syncAccessToken"] = sync_token
            if dsa.access_token_expires_at:
                result["syncAccessTokenExpiresAt"] = _iso(dsa.access_token_expires_at)
        return result
    except Exception:
        return {}


@api_view(["POST"])
@permission_classes([AllowAny])
def sync_agent_register(request):
    """
    Register a Support PC agent.

    Expected body (camelCase, from DSA):
      agentName, departmentCode, machineName, machineGuid, version, operatingSystem
    """
    data = request.data if isinstance(request.data, dict) else {}
    agent_name = (data.get("agentName") or data.get("agent_name") or "").strip()
    department_code = (data.get("departmentCode") or data.get("department_code") or "").strip()
    machine_name = (data.get("machineName") or data.get("machine_name") or "").strip()
    machine_guid = (data.get("machineGuid") or data.get("machine_guid") or "").strip()
    version = (data.get("version") or "").strip()
    operating_system = (data.get("operatingSystem") or data.get("operating_system") or "").strip()

    if not agent_name:
        return Response(
            {"success": False, "message": "agentName is required.", "agentId": None, "registrationToken": None},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not department_code:
        return Response(
            {
                "success": False,
                "message": "departmentCode is required.",
                "agentId": None,
                "registrationToken": None,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not machine_guid:
        return Response(
            {
                "success": False,
                "message": "machineGuid is required.",
                "agentId": None,
                "registrationToken": None,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    department = (
        Department.objects.filter(code__iexact=department_code).order_by("id").first()
    )
    if department is None:
        return Response(
            {
                "success": False,
                "message": f"Unknown department code '{department_code}'.",
                "agentId": None,
                "registrationToken": None,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    existing = SyncAgent.objects.filter(machine_guid=machine_guid).first()
    if existing is not None:
        existing.agent_name = agent_name
        existing.department = department
        existing.department_code = department.code or department_code
        existing.machine_name = machine_name
        existing.version = version
        existing.operating_system = operating_system
        existing.is_active = True
        if not existing.registration_token:
            existing.registration_token = _generate_token()
        existing.save()
        return Response(
            _registration_payload(
                existing,
                success=True,
                message="Agent already registered. Returning existing credentials.",
                control_plane=_bridge_control_plane(existing, issue_token=False),
            )
        )

    agent = SyncAgent.objects.create(
        agent_name=agent_name,
        department=department,
        department_code=department.code or department_code,
        machine_name=machine_name,
        machine_guid=machine_guid,
        version=version,
        operating_system=operating_system,
        registration_token=_generate_token(),
        registered_at=timezone.now(),
    )
    return Response(
        _registration_payload(
            agent,
            success=True,
            message="Agent registered successfully.",
            control_plane=_bridge_control_plane(agent, issue_token=False),
        ),
        status=status.HTTP_201_CREATED,
    )


@api_view(["POST"])
@permission_classes([AllowAny])
def sync_agent_authenticate(request):
    """Issue access/refresh tokens using agentId + registrationToken."""
    data = request.data if isinstance(request.data, dict) else {}
    agent_id = (data.get("agentId") or data.get("agent_id") or "").strip()
    registration_token = (
        data.get("registrationToken") or data.get("registration_token") or ""
    ).strip()

    if not agent_id or not registration_token:
        return Response(
            _login_payload_empty("agentId and registrationToken are required."),
            status=status.HTTP_400_BAD_REQUEST,
        )

    agent = SyncAgent.objects.filter(id=agent_id, is_active=True).first()
    if agent is None or agent.registration_token != registration_token:
        return Response(
            _login_payload_empty("Invalid agent credentials."),
            status=status.HTTP_401_UNAUTHORIZED,
        )

    agent.issue_tokens()
    agent.save(
        update_fields=[
            "access_token",
            "refresh_token",
            "access_token_expires_at",
            "last_authenticated_at",
            "updated_at",
        ]
    )
    return Response(
        _login_payload(
            agent,
            success=True,
            message="Authenticated.",
            control_plane=_bridge_control_plane(agent, issue_token=True),
        )
    )


@api_view(["POST"])
@permission_classes([AllowAny])
def sync_agent_refresh(request):
    """Refresh access token using agentId + refreshToken."""
    data = request.data if isinstance(request.data, dict) else {}
    agent_id = (data.get("agentId") or data.get("agent_id") or "").strip()
    refresh_token = (data.get("refreshToken") or data.get("refresh_token") or "").strip()

    if not agent_id or not refresh_token:
        return Response(
            _login_payload_empty("agentId and refreshToken are required."),
            status=status.HTTP_400_BAD_REQUEST,
        )

    agent = SyncAgent.objects.filter(id=agent_id, is_active=True).first()
    if agent is None or not agent.refresh_token or agent.refresh_token != refresh_token:
        return Response(
            _login_payload_empty("Invalid refresh token."),
            status=status.HTTP_401_UNAUTHORIZED,
        )

    agent.issue_tokens()
    agent.save(
        update_fields=[
            "access_token",
            "refresh_token",
            "access_token_expires_at",
            "last_authenticated_at",
            "updated_at",
        ]
    )
    return Response(
        _login_payload(
            agent,
            success=True,
            message="Token refreshed.",
            control_plane=_bridge_control_plane(agent, issue_token=True),
        )
    )

def _login_payload_empty(message: str) -> dict:
    return {
        "accessToken": "",
        "refreshToken": None,
        "expiresAt": None,
        "success": False,
        "message": message,
    }


@api_view(["GET"])
@permission_classes([AllowAny])
def sync_agent_ping(request):
    """Lightweight liveness check for Department Sync Agent connectivity."""
    return Response(
        {
            "status": "ok",
            "server_time": _iso(timezone.now()),
            "version": "1.0.0",
        }
    )


def _authenticate_sync_agent(request):
    """Validate Bearer access token issued by sync_agent_authenticate."""
    auth = request.META.get("HTTP_AUTHORIZATION") or ""
    if not auth.lower().startswith("bearer "):
        return None
    token = auth[7:].strip()
    if not token:
        return None
    agent = (
        SyncAgent.objects.filter(access_token=token, is_active=True)
        .select_related("department")
        .first()
    )
    if agent is None:
        return None
    if agent.access_token_expires_at and agent.access_token_expires_at < timezone.now():
        return None
    return agent


def _build_unc_path(*, unc_path: str, ip_address: str, share_name: str) -> str:
    unc = (unc_path or "").strip()
    if unc:
        return unc
    ip = (ip_address or "").strip()
    share = (share_name or "").strip()
    if ip and share:
        return f"\\\\{ip}\\{share}"
    return ""


@api_view(["GET"])
@permission_classes([AllowAny])
def sync_agent_instruments(request):
    """
    Instruments assigned to the authenticated Sync Agent's department.

    Authorization: Bearer <access_token>
    """
    agent = _authenticate_sync_agent(request)
    if agent is None:
        return Response(
            {"detail": "Authentication credentials were not provided or are invalid."},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    from iic_booking.equipment.models import Equipment

    qs = Equipment.objects.filter(dsa_enabled=True).order_by("code")
    department = agent.department
    if department is not None:
        qs = qs.filter(internal_department_id=department.id)
    elif agent.department_code:
        qs = qs.filter(internal_department__code__iexact=agent.department_code)
    else:
        qs = qs.none()

    payload = []
    for eq in qs:
        unc = _build_unc_path(
            unc_path=eq.dsa_unc_path,
            ip_address=eq.dsa_ip_address,
            share_name=eq.dsa_share_name,
        )
        payload.append(
            {
                "id": eq.equipment_id,
                "equipment_code": eq.code,
                "name": eq.name,
                "hostname": (eq.dsa_hostname or "").strip(),
                "ip_address": (eq.dsa_ip_address or "").strip(),
                "share_name": (eq.dsa_share_name or "").strip(),
                "unc_path": unc,
                "enabled": bool(eq.dsa_enabled),
                "watch_folder_enabled": bool(eq.dsa_watch_folder_enabled),
                "department_id": eq.internal_department_id,
            }
        )

    return Response(payload)

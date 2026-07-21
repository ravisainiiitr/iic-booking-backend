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


def _registration_payload(agent: SyncAgent, *, success: bool, message: str) -> dict:
    return {
        "agentId": str(agent.id),
        "registrationToken": agent.registration_token,
        "registeredAt": _iso(agent.registered_at),
        "success": success,
        "message": message,
    }


def _login_payload(agent: SyncAgent, *, success: bool, message: str) -> dict:
    return {
        "accessToken": agent.access_token if success else "",
        "refreshToken": agent.refresh_token if success else None,
        "expiresAt": _iso(agent.access_token_expires_at) if success else None,
        "success": success,
        "message": message,
    }


@api_view(["POST"])
@permission_classes([AllowAny])
def sync_agent_register(request):
    """
    Register a Support PC agent.

    Expected body (camelCase, from DSA):
      agentName, departmentCode, machineName, machineGuid, version, operatingSystem
    """
    import logging

    logger = logging.getLogger(__name__)
    logger.warning(
        "sync_agent_register content_type=%s body=%r data=%r",
        request.content_type,
        request.body[:2000] if request.body else b"",
        request.data,
    )
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
        _registration_payload(agent, success=True, message="Agent registered successfully."),
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
    return Response(_login_payload(agent, success=True, message="Authenticated."))


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
    return Response(_login_payload(agent, success=True, message="Token refreshed."))


def _login_payload_empty(message: str) -> dict:
    return {
        "accessToken": "",
        "refreshToken": None,
        "expiresAt": None,
        "success": False,
        "message": message,
    }

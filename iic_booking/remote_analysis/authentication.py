"""DRF authentication for Remote Analysis Agents."""

from __future__ import annotations

from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import authentication, exceptions

from iic_booking.remote_analysis.constants import AuditCategory
from iic_booking.remote_analysis.models import AnalysisWorkstation
from iic_booking.remote_analysis.services.audit import record_event
from iic_booking.remote_analysis.services.tokens import find_active_token


class RemoteAnalysisAgentUser:
    """Lightweight principal for agent-authenticated requests."""

    is_authenticated = True
    is_anonymous = False
    is_staff = False
    is_superuser = False

    def __init__(self, workstation: AnalysisWorkstation):
        self.workstation = workstation
        self.pk = workstation.pk
        self.id = workstation.pk

    def __str__(self) -> str:
        return f"RemoteAnalysisAgent:{self.workstation.agent_id}"


class RemoteAnalysisAgentAuthentication(authentication.BaseAuthentication):
    """
    Authenticate agents with:
      Authorization: Bearer <token>
      X-Agent-Id: <agent_id>
    """

    keyword = "Bearer"

    def authenticate(self, request):
        auth_header = authentication.get_authorization_header(request).decode("utf-8")
        if not auth_header:
            return None

        parts = auth_header.split()
        if len(parts) != 2 or parts[0] != self.keyword:
            return None

        token = parts[1].strip()
        agent_id = request.META.get("HTTP_X_AGENT_ID") or request.headers.get("X-Agent-Id")
        if not agent_id:
            agent_id = request.query_params.get("agentId")
        if not agent_id:
            try:
                agent_id = (request.data or {}).get("agentId") or (request.data or {}).get("agent_id")
            except Exception:
                agent_id = None

        if not agent_id:
            raise exceptions.AuthenticationFailed(_("X-Agent-Id header is required."))

        workstation = AnalysisWorkstation.objects.filter(agent_id=str(agent_id)).first()
        if workstation is None:
            record_event(
                category=AuditCategory.AUTHENTICATION,
                action="Failed",
                details=f"Unknown agentId {agent_id}",
                success=False,
            )
            raise exceptions.AuthenticationFailed(_("Unknown agent."))

        token_row = find_active_token(workstation, token)
        if token_row is None:
            record_event(
                category=AuditCategory.AUTHENTICATION,
                action="Failed",
                details="Invalid or expired token",
                success=False,
                workstation=workstation,
            )
            raise exceptions.AuthenticationFailed(_("Invalid or expired agent token."))

        if not workstation.enabled or workstation.status == "DISABLED":
            raise exceptions.AuthenticationFailed(_("Workstation is disabled."))

        token_row.last_used_at = timezone.now()
        token_row.save(update_fields=["last_used_at"])
        request.analysis_workstation = workstation
        return (RemoteAnalysisAgentUser(workstation), token)

    def authenticate_header(self, request):
        return self.keyword

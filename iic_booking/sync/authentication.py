"""
DRF authentication for Department Sync Agents.

Agents authenticate with:
  Authorization: Bearer <access_token>
  X-Agent-UUID: <agent_uuid>

Tokens are stored hashed and are revocable. Session authentication is not used.
"""

from __future__ import annotations

import uuid

from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import authentication, exceptions

from iic_booking.sync.models import AgentLifecycleStatus, DepartmentSyncAgent, SyncLogCategory, SyncLogSeverity
from iic_booking.sync.services.logging import EVENT_AUTH_FAILED, write_sync_log
from iic_booking.sync.services.tokens import verify_hash


class SyncAgentUser:
    """
    Lightweight principal attached to request.user for agent-authenticated calls.

    Not a Django User — DRF only requires is_authenticated for permission checks.
    """

    is_authenticated = True
    is_anonymous = False
    is_staff = False
    is_superuser = False

    def __init__(self, agent: DepartmentSyncAgent):
        self.agent = agent
        self.pk = agent.pk
        self.id = agent.pk

    def __str__(self) -> str:
        return f"SyncAgent:{self.agent.agent_uuid}"


class DepartmentSyncAgentAuthentication(authentication.BaseAuthentication):
    """Authenticate Department Sync Agents via UUID + Bearer access token."""

    keyword = "Bearer"
    header_agent_uuid = "HTTP_X_AGENT_UUID"

    def authenticate(self, request):
        auth_header = authentication.get_authorization_header(request).decode("utf-8")
        if not auth_header:
            raise exceptions.AuthenticationFailed(_("Authentication credentials were not provided."))

        parts = auth_header.split()
        if len(parts) != 2 or parts[0] != self.keyword:
            raise exceptions.AuthenticationFailed(_("Invalid authorization header."))

        token = parts[1].strip()
        agent_uuid_raw = request.META.get(self.header_agent_uuid) or request.headers.get("X-Agent-UUID")

        # Milestone 11: allow Track A SyncAgent JWT to authenticate the control plane
        # when X-Agent-UUID is absent (bridges to DepartmentSyncAgent by machine_guid).
        if not agent_uuid_raw:
            bridged = self._authenticate_via_sync_agent_jwt(request, token)
            if bridged is not None:
                return bridged
            raise exceptions.AuthenticationFailed(_("X-Agent-UUID header is required."))

        try:
            agent_uuid = uuid.UUID(str(agent_uuid_raw))
        except (TypeError, ValueError) as exc:
            raise exceptions.AuthenticationFailed(_("Invalid agent UUID.")) from exc

        agent = (
            DepartmentSyncAgent.objects.select_related("department", "equipment")
            .filter(agent_uuid=agent_uuid)
            .first()
        )
        if agent is None:
            raise exceptions.AuthenticationFailed(_("Unknown agent."))

        self._assert_agent_allowed(agent)
        self._assert_token_valid(agent, token)
        self._assert_request_signature(request, agent)
        request.sync_agent = agent
        return (SyncAgentUser(agent), token)

    def _authenticate_via_sync_agent_jwt(self, request, token: str):
        from iic_booking.sync.services.agent_identity_bridge import ensure_department_sync_agent
        from iic_booking.users.models.sync_agent import SyncAgent

        sync_agent = (
            SyncAgent.objects.filter(access_token=token, is_active=True)
            .select_related("department")
            .first()
        )
        if sync_agent is None:
            return None
        if sync_agent.access_token_expires_at and sync_agent.access_token_expires_at < timezone.now():
            return None

        try:
            agent, _ = ensure_department_sync_agent(sync_agent, issue_token=False)
        except ValueError as exc:
            raise exceptions.AuthenticationFailed(str(exc)) from exc

        self._assert_agent_allowed(agent)
        self._assert_request_signature(request, agent)
        request.sync_agent = agent
        return (SyncAgentUser(agent), token)

    def _assert_request_signature(self, request, agent: DepartmentSyncAgent) -> None:
        from iic_booking.sync.services.security import RequestSigningService

        ok, reason = RequestSigningService().verify_request(request, agent)
        if not ok:
            raise exceptions.AuthenticationFailed(reason or _("Invalid request signature."))

    def _assert_agent_allowed(self, agent: DepartmentSyncAgent) -> None:
        if agent.status == AgentLifecycleStatus.REVOKED:
            write_sync_log(
                event_code=EVENT_AUTH_FAILED,
                message="Authentication failed: agent revoked",
                category=SyncLogCategory.AUTH,
                severity=SyncLogSeverity.WARNING,
                sync_agent=agent,
                durable=True,
            )
            raise exceptions.AuthenticationFailed(_("Agent is revoked."))

        if agent.status == AgentLifecycleStatus.DISABLED or not agent.is_active:
            write_sync_log(
                event_code=EVENT_AUTH_FAILED,
                message="Authentication failed: agent disabled",
                category=SyncLogCategory.AUTH,
                severity=SyncLogSeverity.WARNING,
                sync_agent=agent,
                durable=True,
            )
            raise exceptions.AuthenticationFailed(_("Agent is disabled."))

    def _assert_token_valid(self, agent: DepartmentSyncAgent, token: str) -> None:
        if not agent.access_token_hash or not verify_hash(token, agent.access_token_hash):
            write_sync_log(
                event_code=EVENT_AUTH_FAILED,
                message="Authentication failed: invalid token",
                category=SyncLogCategory.AUTH,
                severity=SyncLogSeverity.WARNING,
                sync_agent=agent,
                durable=True,
            )
            raise exceptions.AuthenticationFailed(_("Invalid or expired access token."))

        if agent.access_token_expires_at and agent.access_token_expires_at < timezone.now():
            write_sync_log(
                event_code=EVENT_AUTH_FAILED,
                message="Authentication failed: token expired",
                category=SyncLogCategory.AUTH,
                severity=SyncLogSeverity.WARNING,
                sync_agent=agent,
                durable=True,
            )
            raise exceptions.AuthenticationFailed(_("Invalid or expired access token."))

    def authenticate_header(self, request):
        return self.keyword

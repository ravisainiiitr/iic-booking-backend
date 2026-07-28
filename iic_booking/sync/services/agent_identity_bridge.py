"""Bridge Track A SyncAgent identity to Track B DepartmentSyncAgent control plane."""

from __future__ import annotations

import uuid

from django.db import transaction
from django.utils import timezone

from iic_booking.sync.models import AgentLifecycleStatus, DepartmentSyncAgent
from iic_booking.sync.services.tokens import issue_access_token
from iic_booking.users.models.sync_agent import SyncAgent


@transaction.atomic
def ensure_department_sync_agent(
    sync_agent: SyncAgent,
    *,
    issue_token: bool = False,
) -> tuple[DepartmentSyncAgent, str | None]:
    """
    Ensure a DepartmentSyncAgent exists for the given SyncAgent (matched by machine_guid).

    Returns (dsa_agent, sync_access_token_or_None).
    """
    try:
        machine_guid = uuid.UUID(str(sync_agent.machine_guid))
    except (TypeError, ValueError) as exc:
        raise ValueError("SyncAgent.machine_guid is not a valid UUID.") from exc

    department = sync_agent.department
    if department is None:
        raise ValueError("SyncAgent has no department; cannot bridge control-plane identity.")

    agent = (
        DepartmentSyncAgent.objects.select_related("department", "equipment")
        .filter(machine_guid=machine_guid)
        .first()
    )

    now = timezone.now()
    if agent is None:
        agent = DepartmentSyncAgent.objects.create(
            agent_name=sync_agent.agent_name or "Department Sync Agent",
            department=department,
            machine_name=sync_agent.machine_name or "",
            machine_guid=machine_guid,
            version=(sync_agent.version or "")[:50],
            operating_system=(sync_agent.operating_system or "")[:200],
            status=AgentLifecycleStatus.ENROLLED,
            is_active=True,
            last_seen_at=now,
        )
    else:
        agent.agent_name = sync_agent.agent_name or agent.agent_name
        agent.department = department
        agent.machine_name = sync_agent.machine_name or agent.machine_name
        agent.version = (sync_agent.version or agent.version or "")[:50]
        agent.operating_system = (sync_agent.operating_system or agent.operating_system or "")[:200]
        agent.is_active = True
        if agent.status == AgentLifecycleStatus.REGISTERED:
            agent.status = AgentLifecycleStatus.ENROLLED
        agent.last_seen_at = now
        agent.save(
            update_fields=[
                "agent_name",
                "department",
                "machine_name",
                "version",
                "operating_system",
                "is_active",
                "status",
                "last_seen_at",
                "updated_at",
            ]
        )

    plaintext = None
    if issue_token:
        plaintext = issue_access_token(agent)

    return agent, plaintext

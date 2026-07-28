"""Shared helpers for agent versioning and token issuance."""

from __future__ import annotations

import secrets
from datetime import timedelta

from django.contrib.auth.hashers import check_password, make_password
from django.db.models import Max, Prefetch
from django.utils import timezone

from iic_booking.sync.constants import access_token_lifetime_hours, bootstrap_schema_version
from iic_booking.sync.models import AgentAssignment, DepartmentSyncAgent, EquipmentSyncProfile


def hash_value(plaintext: str) -> str:
    return make_password(plaintext)


def verify_hash(plaintext: str, hashed: str) -> bool:
    if not plaintext or not hashed:
        return False
    return check_password(plaintext, hashed)


def issue_access_token(agent: DepartmentSyncAgent) -> str:
    """Generate a new access token, store its hash, return plaintext once."""
    plaintext = secrets.token_urlsafe(48)
    now = timezone.now()
    agent.access_token_hash = hash_value(plaintext)
    agent.access_token_issued_at = now
    agent.access_token_expires_at = now + timedelta(hours=access_token_lifetime_hours())
    agent.save(
        update_fields=[
            "access_token_hash",
            "access_token_issued_at",
            "access_token_expires_at",
            "updated_at",
        ]
    )
    return plaintext


def revoke_access_token(agent: DepartmentSyncAgent) -> None:
    agent.access_token_hash = ""
    agent.access_token_expires_at = None
    agent.access_token_issued_at = None
    agent.save(
        update_fields=[
            "access_token_hash",
            "access_token_expires_at",
            "access_token_issued_at",
            "updated_at",
        ]
    )


def agent_expected_versions(agent: DepartmentSyncAgent) -> tuple[int, int]:
    """
    Return (configuration_version, schema_version) expected by the portal.

    configuration_version = max active profile configuration_version (or 1)
    schema_version = max(portal bootstrap schema, active profile schema versions)
    """
    aggregates = (
        EquipmentSyncProfile.objects.filter(
            assignments__sync_agent=agent,
            assignments__is_active=True,
        )
        .aggregate(
            max_config=Max("configuration_version"),
            max_schema=Max("schema_version"),
        )
    )
    config_version = aggregates["max_config"] or 1
    profile_schema = aggregates["max_schema"] or 1
    schema_version = max(bootstrap_schema_version(), profile_schema)
    return int(config_version), int(schema_version)


def load_agent_with_assignments(agent_uuid) -> DepartmentSyncAgent | None:
    return (
        DepartmentSyncAgent.objects.select_related("department", "laboratory")
        .prefetch_related(
            Prefetch(
                "assignments",
                queryset=AgentAssignment.objects.filter(is_active=True).select_related(
                    "sync_profile",
                    "sync_profile__equipment",
                    "sync_profile__equipment__internal_department",
                ),
            )
        )
        .filter(agent_uuid=agent_uuid)
        .first()
    )

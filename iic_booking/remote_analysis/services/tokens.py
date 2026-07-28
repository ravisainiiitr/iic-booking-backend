"""Agent token hashing, issuance, rotation, and revocation."""

from __future__ import annotations

import secrets
from datetime import timedelta

from django.contrib.auth.hashers import check_password, make_password
from django.utils import timezone

from iic_booking.remote_analysis.constants import DEFAULT_TOKEN_LIFETIME_DAYS
from iic_booking.remote_analysis.models import AgentToken, AnalysisWorkstation


def hash_token(plaintext: str) -> str:
    return make_password(plaintext)


def verify_token(plaintext: str, hashed: str) -> bool:
    if not plaintext or not hashed:
        return False
    return check_password(plaintext, hashed)


def issue_agent_token(
    workstation: AnalysisWorkstation,
    *,
    lifetime_days: int = DEFAULT_TOKEN_LIFETIME_DAYS,
    rotation_of: AgentToken | None = None,
) -> tuple[AgentToken, str]:
    """Issue a new agent token. Returns (token_row, plaintext). Plaintext shown once."""
    plaintext = secrets.token_urlsafe(48)
    now = timezone.now()
    token = AgentToken.objects.create(
        workstation=workstation,
        token_hash=hash_token(plaintext),
        token_prefix=plaintext[:8],
        expires_at=now + timedelta(days=lifetime_days),
        rotation_of=rotation_of,
        is_active=True,
    )
    return token, plaintext


def revoke_token(token: AgentToken) -> None:
    token.is_active = False
    token.revoked_at = timezone.now()
    token.save(update_fields=["is_active", "revoked_at"])


def revoke_all_tokens(workstation: AnalysisWorkstation) -> int:
    now = timezone.now()
    return AgentToken.objects.filter(workstation=workstation, is_active=True).update(
        is_active=False,
        revoked_at=now,
    )


def rotate_agent_token(workstation: AnalysisWorkstation) -> tuple[AgentToken, str]:
    previous = (
        AgentToken.objects.filter(workstation=workstation, is_active=True)
        .order_by("-issued_at")
        .first()
    )
    if previous:
        revoke_token(previous)
    return issue_agent_token(workstation, rotation_of=previous)


def find_active_token(workstation: AnalysisWorkstation, plaintext: str) -> AgentToken | None:
    now = timezone.now()
    candidates = AgentToken.objects.filter(workstation=workstation, is_active=True)
    for token in candidates:
        if token.expires_at and token.expires_at < now:
            continue
        if verify_token(plaintext, token.token_hash):
            return token
    return None

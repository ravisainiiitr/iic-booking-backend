"""
Feature flag, eligibility and workspace authorization for My Research.

Eligibility reuses the portal's authoritative internal-user rule (`is_internal_iitr_user`):
active IITR students, and faculty of internal departments. It is re-checked on every request for
both owners and viewers, so a user who stops being eligible loses access immediately.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

from iic_booking.equipment.results_sharing_service import internal_iitr_users, is_internal_iitr_user

from .models import MemberRole, ResearchWorkspace, ResearchWorkspaceMember

DISABLED_CODE = "my_research_disabled"
NOT_ELIGIBLE_CODE = "my_research_not_eligible"
NOT_ELIGIBLE_MESSAGE = "My Research is available only to IIT Roorkee students and faculty."


def feature_enabled() -> bool:
    return bool(getattr(settings, "MY_RESEARCH_ENABLED", False))


def pilot_emails() -> set[str]:
    raw = getattr(settings, "MY_RESEARCH_PILOT_EMAILS", "") or ""
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def is_eligible(user) -> bool:
    return is_internal_iitr_user(user)


def can_create_workspace(user) -> bool:
    if not (feature_enabled() and is_eligible(user)):
        return False
    pilot = pilot_emails()
    return not pilot or (user.email or "").strip().lower() in pilot


def eligible_users():
    return internal_iitr_users()


@dataclass
class WorkspaceAccess:
    workspace: ResearchWorkspace
    role: str

    @property
    def is_owner(self) -> bool:
        return self.role == MemberRole.OWNER

    @property
    def can_edit(self) -> bool:
        return self.is_owner and not self.workspace.is_archived


def accessible_workspace_ids(user):
    """Workspace ids the user owns or holds an active viewer membership for."""
    owned = ResearchWorkspace.objects.filter(owner=user).values_list("id", flat=True)
    shared = ResearchWorkspaceMember.objects.filter(
        user=user, role=MemberRole.VIEWER, revoked_at__isnull=True
    ).values_list("workspace_id", flat=True)
    return set(owned) | set(shared)


def resolve_access(user, workspace_id) -> WorkspaceAccess | None:
    """None when the workspace does not exist or the user has no access (callers answer 404)."""
    if not is_eligible(user):
        return None
    workspace = ResearchWorkspace.objects.select_related("owner", "owner__department").filter(pk=workspace_id).first()
    if workspace is None:
        return None
    if workspace.owner_id == user.pk:
        return WorkspaceAccess(workspace, MemberRole.OWNER)
    if ResearchWorkspaceMember.objects.filter(
        workspace=workspace, user=user, role=MemberRole.VIEWER, revoked_at__isnull=True
    ).exists():
        return WorkspaceAccess(workspace, MemberRole.VIEWER)
    return None

"""
Feature flag, eligibility and authorization for Research Groups.

Groups need both MY_RESEARCH_ENABLED and MY_RESEARCH_GROUPS_ENABLED. Every request re-checks the
portal's authoritative IITR eligibility (`is_eligible`), and only active faculty of an internal
department may create a group. Group roles are resolved from the database on each call; a caller
with no active membership gets None so the API can answer 404 without revealing the group exists.
Nothing here consults workspace membership: groups and workspaces are authorized independently.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

from iic_booking.users.models.user_type import UserType

from .access import feature_enabled, is_eligible
from .group_models import GroupMemberStatus, GroupRole, ResearchGroup, ResearchGroupMember

GROUPS_DISABLED_CODE = "my_research_groups_disabled"
MANAGER_ROLES = (GroupRole.OWNER, GroupRole.MANAGER)


def groups_enabled() -> bool:
    return feature_enabled() and bool(getattr(settings, "MY_RESEARCH_GROUPS_ENABLED", False))


def groups_pilot_emails() -> set[str]:
    raw = getattr(settings, "MY_RESEARCH_GROUPS_PILOT_EMAILS", "") or ""
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def is_group_faculty(user) -> bool:
    """Active faculty of an internal IITR department (is_eligible already enforces the department rule)."""
    return is_eligible(user) and getattr(user, "user_type", None) == UserType.FACULTY


def can_create_group(user) -> bool:
    if not (groups_enabled() and is_group_faculty(user)):
        return False
    pilot = groups_pilot_emails()
    return not pilot or (user.email or "").strip().lower() in pilot


@dataclass
class GroupAccess:
    group: ResearchGroup
    role: str
    membership: ResearchGroupMember | None

    @property
    def is_owner(self) -> bool:
        return self.role == GroupRole.OWNER

    @property
    def is_manager(self) -> bool:
        return self.role in MANAGER_ROLES

    @property
    def can_manage(self) -> bool:
        return self.is_manager and not self.group.is_archived


def active_membership(group_id, user) -> ResearchGroupMember | None:
    return (
        ResearchGroupMember.objects.select_related("category")
        .filter(group_id=group_id, user=user, status=GroupMemberStatus.ACTIVE)
        .first()
    )


def resolve_group_access(user, group_id) -> GroupAccess | None:
    if not is_eligible(user):
        return None
    group = ResearchGroup.objects.select_related("owner", "owner__department").filter(pk=group_id).first()
    if group is None:
        return None
    membership = active_membership(group.pk, user)
    if group.owner_id == user.pk:
        return GroupAccess(group, GroupRole.OWNER, membership)
    if membership is None:
        return None
    if membership.role == GroupRole.MANAGER and not is_group_faculty(user):
        # A manager who is no longer faculty keeps read access only.
        return GroupAccess(group, GroupRole.MEMBER, membership)
    return GroupAccess(group, membership.role, membership)


def visible_group_ids(user):
    owned = ResearchGroup.objects.filter(owner=user).values_list("id", flat=True)
    member_of = ResearchGroupMember.objects.filter(user=user, status=GroupMemberStatus.ACTIVE).values_list(
        "group_id", flat=True
    )
    return set(owned) | set(member_of)


def managed_group_ids(user):
    owned = set(ResearchGroup.objects.filter(owner=user).values_list("id", flat=True))
    if not is_group_faculty(user):
        return owned
    managed = ResearchGroupMember.objects.filter(
        user=user, status=GroupMemberStatus.ACTIVE, role=GroupRole.MANAGER
    ).values_list("group_id", flat=True)
    return owned | set(managed)

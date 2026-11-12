"""Operator, OIC and Lab In-charge tools on threads, and the equipment page's Discussions panel.

* Staff flags (pin, known problem, workaround, outdated, resolved) are set by moderators of the thread's community
  (OIC, Lab Operator / Lab In-charge, Group Moderator, admins). Each change is audited.
* Nothing here reads or writes equipment status, maintenance, downtime, slots or bookings: discussions are never
  treated as availability information.
* Equipment discussions are resolved through the portal's own equipment visibility first, then through the
  caller's Connect access. A caller who is not a member of the group sees that the community exists for members,
  never its threads.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.utils import timezone

from .. import access
from ..models import ArticleStatus
from ..models import Community
from ..models import CommunityKind
from ..models import DiscussionCategory
from ..models import DiscussionThread
from ..models import ModerationAction
from ..models import ThreadType
from ..models import Visibility
from .membership import group_ids_for_equipment
from .threads import NotAllowed

FLAG_FIELDS = ("is_pinned", "is_known_problem", "has_workaround", "is_outdated")
GENERAL_EQUIPMENT_CATEGORY = "general-instrumentation"


@transaction.atomic
def set_flags(user, thread: DiscussionThread, changes: dict, reason: str = "") -> DiscussionThread:
    thread = DiscussionThread.objects.select_for_update().get(pk=thread.pk)
    if not access.can_moderate(user, thread.community_id):
        raise NotAllowed("Only facility staff and moderators of this community can do this.")
    before, after, fields = {}, {}, []
    now = timezone.now()
    for name in FLAG_FIELDS:
        if name not in changes:
            continue
        value = bool(changes[name])
        if getattr(thread, name) == value:
            continue
        before[name], after[name] = getattr(thread, name), value
        setattr(thread, name, value)
        fields.append(name)
        if name == "is_pinned":
            thread.pinned_at = now if value else None
            thread.pinned_by = user if value else None
            fields += ["pinned_at", "pinned_by"]
    if "resolved" in changes:
        resolved = bool(changes["resolved"])
        if bool(thread.marked_resolved_at) != resolved:
            before["resolved"], after["resolved"] = bool(thread.marked_resolved_at), resolved
            thread.marked_resolved_at = now if resolved else None
            thread.marked_resolved_by = user if resolved else None
            fields += ["marked_resolved_at", "marked_resolved_by"]
    if not fields:
        return thread
    thread.save(update_fields=[*dict.fromkeys(fields), "updated_at"])
    ModerationAction.objects.create(
        actor=user,
        community_id=thread.community_id,
        action="staff_flags",
        target_type="thread",
        target_id=str(thread.pk),
        reason=(reason or "")[:1000],
        before=before,
        after=after,
    )
    return thread


def _visible_equipment(user, equipment_id: int):
    from iic_booking.equipment.api_views import get_visible_equipment_queryset

    return (
        get_visible_equipment_queryset(user, catalog_scope="all", mode_catalog_filter=False)
        .filter(pk=equipment_id)
        .select_related(None)
        .first()
    )


def equipment_context(user, equipment_id: int) -> dict:
    """Where this equipment's discussions live for the caller. Raises Http404 if the caller cannot see it."""
    equipment = _visible_equipment(user, equipment_id)
    if equipment is None or not access.user_enabled(user):
        raise Http404
    group_ids = group_ids_for_equipment([equipment.pk]) if (equipment.equipment_group_id or equipment.parent_equipment_id) else set()
    snap = access.snapshot(user)
    out = {"equipment": {"id": equipment.pk, "name": equipment.name, "code": equipment.code}}
    if group_ids:
        gid = next(iter(group_ids)) if len(group_ids) == 1 else (equipment.equipment_group_id or min(group_ids))
        community = Community.objects.filter(kind=CommunityKind.GROUP, equipment_group_ref_id=gid).first()
        if community is None:
            return {**out, "available": False, "reason": "not_set_up"}
        if community.pk not in snap.readable:
            return {**out, "available": False, "reason": "members_only", "community": {"name": community.name}}
        return {**out, "available": True, "community": community, "category": None}
    general = Community.objects.filter(kind=CommunityKind.GENERAL).first()
    if general is None or general.pk not in snap.readable:
        return {**out, "available": False, "reason": "internal_only"}
    category = DiscussionCategory.objects.filter(community=general, slug=GENERAL_EQUIPMENT_CATEGORY, is_active=True).first()
    return {**out, "available": True, "community": general, "category": category}


def equipment_threads(user, community: Community, equipment_id: int):
    return access.visible_threads(user).filter(community=community, equipment_ref_id=equipment_id, visibility=Visibility.VISIBLE)


def equipment_sections(user, community: Community, equipment_id: int) -> dict:
    """Querysets for each panel section (caller annotates and slices)."""
    base = equipment_threads(user, community, equipment_id)
    now = timezone.now()
    return {
        "recent": base.order_by("-is_pinned", "-last_activity_at"),
        "questions": base.filter(type__in=[ThreadType.QUESTION, ThreadType.TECHNICAL_ISSUE], solved_at__isnull=True).order_by("-last_activity_at"),
        "solutions": base.filter(Q(solved_at__isnull=False) | Q(type=ThreadType.SOLUTION)).order_by("-solved_at", "-last_activity_at"),
        "known_problems": base.filter(Q(is_known_problem=True) | Q(has_workaround=True)).order_by("-last_activity_at"),
        "announcements": base.filter(type=ThreadType.ANNOUNCEMENT)
        .filter(Q(announcement__isnull=True) | Q(announcement__expires_at__isnull=True) | Q(announcement__expires_at__gt=now))
        .order_by("-is_pinned", "-created_at"),
        "articles": access.visible_articles(user)
        .filter(community=community, equipment_ref_id=equipment_id, status=ArticleStatus.PUBLISHED)
        .order_by("-published_at"),
    }


def staff_badge_for_equipment(user, equipment_id: int | None) -> str:
    """Verified badge from the portal's assignments for one instrument (used in General IITR Discussions)."""
    if not equipment_id:
        return ""
    from iic_booking.equipment.models import EquipmentManager
    from iic_booking.equipment.models import EquipmentOperator
    from iic_booking.equipment.models import EquipmentTemporaryOIC

    if EquipmentManager.objects.filter(equipment_id=equipment_id, manager_id=user.pk).exists() or EquipmentTemporaryOIC.objects.active().filter(
        equipment_id=equipment_id, temporary_oic_id=user.pk
    ).exists():
        return access.BADGE_OIC
    if EquipmentOperator.objects.filter(equipment_id=equipment_id, operator_id=user.pk).exists():
        return access.BADGE_OPERATOR
    return ""

"""Community announcements: pinned notices from facility staff with priority, expiry, targeting and acknowledgement.

* Posted by moderators of the community (OIC, Lab Operator / Lab In-charge, Group Moderator, admins).
* An announcement is a thread of type Announcement plus a ``CommunityAnnouncement`` row.
* Audience: every reader of the community, or (``target_equipment``) the people who booked that instrument plus
  its staff. Targeting narrows notifications only; it never widens who can read the thread.
* Announcements never change bookings, slots, availability or equipment status.
"""

from __future__ import annotations

from django.db import IntegrityError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .. import access
from ..models import AnnouncementAcknowledgement
from ..models import AnnouncementPriority
from ..models import CommunityAnnouncement
from ..models import CommunityKind
from ..models import CommunityMembership
from ..models import DiscussionThread
from ..models import MembershipStatus
from ..models import ModerationAction
from ..models import ThreadType
from .threads import NotAllowed
from .threads import ThreadError


def is_active(announcement: CommunityAnnouncement, now=None) -> bool:
    now = now or timezone.now()
    return announcement.expires_at is None or announcement.expires_at > now


@transaction.atomic
def attach(user, thread: DiscussionThread, *, priority=None, expires_at=None, requires_acknowledgement=False, target_equipment_id=None, pin=False):
    """Create the announcement row for a new Announcement thread (called right after the thread is created)."""
    if thread.type != ThreadType.ANNOUNCEMENT:
        raise ThreadError("Only announcements have announcement settings.")
    if not access.can_moderate(user, thread.community_id):
        raise NotAllowed("Only facility staff and moderators can post announcements.")
    priority = priority or AnnouncementPriority.NORMAL
    if priority not in AnnouncementPriority.values:
        raise ThreadError("Choose a priority.", "priority")
    if expires_at is not None and expires_at <= timezone.now():
        raise ThreadError("The expiry must be in the future.", "expires_at")
    equipment = None
    if target_equipment_id:
        from .threads import resolve_equipment

        equipment = resolve_equipment(user, thread.community, target_equipment_id)
    announcement, _ = CommunityAnnouncement.objects.update_or_create(
        thread=thread,
        defaults={
            "priority": priority,
            "expires_at": expires_at,
            "requires_acknowledgement": bool(requires_acknowledgement),
            "target_equipment": equipment,
        },
    )
    if pin and not thread.is_pinned:
        DiscussionThread.objects.filter(pk=thread.pk).update(is_pinned=True, pinned_at=timezone.now(), pinned_by=user)
    ModerationAction.objects.create(
        actor=user,
        community_id=thread.community_id,
        action="announcement_publish",
        target_type="thread",
        target_id=str(thread.pk),
        after={
            "priority": priority,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "requires_acknowledgement": bool(requires_acknowledgement),
            "target_equipment_id": equipment.pk if equipment else None,
            "pinned": bool(pin),
        },
    )
    return announcement


def audience_user_ids(announcement: CommunityAnnouncement) -> list[int]:
    """Who should be notified (still re-checked per user at delivery)."""
    thread = announcement.thread
    community = thread.community
    if community.kind == CommunityKind.GENERAL:
        from iic_booking.equipment.results_sharing_service import internal_iitr_users
        from iic_booking.users.models import User

        staff = User.objects.filter(is_active=True, user_type__in=list(access.INTERNAL_STAFF_TYPES)).values_list("pk", flat=True)
        ids = set(internal_iitr_users().values_list("pk", flat=True)) | set(staff)
        blocked = set(
            CommunityMembership.objects.filter(community=community, status__in=[MembershipStatus.REVOKED, MembershipStatus.LAPSED]).values_list("user_id", flat=True)
        )
        ids -= blocked
    else:
        members = CommunityMembership.objects.filter(community=community, status__in=[MembershipStatus.ACTIVE, MembershipStatus.SUSPENDED])
        if announcement.target_equipment_id:
            from iic_booking.equipment.models import Booking
            from iic_booking.equipment.models import EquipmentManager
            from iic_booking.equipment.models import EquipmentOperator

            eid = announcement.target_equipment_id
            bookers = Booking.objects.filter(Q(equipment_id=eid) | Q(equipment__parent_equipment_id=eid)).values("user_id")
            staff = set(EquipmentManager.objects.filter(equipment_id=eid).values_list("manager_id", flat=True)) | set(
                EquipmentOperator.objects.filter(equipment_id=eid).values_list("operator_id", flat=True)
            )
            members = members.filter(Q(user_id__in=bookers) | Q(user_id__in=staff))
        ids = set(members.values_list("user_id", flat=True))
    ids.discard(thread.author_id)
    return sorted(ids)


@transaction.atomic
def acknowledge(user, thread: DiscussionThread) -> bool:
    announcement = CommunityAnnouncement.objects.filter(thread=thread).first()
    if announcement is None or not announcement.requires_acknowledgement:
        raise ThreadError("This announcement does not ask for acknowledgement.")
    try:
        with transaction.atomic():
            AnnouncementAcknowledgement.objects.create(announcement=announcement, user=user)
    except IntegrityError:
        pass
    return True


def payload(user, thread: DiscussionThread) -> dict | None:
    announcement = CommunityAnnouncement.objects.filter(thread=thread).select_related("target_equipment").first()
    if announcement is None:
        return None
    data = {
        "priority": announcement.priority,
        "expires_at": announcement.expires_at,
        "is_expired": not is_active(announcement),
        "requires_acknowledgement": announcement.requires_acknowledgement,
        "target_equipment": (
            {"id": announcement.target_equipment.pk, "name": announcement.target_equipment.name}
            if announcement.target_equipment_id
            else None
        ),
        "acknowledged": announcement.acknowledgements.filter(user=user).exists(),
    }
    if access.can_moderate(user, thread.community_id):
        data["acknowledged_count"] = announcement.acknowledgements.count()
        data["audience_count"] = len(audience_user_ids(announcement))
    return data


def acknowledgement_list(user, thread: DiscussionThread) -> list[dict]:
    if not access.can_moderate(user, thread.community_id):
        raise NotAllowed("Only facility staff can see who acknowledged.")
    announcement = CommunityAnnouncement.objects.filter(thread=thread).first()
    if announcement is None:
        return []
    return [
        {"user_id": a.user_id, "name": (a.user.name or "").strip() or "Portal user", "acknowledged_at": a.acknowledged_at}
        for a in announcement.acknowledgements.select_related("user").order_by("-acknowledged_at")[:500]
    ]

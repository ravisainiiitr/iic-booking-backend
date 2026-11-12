"""Single source of truth for IITR Connect access.

Every view, search, count, attachment download, notification and analytics query must start from the
querysets / id sets in this module. Rules:

* Connect is on only when ``IITR_CONNECT_ENABLED`` is true and, if ``IITR_CONNECT_PILOT_EMAILS`` is set, the user
  is listed. Anonymous and inactive users never have access.
* General IITR Discussions: internal users only (active IITR students, faculty of an internal department, and
  IITR portal staff). External users (industry, R&D, institutes, startups, other, organisation admins, faculty
  of an external department) never see it.
* Equipment Group communities: active (or posting-suspended) members only. Membership comes from bookings and
  staff assignments (``services.membership``) or a manual add. External bookers are members of the groups they
  booked, and of nothing else.
* Test-only communities: test accounts and the Portal Admin only.
* Portal Admin (Main Administrator) and Community Admins read and moderate everything (test-only: Portal Admin
  and test accounts only).
* Lookups of objects the user may not read raise ``Http404`` (never 403), so existence is not revealed.

Results are memoised on the user object, which DRF creates once per request.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field

from django.conf import settings
from django.db.models import Q
from django.db.models import QuerySet
from django.http import Http404

from iic_booking.users.models.user_type import UserType

from .models import READ_STATUSES
from .models import ArticleStatus
from .models import Community
from .models import CommunityKind
from .models import CommunityMembership
from .models import CommunityStatus
from .models import ConnectRole
from .models import ConnectRoleAssignment
from .models import DiscussionPost
from .models import DiscussionThread
from .models import KnowledgeArticle
from .models import MembershipStatus
from .models import Visibility

DISABLED_CODE = "connect_disabled"
_CACHE_ATTR = "_connect_access_cache"

# IITR staff accounts (in addition to is_internal_iitr_user). org_admin is an external organisation's admin.
INTERNAL_STAFF_TYPES = frozenset(
    {
        UserType.ADMIN,
        UserType.DEPT_ADMIN,
        UserType.MANAGER,
        UserType.OPERATOR,
        UserType.FINANCE,
        UserType.EXTERNAL_RELATIONS,
        UserType.HOD,
        UserType.OC_STORES,
    }
)

BADGE_PORTAL_ADMIN = "Portal Admin"
BADGE_COMMUNITY_ADMIN = "Community Admin"
BADGE_OIC = "Officer-in-Charge"
BADGE_OPERATOR = "Lab Operator (Lab In-charge)"
BADGE_MODERATOR = "Group Moderator"


# ---------------------------------------------------------------------------
# Flag and identity
# ---------------------------------------------------------------------------


def feature_enabled() -> bool:
    return bool(getattr(settings, "IITR_CONNECT_ENABLED", False))


def pilot_emails() -> set[str]:
    raw = getattr(settings, "IITR_CONNECT_PILOT_EMAILS", "") or ""
    return {e.strip().lower() for e in str(raw).split(",") if e.strip()}


def _authenticated(user) -> bool:
    return bool(user is not None and getattr(user, "is_authenticated", False) and getattr(user, "is_active", False))


def user_enabled(user) -> bool:
    """Connect is switched on for this signed-in, active user."""
    if not feature_enabled() or not _authenticated(user):
        return False
    pilot = pilot_emails()
    return not pilot or (getattr(user, "email", "") or "").strip().lower() in pilot


def is_portal_admin(user) -> bool:
    if not _authenticated(user):
        return False
    return bool(getattr(user, "is_staff", False)) or getattr(user, "user_type", None) == UserType.ADMIN


def is_test_account(user) -> bool:
    from iic_booking.users.test_accounts import is_test_user

    try:
        return bool(is_test_user(user))
    except Exception:  # noqa: BLE001 - a broken flag lookup must never grant access
        return False


def is_internal_user(user) -> bool:
    """IITR internal: students, faculty of an internal department, and IITR portal staff."""
    if not _authenticated(user):
        return False
    if getattr(user, "user_type", None) in INTERNAL_STAFF_TYPES or is_portal_admin(user):
        return True
    from iic_booking.equipment.results_sharing_service import is_internal_iitr_user

    return is_internal_iitr_user(user)


# ---------------------------------------------------------------------------
# Per-request access snapshot
# ---------------------------------------------------------------------------


@dataclass
class AccessSnapshot:
    enabled: bool = False
    portal_admin: bool = False
    community_admin: bool = False
    internal: bool = False
    test_account: bool = False
    readable: frozenset[int] = frozenset()
    postable: frozenset[int] = frozenset()
    moderated: frozenset[int] = frozenset()
    general_id: int | None = None
    memberships: dict[int, CommunityMembership] = field(default_factory=dict)

    @property
    def is_admin(self) -> bool:
        return self.portal_admin or self.community_admin


def _general_community_id() -> int | None:
    return Community.objects.filter(kind=CommunityKind.GENERAL).values_list("id", flat=True).first()


def _build_snapshot(user) -> AccessSnapshot:
    if not user_enabled(user):
        return AccessSnapshot()
    snap = AccessSnapshot(enabled=True)
    snap.portal_admin = is_portal_admin(user)
    snap.community_admin = snap.portal_admin or ConnectRoleAssignment.objects.filter(
        user=user, role=ConnectRole.COMMUNITY_ADMIN, revoked_at__isnull=True
    ).exists()
    snap.internal = is_internal_user(user)
    snap.test_account = is_test_account(user)
    snap.general_id = _general_community_id()

    communities = {
        c["id"]: c for c in Community.objects.values("id", "kind", "status", "is_test_only")
    }
    snap.memberships = {
        m.community_id: m for m in CommunityMembership.objects.filter(user=user).select_related(None)
    }

    def test_ok(c) -> bool:
        return not c["is_test_only"] or snap.portal_admin or snap.test_account

    readable: set[int] = set()
    postable: set[int] = set()
    moderated: set[int] = set()
    for cid, c in communities.items():
        if not test_ok(c):
            continue
        membership = snap.memberships.get(cid)
        active = c["status"] == CommunityStatus.ACTIVE
        if snap.community_admin:
            readable.add(cid)
            moderated.add(cid)
            if active:
                postable.add(cid)
            continue
        if c["kind"] == CommunityKind.GENERAL:
            if not snap.internal:
                continue
            if membership is not None and membership.status in (MembershipStatus.REVOKED, MembershipStatus.LAPSED):
                continue
            readable.add(cid)
            suspended = membership is not None and membership.status == MembershipStatus.SUSPENDED
            if active and not suspended:
                postable.add(cid)
            if membership is not None and membership.moderator_override is True and membership.status == MembershipStatus.ACTIVE:
                moderated.add(cid)
            continue
        if membership is None or membership.status not in READ_STATUSES:
            continue
        readable.add(cid)
        if active and membership.status == MembershipStatus.ACTIVE:
            postable.add(cid)
        if membership.status == MembershipStatus.ACTIVE and membership.role == "moderator":
            moderated.add(cid)
    snap.readable = frozenset(readable)
    snap.postable = frozenset(postable)
    snap.moderated = frozenset(moderated)
    return snap


def snapshot(user) -> AccessSnapshot:
    cached = getattr(user, _CACHE_ATTR, None) if user is not None else None
    if isinstance(cached, AccessSnapshot):
        return cached
    snap = _build_snapshot(user)
    if user is not None:
        try:
            setattr(user, _CACHE_ATTR, snap)
        except (AttributeError, TypeError):
            pass
    return snap


def invalidate(user) -> None:
    if user is not None and hasattr(user, _CACHE_ATTR):
        try:
            delattr(user, _CACHE_ATTR)
        except AttributeError:
            pass


def accessible_community_ids(user) -> frozenset[int]:
    return snapshot(user).readable


def can_read_community(user, community_id: int) -> bool:
    return community_id in snapshot(user).readable


def can_post_in(user, community_id: int) -> bool:
    return community_id in snapshot(user).postable


def can_moderate(user, community_id: int) -> bool:
    return community_id in snapshot(user).moderated


# ---------------------------------------------------------------------------
# Visible querysets (start every query here)
# ---------------------------------------------------------------------------


def visible_communities(user) -> QuerySet:
    return Community.objects.filter(id__in=accessible_community_ids(user))


def visible_threads(user) -> QuerySet:
    snap = snapshot(user)
    if not snap.readable:
        return DiscussionThread.objects.none()
    q = Q(community_id__in=snap.readable, visibility=Visibility.VISIBLE)
    if snap.moderated:
        q |= Q(community_id__in=snap.moderated)
    # Authors still see their own thread while a moderator has it hidden (not removed or deleted).
    q |= Q(community_id__in=snap.readable, author_id=user.pk, visibility=Visibility.HIDDEN)
    return DiscussionThread.objects.filter(q)


def visible_posts(user) -> QuerySet:
    snap = snapshot(user)
    if not snap.readable:
        return DiscussionPost.objects.none()
    threads = visible_threads(user).values("id")
    q = Q(visibility=Visibility.VISIBLE)
    if snap.moderated:
        q |= Q(thread__community_id__in=snap.moderated)
    q |= Q(author_id=user.pk, visibility=Visibility.HIDDEN)
    return DiscussionPost.objects.filter(thread_id__in=threads).filter(q)


def visible_articles(user) -> QuerySet:
    snap = snapshot(user)
    if not snap.readable:
        return KnowledgeArticle.objects.none()
    q = Q(community_id__in=snap.readable, status=ArticleStatus.PUBLISHED)
    q |= Q(community_id__in=snap.readable, author_id=user.pk)
    if snap.moderated:
        q |= Q(community_id__in=snap.moderated)
    return KnowledgeArticle.objects.filter(q)


def get_thread_or_404(user, pk) -> DiscussionThread:
    thread = visible_threads(user).select_related("community", "category", "author").filter(pk=pk).first()
    if thread is None:
        raise Http404
    return thread


def get_community_or_404(user, *, pk=None, slug=None) -> Community:
    qs = visible_communities(user)
    community = qs.filter(pk=pk).first() if pk is not None else qs.filter(slug=slug).first()
    if community is None:
        raise Http404
    return community


# ---------------------------------------------------------------------------
# Verified role badges (from assignments, never self-chosen)
# ---------------------------------------------------------------------------


def role_badge(user, community_id: int) -> str:
    snap = snapshot(user)
    if snap.portal_admin:
        return BADGE_PORTAL_ADMIN
    if snap.community_admin:
        return BADGE_COMMUNITY_ADMIN
    membership = snap.memberships.get(community_id)
    if membership is None or membership.status != MembershipStatus.ACTIVE:
        return ""
    if membership.via_oic:
        return BADGE_OIC
    if membership.via_operator:
        return BADGE_OPERATOR
    if membership.moderator_override is True:
        return BADGE_MODERATOR
    return ""


def capabilities(user) -> dict:
    snap = snapshot(user)
    return {
        "enabled": snap.enabled,
        "is_portal_admin": snap.portal_admin,
        "is_community_admin": snap.community_admin,
        "is_internal": snap.internal,
        "can_use_general": bool(snap.general_id and snap.general_id in snap.readable),
        "community_count": len(snap.readable),
        "moderates_any": bool(snap.moderated),
    }

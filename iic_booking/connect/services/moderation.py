"""Moderation on the portal's roles: reports, hide / remove / restore, lock, move, queue, audit and Community Admins.

Who moderates (``access.can_moderate``): Portal Admin, Community Admins, and in each group community its Officers-in-
Charge, Lab Operators (Lab In-charge) and appointed Group Moderators. General IITR Discussions: Portal / Community
Admins and appointed moderators.

* Every action writes ``ModerationAction`` (append-only) with before / after state and a reason, and tells the
  author (``notifications.moderation_notice``).
* Reports: readers report others' posts once each. Three distinct open reports hide a post automatically until a
  moderator reviews it (spam / abuse brake that no single user can trigger).
* Moving a thread to another community can widen who can read it, so only Portal / Community Admins may do it and
  they must confirm explicitly. Moderators may change the category within the community.
* Nothing here changes bookings, equipment or availability.
"""

from __future__ import annotations

from django.db import IntegrityError
from django.db import transaction
from django.db.models import Count
from django.db.models import Q
from django.utils import timezone

from .. import access
from ..models import Community
from ..models import ConnectRole
from ..models import ConnectRoleAssignment
from ..models import DiscussionCategory
from ..models import DiscussionPost
from ..models import DiscussionReport
from ..models import DiscussionThread
from ..models import ModerationAction
from ..models import ReportReason
from ..models import ReportStatus
from ..models import ThreadStatus
from ..models import Visibility
from .threads import NotAllowed
from .threads import ThreadError
from .threads import refresh_counters

AUTO_HIDE_REPORTS = 3
POST_ACTIONS = ("hide", "remove", "restore")


def _audit(actor, community_id, action, target_type, target_id, *, before=None, after=None, reason=""):
    ModerationAction.objects.create(
        actor=actor,
        community_id=community_id,
        action=action,
        target_type=target_type,
        target_id=str(target_id),
        reason=(reason or "")[:1000],
        before=before or {},
        after=after or {},
    )


def _notify(post, actor, action):
    try:
        from . import notifications

        notifications.moderation_notice(post, actor, action)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


@transaction.atomic
def report(user, post: DiscussionPost, reason: str, details: str = "") -> DiscussionReport:
    if reason not in ReportReason.values:
        raise ThreadError("Choose why you are reporting this post.", "reason")
    if post.author_id == user.pk:
        raise ThreadError("You cannot report your own post.")
    if post.visibility != Visibility.VISIBLE:
        raise ThreadError("This post is already under review.")
    try:
        with transaction.atomic():
            row = DiscussionReport.objects.create(post=post, reporter=user, reason=reason, details=(details or "").strip()[:1000])
    except IntegrityError as exc:
        raise ThreadError("You already reported this post. A moderator will review it.") from exc
    open_reporters = DiscussionReport.objects.filter(post=post, status=ReportStatus.OPEN).values("reporter").distinct().count()
    if open_reporters >= AUTO_HIDE_REPORTS:
        _set_visibility(post, Visibility.HIDDEN)
        _audit(
            user, post.thread.community_id, "auto_hide", "post", post.pk,
            before={"visibility": Visibility.VISIBLE}, after={"visibility": Visibility.HIDDEN, "open_reports": open_reporters},
            reason="Hidden automatically after several reports, pending moderator review",
        )
    return row


def _set_visibility(post: DiscussionPost, visibility: str) -> None:
    from .solutions import clear_if_post_gone

    DiscussionPost.objects.filter(pk=post.pk).update(visibility=visibility)
    post.visibility = visibility
    if post.is_opening:
        DiscussionThread.objects.filter(pk=post.thread_id).update(visibility=visibility)
    if visibility != Visibility.VISIBLE:
        clear_if_post_gone(post)
    refresh_counters(post.thread_id)


@transaction.atomic
def moderate_post(actor, post: DiscussionPost, action: str, reason: str = "") -> DiscussionPost:
    if action not in POST_ACTIONS:
        raise ThreadError("Unknown moderation action.")
    post = DiscussionPost.objects.select_for_update().select_related("thread").get(pk=post.pk)
    community_id = post.thread.community_id
    if not access.can_moderate(actor, community_id):
        raise NotAllowed("Only moderators of this community can do this.")
    if action in ("hide", "remove") and not (reason or "").strip():
        raise ThreadError("Give a reason; it is recorded and shown to the author.", "reason")
    before = post.visibility
    target = {"hide": Visibility.HIDDEN, "remove": Visibility.REMOVED, "restore": Visibility.VISIBLE}[action]
    if action == "restore" and before == Visibility.DELETED:
        raise ThreadError("The author deleted this post; it cannot be restored.")
    if before == target:
        return post
    _set_visibility(post, target)
    DiscussionReport.objects.filter(post=post, status=ReportStatus.OPEN).update(
        status=ReportStatus.ACTIONED, handled_by=actor, handled_at=timezone.now(), resolution_note=f"{action}: {reason}"[:1000]
    )
    _audit(actor, community_id, f"post_{action}", "thread" if post.is_opening else "post", post.thread_id if post.is_opening else post.pk,
           before={"visibility": before}, after={"visibility": target}, reason=reason)
    _notify(post, actor, action)
    return post


@transaction.atomic
def dismiss_reports(actor, post: DiscussionPost, note: str = "") -> int:
    if not access.can_moderate(actor, post.thread.community_id):
        raise NotAllowed("Only moderators of this community can do this.")
    n = DiscussionReport.objects.filter(post=post, status=ReportStatus.OPEN).update(
        status=ReportStatus.DISMISSED, handled_by=actor, handled_at=timezone.now(), resolution_note=(note or "")[:1000]
    )
    if n:
        _audit(actor, post.thread.community_id, "reports_dismiss", "post", post.pk, after={"dismissed": n}, reason=note)
    return n


# ---------------------------------------------------------------------------
# Threads: lock, move
# ---------------------------------------------------------------------------


@transaction.atomic
def set_lock(actor, thread: DiscussionThread, locked: bool, reason: str = "") -> DiscussionThread:
    thread = DiscussionThread.objects.select_for_update().get(pk=thread.pk)
    if not access.can_moderate(actor, thread.community_id):
        raise NotAllowed("Only moderators of this community can do this.")
    target = ThreadStatus.LOCKED if locked else ThreadStatus.OPEN
    if thread.status != target:
        before = thread.status
        thread.status = target
        thread.save(update_fields=["status", "updated_at"])
        _audit(actor, thread.community_id, "thread_lock" if locked else "thread_unlock", "thread", thread.pk,
               before={"status": before}, after={"status": target}, reason=reason)
    return thread


@transaction.atomic
def move(actor, thread: DiscussionThread, *, category_id=None, community_id=None, confirm_widen=False, reason="") -> DiscussionThread:
    thread = DiscussionThread.objects.select_for_update().get(pk=thread.pk)
    snap = access.snapshot(actor)
    if not access.can_moderate(actor, thread.community_id):
        raise NotAllowed("Only moderators of this community can do this.")
    before = {"community_id": thread.community_id, "category_id": thread.category_id}
    if community_id and int(community_id) != thread.community_id:
        if not snap.is_admin:
            raise NotAllowed("Only a Community Admin can move a discussion to another community.")
        target = Community.objects.filter(pk=community_id, status="active").first()
        if target is None or target.pk not in snap.readable:
            raise ThreadError("Choose an active community.", "community")
        if not confirm_widen:
            raise ThreadError(
                "Moving changes who can read this discussion and its attachments. Confirm to continue.", "confirm_widen"
            )
        thread.community = target
        thread.category = None
        if thread.equipment_ref_id and target.kind == "group":
            from .membership import group_ids_for_equipment

            if target.equipment_group_ref_id not in group_ids_for_equipment([thread.equipment_ref_id]):
                thread.equipment = None
                thread.equipment_ref_id = None
        from ..models import DiscussionAttachment

        DiscussionAttachment.objects.filter(post__thread=thread).update(community=target)
    if category_id is not None:
        if category_id == "":
            thread.category = None
        else:
            category = DiscussionCategory.objects.filter(pk=category_id, community_id=thread.community_id, is_active=True).first()
            if category is None:
                raise ThreadError("Choose a category of this community.", "category")
            thread.category = category
    thread.save()
    after = {"community_id": thread.community_id, "category_id": thread.category_id}
    if after != before:
        _audit(actor, before["community_id"], "thread_move", "thread", thread.pk, before=before, after=after, reason=reason)
    return thread


# ---------------------------------------------------------------------------
# Queue and dashboard
# ---------------------------------------------------------------------------


def moderated_ids(user):
    return access.snapshot(user).moderated


def queue(user, community_id=None):
    """Posts with open reports, plus hidden posts awaiting review, in communities the caller moderates."""
    ids = moderated_ids(user)
    if community_id:
        ids = {int(community_id)} & set(ids)
    reported = (
        DiscussionPost.objects.filter(thread__community_id__in=ids, reports__status=ReportStatus.OPEN)
        .annotate(open_reports=Count("reports", filter=Q(reports__status=ReportStatus.OPEN), distinct=True))
        .select_related("thread", "thread__community", "author")
        .order_by("-open_reports", "-created_at")
        .distinct()
    )
    hidden = (
        DiscussionPost.objects.filter(thread__community_id__in=ids, visibility=Visibility.HIDDEN)
        .select_related("thread", "thread__community", "author")
        .order_by("-created_at")
    )
    return reported, hidden


def dashboard(user) -> dict:
    ids = list(moderated_ids(user))
    reports_open = dict(
        DiscussionReport.objects.filter(post__thread__community_id__in=ids, status=ReportStatus.OPEN)
        .values("post__thread__community_id")
        .annotate(n=Count("pk"))
        .values_list("post__thread__community_id", "n")
    )
    hidden = dict(
        DiscussionPost.objects.filter(thread__community_id__in=ids, visibility=Visibility.HIDDEN)
        .values("thread__community_id")
        .annotate(n=Count("pk"))
        .values_list("thread__community_id", "n")
    )
    locked = dict(
        DiscussionThread.objects.filter(community_id__in=ids, status=ThreadStatus.LOCKED)
        .values("community_id")
        .annotate(n=Count("pk"))
        .values_list("community_id", "n")
    )
    names = dict(Community.objects.filter(pk__in=ids).values_list("pk", "name"))
    rows = [
        {"community_id": cid, "name": names.get(cid, ""), "open_reports": reports_open.get(cid, 0), "hidden_posts": hidden.get(cid, 0), "locked_threads": locked.get(cid, 0)}
        for cid in ids
        if reports_open.get(cid) or hidden.get(cid) or locked.get(cid)
    ]
    rows.sort(key=lambda r: (-r["open_reports"], -r["hidden_posts"], r["name"]))
    return {
        "totals": {
            "open_reports": sum(reports_open.values()),
            "hidden_posts": sum(hidden.values()),
            "locked_threads": sum(locked.values()),
            "communities_moderated": len(ids),
        },
        "communities": rows,
    }


# ---------------------------------------------------------------------------
# Community Admins (granted by the Portal Admin only)
# ---------------------------------------------------------------------------


@transaction.atomic
def grant_community_admin(portal_admin, user) -> ConnectRoleAssignment:
    if not access.is_portal_admin(portal_admin):
        raise NotAllowed("Only the Portal Admin can appoint Community Admins.")
    if user.pk == portal_admin.pk:
        raise ThreadError("You already have full access.")
    if not access.is_internal_user(user):
        raise ThreadError("Community Admins must be IIT Roorkee users.")
    row = ConnectRoleAssignment.objects.filter(user=user, role=ConnectRole.COMMUNITY_ADMIN, revoked_at__isnull=True).first()
    if row:
        return row
    row = ConnectRoleAssignment.objects.create(user=user, role=ConnectRole.COMMUNITY_ADMIN, granted_by=portal_admin)
    _audit(portal_admin, None, "community_admin_grant", "user", user.pk)
    access.invalidate(user)
    return row


@transaction.atomic
def revoke_community_admin(portal_admin, user) -> int:
    if not access.is_portal_admin(portal_admin):
        raise NotAllowed("Only the Portal Admin can remove Community Admins.")
    n = ConnectRoleAssignment.objects.filter(user=user, role=ConnectRole.COMMUNITY_ADMIN, revoked_at__isnull=True).update(revoked_at=timezone.now())
    if n:
        _audit(portal_admin, None, "community_admin_revoke", "user", user.pk)
    access.invalidate(user)
    return n

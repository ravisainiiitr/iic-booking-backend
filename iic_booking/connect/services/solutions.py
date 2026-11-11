"""Accepted solutions for Question and Technical Issue threads.

* The thread's author or a moderator of its community (Officer-in-Charge, Lab Operator, Group Moderator, admins)
  may accept, replace or revoke the accepted answer. Every change is recorded in ModerationAction with the
  previous and new post.
* Only a visible reply of the same thread can be accepted (never the opening post, a hidden or a deleted post).
* ``solved_at`` is the time the current answer was accepted; resolution time = solved_at - created_at.
* Accepting an answer never changes equipment status, bookings or availability.
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from .. import access
from ..models import AcceptedSolution
from ..models import DiscussionPost
from ..models import DiscussionThread
from ..models import ModerationAction
from ..models import ThreadStatus
from ..models import Visibility
from .threads import NotAllowed
from .threads import ThreadError


def can_accept(user, thread: DiscussionThread) -> bool:
    if not thread.is_solvable or thread.visibility != Visibility.VISIBLE:
        return False
    if access.can_moderate(user, thread.community_id):
        return True
    if thread.author_id != user.pk or not access.can_post_in(user, thread.community_id):
        return False
    return thread.status != ThreadStatus.LOCKED


def _post_summary(post_id):
    return {"post_id": post_id} if post_id else {}


@transaction.atomic
def accept(user, thread: DiscussionThread, post_id) -> DiscussionThread:
    thread = DiscussionThread.objects.select_for_update().get(pk=thread.pk)
    if not thread.is_solvable:
        raise ThreadError("Only questions and technical issues can have an accepted solution.")
    if not can_accept(user, thread):
        raise NotAllowed("Only the person who asked or a moderator can accept an answer.")
    post = DiscussionPost.objects.filter(pk=post_id, thread=thread, is_opening=False, visibility=Visibility.VISIBLE).first()
    if post is None:
        raise ThreadError("Choose a visible reply in this discussion.", "post")
    previous = thread.accepted_post_id
    if previous == post.pk:
        return thread
    now = timezone.now()
    AcceptedSolution.objects.update_or_create(thread=thread, defaults={"post": post, "accepted_by": user, "accepted_at": now})
    DiscussionThread.objects.filter(pk=thread.pk).update(accepted_post=post, solved_at=now)
    ModerationAction.objects.create(
        actor=user,
        community=thread.community,
        action="solution_replace" if previous else "solution_accept",
        target_type="thread",
        target_id=str(thread.pk),
        before=_post_summary(previous),
        after={"post_id": post.pk, "by_author": thread.author_id == user.pk},
    )
    thread.refresh_from_db()
    _notify(thread, post, user)
    return thread


@transaction.atomic
def revoke(user, thread: DiscussionThread, reason: str = "") -> DiscussionThread:
    thread = DiscussionThread.objects.select_for_update().get(pk=thread.pk)
    if not thread.accepted_post_id:
        raise ThreadError("This discussion has no accepted solution.")
    if not can_accept(user, thread):
        raise NotAllowed("Only the person who asked or a moderator can change the accepted answer.")
    previous = thread.accepted_post_id
    AcceptedSolution.objects.filter(thread=thread).delete()
    DiscussionThread.objects.filter(pk=thread.pk).update(accepted_post=None, solved_at=None)
    ModerationAction.objects.create(
        actor=user,
        community=thread.community,
        action="solution_revoke",
        target_type="thread",
        target_id=str(thread.pk),
        reason=(reason or "")[:1000],
        before=_post_summary(previous),
    )
    thread.refresh_from_db()
    return thread


def clear_if_post_gone(post: DiscussionPost) -> None:
    """When an accepted reply is hidden, removed or deleted, the thread is no longer solved by it."""
    thread = post.thread
    if thread.accepted_post_id != post.pk or post.visibility == Visibility.VISIBLE:
        return
    AcceptedSolution.objects.filter(thread=thread).delete()
    DiscussionThread.objects.filter(pk=thread.pk).update(accepted_post=None, solved_at=None)


def solution_payload(user, thread: DiscussionThread) -> dict | None:
    if not thread.accepted_post_id:
        return None
    row = AcceptedSolution.objects.select_related("post__author", "accepted_by").filter(thread=thread).first()
    if row is None or not access.visible_posts(user).filter(pk=row.post_id).exists():
        return None
    hours = round((row.accepted_at - thread.created_at).total_seconds() / 3600, 1)
    return {
        "post_id": row.post_id,
        "solver": {"id": row.post.author_id, "name": (row.post.author.name or "").strip() or "Portal user", "badge": row.post.author_badge},
        "accepted_by": (row.accepted_by.name or "").strip() or "Portal user",
        "accepted_by_author": row.accepted_by_id == thread.author_id,
        "accepted_at": row.accepted_at,
        "resolution_hours": hours,
    }


def _notify(thread, post, actor) -> None:
    try:
        from . import notifications
    except ImportError:
        return
    try:
        notifications.solution_accepted(thread, post, actor)
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning("IITR Connect: accepted-answer notification failed", exc_info=True)

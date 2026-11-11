"""Response shapes for threads and posts. Querysets are annotated up front so lists cost a fixed number of queries."""

from __future__ import annotations

from collections import defaultdict

from django.db.models import Exists
from django.db.models import OuterRef
from django.db.models import Prefetch
from django.db.models import Subquery
from django.db.models.functions import Substr

from .. import access
from ..models import DiscussionAttachment
from ..models import DiscussionBookmark
from ..models import DiscussionPost
from ..models import DiscussionReaction
from ..models import DiscussionSubscription
from ..models import DiscussionThread
from ..models import SubscriptionLevel
from ..models import ThreadReadState
from ..models import ThreadTag
from ..models import Visibility
from ..services import threads as thread_service

EXCERPT_CHARS = 240


def user_label(user) -> str:
    return (getattr(user, "name", "") or "").strip() or "Portal user"


def annotate_cards(qs, user):
    opening = DiscussionPost.objects.filter(thread_id=OuterRef("pk"), is_opening=True)
    return (
        qs.select_related("community", "category", "author", "equipment")
        .prefetch_related(Prefetch("thread_tags", queryset=ThreadTag.objects.select_related("tag")))
        .annotate(
            _excerpt=Subquery(opening.annotate(x=Substr("body_text", 1, EXCERPT_CHARS + 20)).values("x")[:1]),
            _badge=Subquery(opening.values("author_badge")[:1]),
            _bookmarked=Exists(DiscussionBookmark.objects.filter(user=user, thread_id=OuterRef("pk"))),
            _following=Exists(
                DiscussionSubscription.objects.filter(user=user, thread_id=OuterRef("pk"), level=SubscriptionLevel.WATCHING)
            ),
            _last_viewed=Subquery(ThreadReadState.objects.filter(user=user, thread_id=OuterRef("pk")).values("last_viewed_at")[:1]),
        )
    )


def _excerpt(text: str | None) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= EXCERPT_CHARS else text[:EXCERPT_CHARS].rsplit(" ", 1)[0] + "…"


def equipment_ref(thread) -> dict | None:
    if thread.equipment_id and thread.equipment is not None:
        return {"id": thread.equipment.equipment_id, "name": thread.equipment.name, "code": thread.equipment.code}
    if thread.equipment_ref_id:
        return {"id": thread.equipment_ref_id, "name": "", "code": ""}
    return None


def thread_card(thread: DiscussionThread) -> dict:
    last_viewed = getattr(thread, "_last_viewed", None)
    return {
        "id": thread.pk,
        "title": thread.title,
        "type": thread.type,
        "status": thread.status,
        "visibility": thread.visibility,
        "community": {
            "id": thread.community_id,
            "slug": thread.community.slug,
            "name": thread.community.name,
            "kind": thread.community.kind,
        },
        "category": (
            {"id": thread.category_id, "slug": thread.category.slug, "name": thread.category.name} if thread.category_id else None
        ),
        "equipment": equipment_ref(thread),
        "author": {"id": thread.author_id, "name": user_label(thread.author), "badge": getattr(thread, "_badge", "") or ""},
        "tags": [{"slug": tt.tag.slug, "name": tt.tag.name} for tt in thread.thread_tags.all()],
        "excerpt": _excerpt(getattr(thread, "_excerpt", "")),
        "created_at": thread.created_at,
        "last_activity_at": thread.last_activity_at,
        "reply_count": thread.reply_count,
        "view_count": thread.view_count,
        "helpful_count": thread.helpful_count,
        "is_pinned": thread.is_pinned,
        "is_solvable": thread.is_solvable,
        "is_solved": bool(thread.solved_at),
        "solved_at": thread.solved_at,
        "is_resolved": bool(thread.solved_at and thread.resolved_by_author_at),
        "is_known_problem": thread.is_known_problem,
        "has_workaround": thread.has_workaround,
        "is_outdated": thread.is_outdated,
        "marked_resolved_at": thread.marked_resolved_at,
        "bookmarked": bool(getattr(thread, "_bookmarked", False)),
        "following": bool(getattr(thread, "_following", False)),
        "unread": last_viewed is None or last_viewed < thread.last_activity_at,
    }


def attachment_payload(a: DiscussionAttachment) -> dict:
    return {
        "id": str(a.pk),
        "name": a.original_name,
        "size": a.size,
        "kind": a.sniffed_kind,
        "is_inline_image": a.is_inline_image,
        "download_path": f"/api/v1/connect/attachments/{a.pk}/",
    }


def posts_payload(posts: list[DiscussionPost], user, thread: DiscussionThread) -> list[dict]:
    """Posts with reaction counts, the caller's reactions and attachments in three queries."""
    ids = [p.pk for p in posts]
    counts: dict[int, dict[str, int]] = defaultdict(dict)
    mine: dict[int, list[str]] = defaultdict(list)
    for post_id, kind, uid in DiscussionReaction.objects.filter(post_id__in=ids).values_list("post_id", "kind", "user_id"):
        counts[post_id][kind] = counts[post_id].get(kind, 0) + 1
        if uid == user.pk:
            mine[post_id].append(kind)
    attachments: dict[int, list[dict]] = defaultdict(list)
    for a in DiscussionAttachment.objects.filter(post_id__in=ids).order_by("created_at"):
        attachments[a.post_id].append(attachment_payload(a))
    moderator = access.can_moderate(user, thread.community_id)
    with_replies = set(
        DiscussionPost.objects.filter(parent_id__in=ids, visibility=Visibility.VISIBLE).values_list("parent_id", flat=True)
    )
    for p in posts:
        p.thread = thread  # avoid one query per post in the permission checks
    out = []
    for p in posts:
        visible = p.visibility == Visibility.VISIBLE
        out.append(
            {
                "id": p.pk,
                "is_opening": p.is_opening,
                "parent_id": p.parent_id,
                "author": {"id": p.author_id, "name": user_label(p.author), "badge": p.author_badge},
                "is_verified_response": p.is_verified_response,
                "body_html": p.body_html if (visible or moderator or p.author_id == user.pk) else "",
                "visibility": p.visibility,
                "created_at": p.created_at,
                "edited_at": p.edited_at,
                "edit_count": p.edit_count,
                "helpful_count": p.helpful_count,
                "reactions": counts.get(p.pk, {}),
                "my_reactions": mine.get(p.pk, []),
                "attachments": attachments.get(p.pk, []),
                "is_accepted": thread.accepted_post_id == p.pk,
                "can_edit": thread_service.can_edit(user, p),
                "can_delete": thread_service.can_delete(user, p, has_visible_replies=p.pk in with_replies),
            }
        )
    return out


def thread_detail(thread: DiscussionThread, user) -> dict:
    data = thread_card(thread)
    opening = (
        access.visible_posts(user)
        .filter(thread=thread, is_opening=True)
        .select_related("author", "thread", "thread__community")
        .first()
    )
    data["opening_post"] = posts_payload([opening], user, thread)[0] if opening else None
    data["poll"] = thread_service.poll_payload(thread, user)
    data["accepted_post_id"] = thread.accepted_post_id
    from ..models import ArticleStatus
    from ..services import knowledge as knowledge_service
    from ..services import solutions as solution_service

    data["solution"] = solution_service.solution_payload(user, thread)
    from ..services import announcements as announcement_service

    data["announcement"] = announcement_service.payload(user, thread) if thread.type == "announcement" else None
    data["articles"] = [
        {"id": a.pk, "title": a.title, "status": a.status}
        for a in access.visible_articles(user).filter(source_thread=thread).exclude(status=ArticleStatus.ARCHIVED).order_by("-updated_at")[:5]
    ]
    data["permissions"] = {
        "can_reply": access.can_post_in(user, thread.community_id)
        and (thread.status != "locked" or access.can_moderate(user, thread.community_id)),
        "can_moderate": access.can_moderate(user, thread.community_id),
        "can_edit": bool(opening and thread_service.can_edit(user, opening)),
        "can_delete": bool(opening and thread_service.can_delete(user, opening)),
        "can_accept": solution_service.can_accept(user, thread),
        "can_curate": knowledge_service.can_curate(user, thread.community_id),
    }
    return data

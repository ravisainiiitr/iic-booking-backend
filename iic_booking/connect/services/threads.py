"""Threads, replies, edits, deletes, reactions, follows, bookmarks, polls, drafts and views.

Callers (API views) have already loaded the community / thread / post through ``access`` (404 otherwise). These
functions still re-check write permissions, because a reader is not necessarily allowed to post (suspended
members, archived communities, locked threads, staff-only categories).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

from django.contrib.postgres.search import SearchVector
from django.db import IntegrityError
from django.db import transaction
from django.db.models import Count
from django.db.models import F
from django.db.models import Q
from django.db.models import Sum
from django.db.models import Value
from django.utils import timezone
from django.utils.text import slugify

from .. import access
from ..models import SOLVABLE_TYPES
from ..models import Community
from ..models import CommunityKind
from ..models import ConnectSettings
from ..models import DiscussionAttachment
from ..models import DiscussionBookmark
from ..models import DiscussionCategory
from ..models import DiscussionDraft
from ..models import DiscussionPollOption
from ..models import DiscussionPollVote
from ..models import DiscussionPost
from ..models import DiscussionPostRevision
from ..models import DiscussionReaction
from ..models import DiscussionSubscription
from ..models import DiscussionTag
from ..models import DiscussionThread
from ..models import ModerationAction
from ..models import ReactionKind
from ..models import SubscriptionLevel
from ..models import ThreadReadState
from ..models import ThreadStatus
from ..models import ThreadTag
from ..models import ThreadType
from ..models import Visibility
from ..sanitize import RichTextTooLong
from ..sanitize import clean_plain
from ..sanitize import html_to_text
from ..sanitize import mentioned_user_ids
from ..sanitize import referenced_attachment_ids
from ..sanitize import sanitize_post_html
from ..sanitize import strip_attachment_refs

TITLE_MIN, TITLE_MAX = 8, 200
MAX_TAGS = 5
MAX_POLL_OPTIONS = 10
MAX_LINKS = 20
DUPLICATE_WINDOW = timedelta(minutes=5)
VIEW_DEDUPE = timedelta(minutes=30)
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$")
_CONTEXT_KEY = re.compile(r"^(new|new:community:\d{1,9}|new:equipment:\d{1,9}|reply:\d{1,12}|edit:\d{1,12})$")
STAFF_ONLY_TYPES = frozenset({ThreadType.ANNOUNCEMENT})


class ThreadError(ValueError):
    """User-facing validation error (answered with 400)."""

    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.field = field


class NotAllowed(PermissionError):
    """The user can read but may not do this (answered with 403)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_SETTINGS_CACHE: dict = {}
SETTINGS_TTL_SECONDS = 15


def _settings() -> ConnectSettings:
    """Module settings, cached briefly per process (cleared whenever the row is saved)."""
    import time

    hit = _SETTINGS_CACHE.get("row")
    if hit and hit[1] > time.monotonic():
        return hit[0]
    row = ConnectSettings.load()
    _SETTINGS_CACHE["row"] = (row, time.monotonic() + SETTINGS_TTL_SECONDS)
    return row


def clear_settings_cache() -> None:
    _SETTINGS_CACHE.clear()


def is_staff_in(user, community_id: int) -> bool:
    """Moderator / OIC / Lab Operator of the community, or a Connect admin."""
    return access.can_moderate(user, community_id)


def ensure_can_post(user, community: Community) -> None:
    if not access.can_read_community(user, community.pk):
        raise NotAllowed("You cannot post here.")
    if not access.can_post_in(user, community.pk):
        raise NotAllowed("Posting is not available to you in this community.")
    hours = _settings().min_account_age_hours_to_post
    if hours and getattr(user, "date_joined", None) and user.date_joined > timezone.now() - timedelta(hours=hours):
        raise NotAllowed("New accounts can post after a short waiting period.")


def _clean_body(raw) -> tuple[str, str]:
    try:
        html = sanitize_post_html(raw)
    except RichTextTooLong as exc:
        raise ThreadError(str(exc), "body") from exc
    text = html_to_text(html)
    if not text.strip() and "data-attachment-id" not in html:
        raise ThreadError("Write something before posting.", "body")
    if html.count("<a ") > MAX_LINKS:
        raise ThreadError(f"A post can contain at most {MAX_LINKS} links.", "body")
    return html, text


def _clean_title(raw) -> str:
    title = clean_plain(raw, max_length=TITLE_MAX)
    if len(title) < TITLE_MIN:
        raise ThreadError(f"The title needs at least {TITLE_MIN} characters.", "title")
    return title


def _check_duplicate(user, text: str) -> None:
    recent = DiscussionPost.objects.filter(author=user, created_at__gte=timezone.now() - DUPLICATE_WINDOW)
    if text and recent.filter(body_text=text).exists():
        raise ThreadError("You posted the same text a moment ago.", "body")


def badge_for(user, community_id: int, equipment_id: int | None = None) -> str:
    """Verified badge from assignments. In General IITR Discussions (no group membership), a thread linked to an
    instrument shows the author's OIC / Lab Operator badge for that instrument."""
    badge = access.role_badge(user, community_id)
    if badge or not equipment_id:
        return badge
    snap = access.snapshot(user)
    if snap.general_id != community_id:
        return ""
    from .staff_tools import staff_badge_for_equipment

    return staff_badge_for_equipment(user, equipment_id)


def resolve_tags(user, raw_tags) -> list[DiscussionTag]:
    if not raw_tags:
        return []
    if not isinstance(raw_tags, (list, tuple)):
        raise ThreadError("Tags must be a list.", "tags")
    slugs: list[str] = []
    for raw in raw_tags:
        slug = slugify(str(raw or ""))[:40].strip("-")
        if not slug:
            continue
        if not _TAG_RE.match(slug):
            raise ThreadError("Tags use 2 to 40 letters, digits or hyphens.", "tags")
        if slug not in slugs:
            slugs.append(slug)
    if len(slugs) > MAX_TAGS:
        raise ThreadError(f"Use at most {MAX_TAGS} tags.", "tags")
    existing = {t.slug: t for t in DiscussionTag.objects.filter(slug__in=slugs)}
    tags = []
    for slug in slugs:
        tag = existing.get(slug)
        if tag is None:
            tag, _ = DiscussionTag.objects.get_or_create(
                slug=slug, defaults={"name": slug.replace("-", " "), "created_by": user}
            )
        if not tag.is_active:
            raise ThreadError(f"The tag '{slug}' is not available.", "tags")
        tags.append(tag)
    return tags


def resolve_equipment(user, community: Community, equipment_id):
    """Equipment the user can see that belongs to this community's group (any visible equipment in General)."""
    if equipment_id in (None, "", 0):
        return None
    try:
        equipment_id = int(equipment_id)
    except (TypeError, ValueError) as exc:
        raise ThreadError("Unknown equipment.", "equipment") from exc
    from iic_booking.equipment.api_views import get_visible_equipment_queryset

    qs = get_visible_equipment_queryset(user, catalog_scope="all", mode_catalog_filter=False).filter(pk=equipment_id)
    if community.kind == CommunityKind.GROUP:
        gid = community.equipment_group_ref_id
        qs = qs.filter(Q(equipment_group_id=gid) | Q(equipment_group_id__isnull=True, parent_equipment__equipment_group_id=gid))
    equipment = qs.select_related(None).first()
    if equipment is None:
        raise ThreadError("Choose equipment from this community's group.", "equipment")
    return equipment


def bind_attachments(uploader, community: Community, post: DiscussionPost, ids, html: str) -> str:
    """Bind ``uploader``'s own unbound uploads of this community to ``post`` (``uploader=None`` binds nothing new).

    Returns ``html`` with inline images limited to the post's own image attachments (foreign or unknown ids removed).
    """
    already = {str(pk): inline for pk, inline in post.attachments.values_list("pk", "is_inline_image")}
    wanted = {str(i) for i in (ids or []) if i} | set(referenced_attachment_ids(html))
    new_ids = [w for w in wanted if w not in already and re.fullmatch(r"[0-9a-f-]{36}", w)]
    rows = []
    if uploader is not None and new_ids:
        rows = list(
            DiscussionAttachment.objects.select_for_update().filter(
                pk__in=new_ids, uploader=uploader, community=community, post__isnull=True
            )
        )
    limit = _settings().max_attachments_per_post
    if len(already) + len(rows) > limit:
        raise ThreadError(f"Attach at most {limit} files to one post.", "attachments")
    now = timezone.now()
    for row in rows:
        row.post = post
        row.attached_at = now
        row.save(update_fields=["post", "attached_at"])
    allowed = {pk for pk, inline in already.items() if inline} | {str(r.pk) for r in rows if r.is_inline_image}
    return strip_attachment_refs(html, allowed)


def refresh_thread_search(thread_id: int) -> None:
    thread = DiscussionThread.objects.filter(pk=thread_id).only("id", "title").first()
    if thread is None:
        return
    opening = DiscussionPost.objects.filter(thread_id=thread_id, is_opening=True).values_list("body_text", flat=True).first() or ""
    tags = " ".join(ThreadTag.objects.filter(thread_id=thread_id).values_list("tag__name", flat=True))
    DiscussionThread.objects.filter(pk=thread_id).update(
        search_vector=SearchVector(Value(thread.title), weight="A", config="english")
        + SearchVector(Value(tags), weight="B", config="english")
        + SearchVector(Value(opening[:20000]), weight="C", config="english")
    )


def refresh_post_search(post_id: int) -> None:
    text = DiscussionPost.objects.filter(pk=post_id).values_list("body_text", flat=True).first() or ""
    DiscussionPost.objects.filter(pk=post_id).update(search_vector=SearchVector(Value(text[:20000]), config="english"))


def refresh_counters(thread_id: int) -> None:
    visible = DiscussionPost.objects.filter(thread_id=thread_id, visibility=Visibility.VISIBLE)
    replies = visible.filter(is_opening=False)
    last = replies.order_by("-created_at", "-id").first()
    thread = DiscussionThread.objects.get(pk=thread_id)
    DiscussionThread.objects.filter(pk=thread_id).update(
        reply_count=replies.count(),
        participant_count=visible.values("author_id").distinct().count(),
        helpful_count=visible.aggregate(n=Sum("helpful_count"))["n"] or 0,
        last_post_id=last.pk if last else None,
        last_activity_at=max(filter(None, [last.created_at if last else None, thread.created_at])),
    )


def auto_follow(user, thread: DiscussionThread, *, reason: str) -> None:
    from ..models import ConnectNotificationPreference

    pref = ConnectNotificationPreference.objects.filter(user=user).first()
    if pref is not None:
        if reason == "own" and not pref.auto_follow_own_threads:
            return
        if reason == "reply" and not pref.auto_follow_replied_threads:
            return
    DiscussionSubscription.objects.get_or_create(user=user, thread=thread, defaults={"level": SubscriptionLevel.WATCHING})


def mention_targets(html: str, thread: DiscussionThread, author) -> list[int]:
    """Mentioned users who can read the thread (others are silently ignored, never notified)."""
    from django.contrib.auth import get_user_model

    ids = [i for i in mentioned_user_ids(html) if i != author.pk]
    if not ids:
        return []
    out = []
    for user in get_user_model().objects.filter(pk__in=ids, is_active=True):
        if access.visible_threads(user).filter(pk=thread.pk).exists():
            out.append(user.pk)
    return out


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@dataclass
class ThreadInput:
    community: Community
    title: str
    body: str
    type: str = ThreadType.GENERAL
    category_id: int | None = None
    equipment_id: int | None = None
    tags: list | None = None
    attachments: list | None = None
    poll_options: list | None = None
    poll_closes_at: object = None
    follow: bool = True


@transaction.atomic
def create_thread(user, data: ThreadInput) -> DiscussionThread:
    community = data.community
    ensure_can_post(user, community)
    if data.type not in ThreadType.values:
        raise ThreadError("Choose a discussion type.", "type")
    staff = is_staff_in(user, community.pk)
    if data.type in STAFF_ONLY_TYPES and not staff:
        raise NotAllowed("Only facility staff and moderators can post announcements.")
    category = None
    if data.category_id:
        category = DiscussionCategory.objects.filter(pk=data.category_id, community=community, is_active=True).first()
        if category is None:
            raise ThreadError("Choose a category of this community.", "category")
        if category.staff_only_posting and not staff:
            raise NotAllowed("Only facility staff and moderators can start threads in this category.")
    title = _clean_title(data.title)
    html, text = _clean_body(data.body)
    _check_duplicate(user, text)
    equipment = resolve_equipment(user, community, data.equipment_id)
    tags = resolve_tags(user, data.tags)
    options = []
    if data.type == ThreadType.POLL:
        options = [clean_plain(o, max_length=200) for o in (data.poll_options or [])]
        options = [o for o in dict.fromkeys(options) if o]
        if not 2 <= len(options) <= MAX_POLL_OPTIONS:
            raise ThreadError(f"A poll needs 2 to {MAX_POLL_OPTIONS} different options.", "poll_options")

    now = timezone.now()
    thread = DiscussionThread.objects.create(
        community=community,
        category=category,
        equipment=equipment,
        equipment_ref_id=equipment.pk if equipment else None,
        author=user,
        type=data.type,
        title=title,
        last_activity_at=now,
        poll_closes_at=data.poll_closes_at if data.type == ThreadType.POLL else None,
    )
    badge = badge_for(user, community.pk, thread.equipment_ref_id)
    post = DiscussionPost.objects.create(
        thread=thread,
        author=user,
        is_opening=True,
        body_html=html,
        body_text=text,
        author_badge=badge,
        is_verified_response=bool(badge),
    )
    final_html = bind_attachments(user, community, post, data.attachments, html)
    if final_html != html:
        DiscussionPost.objects.filter(pk=post.pk).update(body_html=final_html)
    for tag in tags:
        ThreadTag.objects.create(thread=thread, tag=tag)
    for position, label in enumerate(options):
        DiscussionPollOption.objects.create(thread=thread, label=label, position=position)
    if data.follow:
        auto_follow(user, thread, reason="own")
    DiscussionDraft.objects.filter(user=user, context_key__startswith="new").delete()
    refresh_thread_search(thread.pk)
    refresh_post_search(post.pk)
    thread.refresh_from_db()
    thread._new_post = post  # noqa: SLF001 - consumed by notifications (M10)
    _after_post(thread, post, final_html)
    return thread


@transaction.atomic
def create_reply(user, thread: DiscussionThread, body, *, parent_id=None, attachments=None) -> DiscussionPost:
    DiscussionThread.objects.select_for_update().filter(pk=thread.pk).first()
    thread.refresh_from_db()
    community = thread.community
    ensure_can_post(user, community)
    staff = is_staff_in(user, community.pk)
    if thread.visibility != Visibility.VISIBLE and not staff:
        raise NotAllowed("This thread is not open for replies.")
    if thread.status == ThreadStatus.LOCKED and not staff:
        raise NotAllowed("This thread is locked.")
    parent = None
    if parent_id:
        parent = DiscussionPost.objects.filter(pk=parent_id, thread=thread, visibility=Visibility.VISIBLE).first()
        if parent is None:
            raise ThreadError("The post you are replying to is not available.", "parent")
    html, text = _clean_body(body)
    _check_duplicate(user, text)
    badge = badge_for(user, community.pk, thread.equipment_ref_id)
    post = DiscussionPost.objects.create(
        thread=thread,
        parent=parent,
        author=user,
        body_html=html,
        body_text=text,
        author_badge=badge,
        is_verified_response=bool(badge),
    )
    final_html = bind_attachments(user, community, post, attachments, html)
    if final_html != html:
        DiscussionPost.objects.filter(pk=post.pk).update(body_html=final_html)
        post.body_html = final_html
    refresh_counters(thread.pk)
    refresh_post_search(post.pk)
    auto_follow(user, thread, reason="reply")
    DiscussionDraft.objects.filter(user=user, context_key=f"reply:{thread.pk}").delete()
    _after_post(thread, post, final_html)
    return post


def _after_post(thread, post, html) -> None:
    """Hook for notifications (wired in M10). Never raises."""
    try:
        from . import notifications  # noqa: F401  (optional until M10)
    except ImportError:
        return
    try:
        notifications.post_created(thread, post, html)
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning("IITR Connect: notification fan-out failed", exc_info=True)


# ---------------------------------------------------------------------------
# Edit / delete (author policy; moderator edits are audited)
# ---------------------------------------------------------------------------


def can_edit(user, post: DiscussionPost) -> bool:
    thread = post.thread
    if post.visibility in (Visibility.REMOVED, Visibility.DELETED):
        return False
    if is_staff_in(user, thread.community_id):
        return True
    if post.author_id != user.pk or not access.can_post_in(user, thread.community_id):
        return False
    if thread.status == ThreadStatus.LOCKED:
        return False
    window = _settings().edit_window_minutes
    return not window or post.created_at >= timezone.now() - timedelta(minutes=window)


def can_delete(user, post: DiscussionPost, *, has_visible_replies: bool | None = None) -> bool:
    if post.author_id != user.pk or post.visibility in (Visibility.REMOVED, Visibility.DELETED):
        return False
    if not access.can_post_in(user, post.thread.community_id):
        return False
    if post.is_opening and post.thread.reply_count == 0:
        return True
    if has_visible_replies is None:
        has_visible_replies = post.replies.filter(visibility=Visibility.VISIBLE).exists()
    if not post.is_opening and not has_visible_replies:
        if post.thread.accepted_post_id == post.pk:
            return False
        return True
    window = _settings().delete_window_minutes
    return bool(window) and post.created_at >= timezone.now() - timedelta(minutes=window) and not post.is_opening


@transaction.atomic
def edit_post(
    user, post: DiscussionPost, *, body=None, title=None, tags=None, category_id=None, attachments=None, reason: str = ""
) -> DiscussionPost:
    post = DiscussionPost.objects.select_for_update().select_related("thread", "thread__community").get(pk=post.pk)
    if not can_edit(user, post):
        raise NotAllowed("You can no longer edit this post.")
    thread = post.thread
    by_moderator = post.author_id != user.pk
    before = {"title": thread.title if post.is_opening else "", "body_html": post.body_html}
    changed = False
    if body is not None:
        html, text = _clean_body(body)
        html = bind_attachments(None if by_moderator else user, thread.community, post, attachments, html)
        if html != post.body_html:
            post.body_html, post.body_text = html, text
            changed = True
    thread_fields = []
    if post.is_opening:
        if title is not None:
            new_title = _clean_title(title)
            if new_title != thread.title:
                thread.title = new_title
                thread_fields.append("title")
        if category_id is not None:
            category = DiscussionCategory.objects.filter(pk=category_id, community=thread.community, is_active=True).first()
            if category is None:
                raise ThreadError("Choose a category of this community.", "category")
            if category.staff_only_posting and not is_staff_in(user, thread.community_id):
                raise NotAllowed("Only facility staff can use this category.")
            if category.pk != thread.category_id:
                thread.category = category
                thread_fields.append("category")
        if tags is not None:
            new_tags = resolve_tags(user, tags)
            ThreadTag.objects.filter(thread=thread).exclude(tag__in=new_tags).delete()
            for tag in new_tags:
                ThreadTag.objects.get_or_create(thread=thread, tag=tag)
            changed = True
    if not changed and not thread_fields:
        return post
    DiscussionPostRevision.objects.create(
        post=post, editor=user, title=before["title"], body_html=before["body_html"], reason=(reason or "")[:300],
        by_moderator=by_moderator,
    )
    now = timezone.now()
    post.edit_count = F("edit_count") + 1
    post.edited_at = now
    post.save(update_fields=["body_html", "body_text", "edit_count", "edited_at"])
    if thread_fields:
        thread.edited_at = now
        thread.save(update_fields=[*thread_fields, "edited_at", "updated_at"])
    if by_moderator:
        ModerationAction.objects.create(
            actor=user,
            community=thread.community,
            action="edit",
            target_type="post",
            target_id=str(post.pk),
            reason=(reason or "")[:1000],
            before={"title": before["title"], "body_excerpt": html_to_text(before["body_html"])[:500]},
            after={"title": thread.title if post.is_opening else "", "body_excerpt": post.body_text[:500] if isinstance(post.body_text, str) else ""},
        )
    if post.is_opening:
        refresh_thread_search(thread.pk)
    refresh_post_search(post.pk)
    post.refresh_from_db()
    return post


@transaction.atomic
def delete_own_post(user, post: DiscussionPost) -> None:
    post = DiscussionPost.objects.select_for_update().select_related("thread", "thread__community").get(pk=post.pk)
    if not can_delete(user, post):
        raise NotAllowed("This post can no longer be deleted. Ask a moderator if it must be removed.")
    post.visibility = Visibility.DELETED
    post.save(update_fields=["visibility"])
    thread = post.thread
    if post.is_opening:
        thread.visibility = Visibility.DELETED
        thread.save(update_fields=["visibility", "updated_at"])
    ModerationAction.objects.create(
        actor=user, community=thread.community, action="author_delete", target_type="thread" if post.is_opening else "post",
        target_id=str(thread.pk if post.is_opening else post.pk),
    )
    refresh_counters(thread.pk)


# ---------------------------------------------------------------------------
# Reactions, bookmarks, follows, polls, views, drafts
# ---------------------------------------------------------------------------


@transaction.atomic
def toggle_reaction(user, post: DiscussionPost, kind: str) -> dict:
    if kind not in ReactionKind.values:
        raise ThreadError("Unknown reaction.", "kind")
    if post.author_id == user.pk:
        raise NotAllowed("You cannot react to your own post.")
    if post.visibility != Visibility.VISIBLE:
        raise NotAllowed("This post is not available.")
    if not access.can_post_in(user, post.thread.community_id):
        raise NotAllowed("Reactions are not available to you in this community.")
    existing = DiscussionReaction.objects.filter(post=post, user=user, kind=kind).first()
    if existing:
        existing.delete()
        active = False
    else:
        try:
            with transaction.atomic():
                DiscussionReaction.objects.create(post=post, user=user, kind=kind)
        except IntegrityError:
            pass
        active = True
    if kind == ReactionKind.HELPFUL:
        n = DiscussionReaction.objects.filter(post=post, kind=ReactionKind.HELPFUL).count()
        DiscussionPost.objects.filter(pk=post.pk).update(helpful_count=n)
        refresh_counters(post.thread_id)
    counts = dict(DiscussionReaction.objects.filter(post=post).values_list("kind").annotate(n=Count("pk")).values_list("kind", "n"))
    return {"kind": kind, "active": active, "counts": counts}


def toggle_bookmark(user, thread: DiscussionThread) -> bool:
    deleted, _ = DiscussionBookmark.objects.filter(user=user, thread=thread).delete()
    if deleted:
        return False
    DiscussionBookmark.objects.get_or_create(user=user, thread=thread)
    return True


def set_thread_follow(user, thread: DiscussionThread, level: str | None) -> str | None:
    if level is None:
        DiscussionSubscription.objects.filter(user=user, thread=thread).delete()
        return None
    if level not in SubscriptionLevel.values:
        raise ThreadError("Unknown follow level.", "level")
    DiscussionSubscription.objects.update_or_create(user=user, thread=thread, defaults={"level": level})
    return level


def set_community_follow(user, community: Community, level: str | None) -> str | None:
    if level is None:
        DiscussionSubscription.objects.filter(user=user, community=community).delete()
        return None
    if level not in SubscriptionLevel.values:
        raise ThreadError("Unknown follow level.", "level")
    DiscussionSubscription.objects.update_or_create(user=user, community=community, defaults={"level": level})
    return level


@transaction.atomic
def vote(user, thread: DiscussionThread, option_id) -> dict:
    if thread.type != ThreadType.POLL:
        raise ThreadError("This thread is not a poll.")
    if not access.can_post_in(user, thread.community_id) or thread.status == ThreadStatus.LOCKED:
        raise NotAllowed("Voting is not available to you.")
    if thread.poll_closes_at and thread.poll_closes_at <= timezone.now():
        raise NotAllowed("This poll is closed.")
    option = DiscussionPollOption.objects.filter(pk=option_id, thread=thread).first()
    if option is None:
        raise ThreadError("Choose one of the poll options.", "option")
    DiscussionThread.objects.select_for_update().filter(pk=thread.pk).first()
    DiscussionPollVote.objects.update_or_create(thread=thread, user=user, defaults={"option": option})
    for opt in DiscussionPollOption.objects.filter(thread=thread).annotate(n=Count("votes")):
        if opt.vote_count != opt.n:
            DiscussionPollOption.objects.filter(pk=opt.pk).update(vote_count=opt.n)
    return poll_payload(thread, user)


def poll_payload(thread: DiscussionThread, user) -> dict | None:
    if thread.type != ThreadType.POLL:
        return None
    mine = DiscussionPollVote.objects.filter(thread=thread, user=user).values_list("option_id", flat=True).first()
    options = list(DiscussionPollOption.objects.filter(thread=thread).values("id", "label", "vote_count"))
    return {
        "options": options,
        "total_votes": sum(o["vote_count"] for o in options),
        "my_vote": mine,
        "closes_at": thread.poll_closes_at,
        "is_closed": bool(thread.poll_closes_at and thread.poll_closes_at <= timezone.now()),
    }


def record_view(user, thread: DiscussionThread) -> None:
    now = timezone.now()
    last_post_id = thread.last_post_id
    state = ThreadReadState.objects.filter(user=user, thread=thread).first()
    if state is None:
        try:
            with transaction.atomic():
                ThreadReadState.objects.create(user=user, thread=thread, last_viewed_at=now, last_read_post_id=last_post_id)
        except IntegrityError:
            return
        DiscussionThread.objects.filter(pk=thread.pk).update(view_count=F("view_count") + 1)
        return
    if state.last_viewed_at < now - VIEW_DEDUPE:
        DiscussionThread.objects.filter(pk=thread.pk).update(view_count=F("view_count") + 1)
    ThreadReadState.objects.filter(pk=state.pk).update(last_viewed_at=now, last_read_post_id=last_post_id)


MAX_DRAFT_BYTES = 200_000


def validate_context_key(key: str) -> str:
    key = str(key or "")
    if not _CONTEXT_KEY.match(key):
        raise ThreadError("Unknown draft.", "context_key")
    return key


def save_draft(user, key: str, payload) -> DiscussionDraft:
    import json

    key = validate_context_key(key)
    if not isinstance(payload, dict):
        raise ThreadError("Draft content must be an object.", "payload")
    if len(json.dumps(payload, default=str)) > MAX_DRAFT_BYTES:
        raise ThreadError("The draft is too large.", "payload")
    allowed = {"title", "body", "type", "category_id", "community_id", "equipment_id", "tags", "attachments", "poll_options"}
    clean = {k: v for k, v in payload.items() if k in allowed}
    draft, _ = DiscussionDraft.objects.update_or_create(user=user, context_key=key, defaults={"payload": clean})
    return draft


__all__ = [
    "SOLVABLE_TYPES",
    "NotAllowed",
    "ThreadError",
    "ThreadInput",
    "can_delete",
    "can_edit",
    "create_reply",
    "create_thread",
    "delete_own_post",
    "edit_post",
    "poll_payload",
    "record_view",
    "save_draft",
    "set_community_follow",
    "set_thread_follow",
    "toggle_bookmark",
    "toggle_reaction",
    "vote",
]

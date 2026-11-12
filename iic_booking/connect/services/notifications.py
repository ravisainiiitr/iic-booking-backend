"""IITR Connect notifications through the portal's existing in-app bell and email infrastructure.

Events: reply, reply to my post, mention, accepted answer, followed activity, announcement, article published,
moderation of my content.

* One ``DiscussionNotification`` per recipient per post (the most specific event wins: mention > reply to me > reply
  > followed). Authors are never notified about their own actions.
* Access is re-checked for every recipient when the notification is created and again when an email (immediate or
  digest) is built, so someone who left a group is never told about it.
* Notifications carry only the thread title, community and a link, never post bodies.
* In-app: ``communication.in_app.notify_in_app`` (category "discussions", honouring the portal preference) unless the
  user switched Connect in-app off. Email: immediately, in a daily digest or a weekly digest, or never, per
  ``ConnectNotificationPreference`` (default: daily digest), and only if the portal "discussions" email category is
  on. Large audiences (announcements) are fanned out by Celery.
"""

from __future__ import annotations

import html
import logging
from collections import defaultdict

from django.conf import settings
from django.db import IntegrityError
from django.db import transaction
from django.utils import timezone

from .. import access
from ..models import ConnectNotificationPreference
from ..models import DiscussionNotification
from ..models import DiscussionPost
from ..models import DiscussionSubscription
from ..models import DiscussionThread
from ..models import EmailMode
from ..models import NotificationEvent
from ..models import SubscriptionLevel

logger = logging.getLogger(__name__)

CATEGORY = "discussions"
ACTIVITY_TEMPLATE = "connect_activity_email"
DIGEST_TEMPLATE = "connect_digest_email"
INLINE_FANOUT_LIMIT = 50
PRIORITY = [
    NotificationEvent.MENTION,
    NotificationEvent.REPLY_TO_ME,
    NotificationEvent.REPLY,
    NotificationEvent.ACCEPTED,
    NotificationEvent.ANNOUNCEMENT,
    NotificationEvent.ARTICLE_PUBLISHED,
    NotificationEvent.MODERATION,
    NotificationEvent.FOLLOWED,
]

TITLES = {
    NotificationEvent.REPLY: "{actor} replied to your discussion",
    NotificationEvent.REPLY_TO_ME: "{actor} replied to your post",
    NotificationEvent.MENTION: "{actor} mentioned you",
    NotificationEvent.ACCEPTED: "Your answer was accepted",
    NotificationEvent.FOLLOWED: "New activity in a discussion you follow",
    NotificationEvent.ANNOUNCEMENT: "Announcement in {community}",
    NotificationEvent.ARTICLE_PUBLISHED: "New Knowledge Hub article in {community}",
    NotificationEvent.MODERATION: "A moderator updated your post",
}


def _name(user) -> str:
    return (getattr(user, "name", "") or "").strip() or "Someone"


def preference(user) -> ConnectNotificationPreference:
    pref = ConnectNotificationPreference.objects.filter(user=user).first()
    return pref or ConnectNotificationPreference(user=user)


def _link(thread_id: int | None, post_id: int | None = None, article_id: int | None = None) -> str:
    if article_id:
        return f"/connect/knowledge/{article_id}"
    return f"/connect/t/{thread_id}" + (f"#post-{post_id}" if post_id else "")


def _can_read(user, thread: DiscussionThread | None, article_id: int | None = None) -> bool:
    if not getattr(user, "is_active", False):
        return False
    access.invalidate(user)
    if article_id:
        return access.visible_articles(user).filter(pk=article_id, status="published").exists()
    return thread is not None and access.visible_threads(user).filter(pk=thread.pk, visibility="visible").exists()


def _after_commit(fn, *args):
    def run():
        try:
            fn(*args)
        except Exception:  # noqa: BLE001 - notifications never break the action that triggered them
            logger.warning("IITR Connect: notification delivery failed (%s)", getattr(fn, "__name__", fn), exc_info=True)

    transaction.on_commit(run)


# ---------------------------------------------------------------------------
# Event entry points (called by services; always after commit)
# ---------------------------------------------------------------------------


def post_created(thread, post, html_body) -> None:
    if not access.feature_enabled():
        return
    _after_commit(_fanout_post, post.pk)


def solution_accepted(thread, post, actor) -> None:
    if not access.feature_enabled() or post.author_id == actor.pk:
        return
    _after_commit(_deliver_many, [(post.author_id, NotificationEvent.ACCEPTED)], thread.pk, post.pk, None, actor.pk)


def article_published(article, actor) -> None:
    if not access.feature_enabled():
        return
    _after_commit(_fanout_article, article.pk, actor.pk)


def announcement_published(announcement) -> None:
    if not access.feature_enabled():
        return
    _after_commit(_queue_announcement, announcement.pk)


def moderation_notice(post, actor, action: str) -> None:
    if not access.feature_enabled() or post.author_id == actor.pk:
        return
    _after_commit(_deliver_many, [(post.author_id, NotificationEvent.MODERATION)], post.thread_id, post.pk, None, actor.pk)


# ---------------------------------------------------------------------------
# Recipient computation
# ---------------------------------------------------------------------------


def _fanout_post(post_id: int) -> None:
    from .threads import mention_targets

    post = DiscussionPost.objects.select_related("thread", "author", "parent").filter(pk=post_id).first()
    if post is None:
        return
    thread = post.thread
    events: dict[int, str] = {}

    def add(uid, event):
        if not uid or uid == post.author_id:
            return
        current = events.get(uid)
        if current is None or PRIORITY.index(event) < PRIORITY.index(current):
            events[uid] = event

    for uid in mention_targets(post.body_html, thread, post.author):
        add(uid, NotificationEvent.MENTION)
    if post.parent_id and post.parent and not post.parent.is_opening:
        add(post.parent.author_id, NotificationEvent.REPLY_TO_ME)
    if not post.is_opening:
        add(thread.author_id, NotificationEvent.REPLY)
    muted = set(DiscussionSubscription.objects.filter(thread=thread, level=SubscriptionLevel.MUTED).values_list("user_id", flat=True))
    watchers = DiscussionSubscription.objects.filter(level=SubscriptionLevel.WATCHING)
    if post.is_opening:
        watchers = watchers.filter(community_id=thread.community_id)  # new thread in a followed community
    else:
        watchers = watchers.filter(thread=thread)
    for uid in watchers.values_list("user_id", flat=True):
        add(uid, NotificationEvent.FOLLOWED)
    for uid in muted:
        events.pop(uid, None)
    if thread.type == "announcement" and post.is_opening:
        return  # announcements are delivered to their audience by announcement_published
    _deliver_many(list(events.items()), thread.pk, post.pk, None, post.author_id)


def _fanout_article(article_id: int, actor_id: int) -> None:
    from ..models import KnowledgeArticle

    article = KnowledgeArticle.objects.filter(pk=article_id).select_related("source_thread").first()
    if article is None:
        return
    ids = set(DiscussionSubscription.objects.filter(community_id=article.community_id, level=SubscriptionLevel.WATCHING).values_list("user_id", flat=True))
    if article.source_thread_id:
        ids |= set(DiscussionSubscription.objects.filter(thread_id=article.source_thread_id, level=SubscriptionLevel.WATCHING).values_list("user_id", flat=True))
        ids.add(article.source_thread.author_id)
    ids.discard(actor_id)
    _deliver_many([(uid, NotificationEvent.ARTICLE_PUBLISHED) for uid in ids], article.source_thread_id, None, article.pk, actor_id,
                  community_id=article.community_id)


def _queue_announcement(announcement_id: int) -> None:
    from ..models import CommunityAnnouncement
    from .announcements import audience_user_ids

    announcement = CommunityAnnouncement.objects.select_related("thread").filter(pk=announcement_id).first()
    if announcement is None or announcement.notified_at:
        return
    ids = audience_user_ids(announcement)
    CommunityAnnouncement.objects.filter(pk=announcement.pk).update(notified_at=timezone.now())
    if len(ids) <= INLINE_FANOUT_LIMIT:
        deliver_announcement_batch(announcement.pk, ids)
        return
    from ..tasks import announcement_batch_task

    for i in range(0, len(ids), 200):
        announcement_batch_task.delay(announcement.pk, ids[i : i + 200])


def deliver_announcement_batch(announcement_id: int, user_ids: list[int]) -> None:
    from ..models import CommunityAnnouncement

    announcement = CommunityAnnouncement.objects.select_related("thread").filter(pk=announcement_id).first()
    if announcement is None:
        return
    thread = announcement.thread
    opening = DiscussionPost.objects.filter(thread=thread, is_opening=True).values_list("pk", flat=True).first()
    _deliver_many([(uid, NotificationEvent.ANNOUNCEMENT) for uid in user_ids], thread.pk, opening, None, thread.author_id)


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def _deliver_many(pairs, thread_id, post_id, article_id, actor_id, *, community_id=None) -> int:
    from django.contrib.auth import get_user_model

    if not pairs:
        return 0
    User = get_user_model()
    thread = DiscussionThread.objects.select_related("community").filter(pk=thread_id).first() if thread_id else None
    community_id = community_id or (thread.community_id if thread else None)
    if community_id is None:
        return 0
    actor = User.objects.filter(pk=actor_id).first() if actor_id else None
    users = User.objects.in_bulk([uid for uid, _ in pairs])
    prefs = {p.user_id: p for p in ConnectNotificationPreference.objects.filter(user_id__in=list(users))}
    sent = 0
    for uid, event in pairs:
        user = users.get(uid)
        if user is None or not access.user_enabled(user) or not _can_read(user, thread, article_id):
            continue
        pref = prefs.get(uid) or ConnectNotificationPreference(user=user)
        if event in (pref.muted_events or []):
            continue
        try:
            with transaction.atomic():
                note = DiscussionNotification.objects.create(
                    recipient=user,
                    event=event,
                    community_id=community_id,
                    thread=thread,
                    post_id=post_id,
                    article_id=article_id,
                    actor=actor,
                    email_mode=pref.email_mode,
                )
        except IntegrityError:
            continue  # already notified about this post
        _deliver_one(note, user, pref, thread, actor)
        sent += 1
    return sent


def _text(note: DiscussionNotification, thread, actor) -> tuple[str, str]:
    community = thread.community.name if thread else ""
    title = TITLES.get(note.event, "IITR Connect").format(actor=_name(actor), community=community)
    subject_line = thread.title if thread else "Knowledge Hub"
    if note.article_id:
        from ..models import KnowledgeArticle

        subject_line = KnowledgeArticle.objects.filter(pk=note.article_id).values_list("title", flat=True).first() or subject_line
    message = f"{subject_line} ({community})" if community else subject_line
    return title, message


def _deliver_one(note: DiscussionNotification, user, pref, thread, actor) -> None:
    title, message = _text(note, thread, actor)
    link = _link(note.thread_id, note.post_id, note.article_id)
    if pref.in_app_enabled:
        from iic_booking.communication.in_app import notify_in_app

        notify_in_app(
            [user],
            title=title,
            message=message,
            link=link,
            notification_type="info",
            event=f"connect_{note.event}",
            created_by=actor,
            extra={"notification_category": CATEGORY, "connect_notification_id": note.pk},
        )
        DiscussionNotification.objects.filter(pk=note.pk).update(in_app_sent_at=timezone.now())
    if note.email_mode == EmailMode.IMMEDIATE:
        _send_activity_email(user, note, title, message, link)


def _absolute(link: str) -> str:
    base = (getattr(settings, "FRONTEND_URL", "") or getattr(settings, "FRONTEND_BASE_URL", "") or "https://equip.iitr.ac.in").rstrip("/")
    return f"{base}{link}"


def _send_activity_email(user, note, title, message, link) -> None:
    from iic_booking.communication.service import CommunicationService

    try:
        CommunicationService.send_email(
            recipient=user,
            template=ACTIVITY_TEMPLATE,
            template_context={
                "user_name": user.name,
                "headline": html.escape(title),
                "summary": html.escape(message),
                "link_url": _absolute(link),
            },
            metadata={"notification_category": CATEGORY, "connect_notification_id": note.pk},
        )
    except Exception:  # noqa: BLE001
        logger.warning("IITR Connect: activity email failed for notification %s", note.pk, exc_info=True)
        return
    DiscussionNotification.objects.filter(pk=note.pk).update(email_sent_at=timezone.now())


# ---------------------------------------------------------------------------
# Digests
# ---------------------------------------------------------------------------


def send_digests(mode: str, now=None) -> int:
    """Daily or weekly digest: one email per user with the unsent items they can still read."""
    now = now or timezone.now()
    pending = (
        DiscussionNotification.objects.filter(email_mode=mode, email_sent_at__isnull=True, created_at__lte=now)
        .select_related("thread", "thread__community", "recipient")
        .order_by("recipient_id", "-created_at")
    )
    by_user: dict[int, list[DiscussionNotification]] = defaultdict(list)
    for note in pending[:20000]:
        by_user[note.recipient_id].append(note)
    sent = 0
    from iic_booking.communication.service import CommunicationService

    for notes in by_user.values():
        user = notes[0].recipient
        readable = [n for n in notes if access.user_enabled(user) and _can_read(user, n.thread, n.article_id)]
        ids = [n.pk for n in notes]
        if readable:
            rows = []
            for n in readable[:50]:
                title, message = _text(n, n.thread, None)
                rows.append(
                    f'<li style="margin:0 0 8px"><a href="{html.escape(_absolute(_link(n.thread_id, n.post_id, n.article_id)))}">'
                    f"{html.escape(message)}</a><br><span style=\"color:#64748b;font-size:12px\">{html.escape(title)}</span></li>"
                )
            try:
                CommunicationService.send_email(
                    recipient=user,
                    template=DIGEST_TEMPLATE,
                    template_context={
                        "user_name": user.name,
                        "period": "weekly" if mode == EmailMode.WEEKLY else "daily",
                        "count": str(len(readable)),
                        "items_html": f"<ul style=\"padding-left:18px\">{''.join(rows)}</ul>",
                        "items_text": "\n".join(f"- {_text(n, n.thread, None)[1]}" for n in readable[:50]),
                        "link_url": _absolute("/connect"),
                    },
                    metadata={"notification_category": CATEGORY, "connect_digest": mode},
                )
                sent += 1
            except Exception:  # noqa: BLE001
                logger.warning("IITR Connect: digest failed for user %s", user.pk, exc_info=True)
                continue
        # Items the user can no longer read are closed without being sent.
        DiscussionNotification.objects.filter(pk__in=ids).update(email_sent_at=now)
    return sent


def mark_read(user, ids=None) -> int:
    qs = DiscussionNotification.objects.filter(recipient=user, read_at__isnull=True)
    if ids is not None:
        qs = qs.filter(pk__in=[int(i) for i in ids if str(i).isdigit()][:500])
    return qs.update(read_at=timezone.now())


def unread_count(user) -> int:
    return DiscussionNotification.objects.filter(recipient=user, read_at__isnull=True).count()


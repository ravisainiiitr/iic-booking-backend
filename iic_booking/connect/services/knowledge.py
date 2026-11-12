"""Knowledge Hub: structured articles promoted from discussions, reviewed and published by facility staff.

Workflow: Draft -> Under Review -> (approved) -> Published -> Archived (Archived can be restored to Draft).

* Curators = moderators of the article's community: Officer-in-Charge, Lab Operator (Lab In-charge), Group
  Moderator, plus Portal / Community Admins. Only curators promote, review, publish and archive.
* An article's community is copied from its source thread and can never change, so an article is never readable by
  anyone who could not read the discussion it came from (access is inherited, never widened).
* A reviewer cannot approve their own article (four-eyes), except the Portal Admin.
* Publishing needs an approved review of the *current* version. "Reviewed institutional guidance" is shown only
  while ``reviewed_version == version``: any edit afterwards drops the label until the next approved review.
* Unverified community replies are never labelled as guidance: the label exists only on reviewed articles.
"""

from __future__ import annotations

from django.contrib.postgres.search import SearchVector
from django.db import transaction
from django.db.models import F
from django.db.models import Value
from django.utils import timezone

from .. import access
from ..models import ArticleStatus
from ..models import DiscussionCategory
from ..models import DiscussionPost
from ..models import DiscussionThread
from ..models import KnowledgeArticle
from ..models import KnowledgeArticleReview
from ..models import KnowledgeArticleRevision
from ..models import ModerationAction
from ..models import ReviewDecision
from ..models import Visibility
from ..sanitize import RichTextTooLong
from ..sanitize import clean_plain
from ..sanitize import html_to_text
from ..sanitize import sanitize_post_html
from ..sanitize import strip_attachment_refs
from .threads import NotAllowed
from .threads import ThreadError
from .threads import resolve_tags

TITLE_MIN = 8


def can_curate(user, community_id: int) -> bool:
    return access.can_moderate(user, community_id)


def is_guidance(article: KnowledgeArticle) -> bool:
    return bool(
        article.is_institutional_guidance
        and article.status == ArticleStatus.PUBLISHED
        and article.reviewed_version is not None
        and article.reviewed_version == article.version
    )


def _clean_body(raw) -> tuple[str, str]:
    try:
        html = sanitize_post_html(raw)
    except RichTextTooLong as exc:
        raise ThreadError(str(exc), "body") from exc
    text = html_to_text(html)
    if not text.strip():
        raise ThreadError("The article needs some content.", "body")
    return html, text


def _refresh_search(article_id: int) -> None:
    a = KnowledgeArticle.objects.filter(pk=article_id).values("title", "summary", "body_text").first()
    if not a:
        return
    KnowledgeArticle.objects.filter(pk=article_id).update(
        search_vector=SearchVector(Value(a["title"]), weight="A", config="english")
        + SearchVector(Value(a["summary"] or ""), weight="B", config="english")
        + SearchVector(Value((a["body_text"] or "")[:20000]), weight="C", config="english")
    )


def _revision(article: KnowledgeArticle, editor, note: str) -> None:
    KnowledgeArticleRevision.objects.create(
        article=article,
        version=article.version,
        title=article.title,
        summary=article.summary,
        body_html=article.body_html,
        status=article.status,
        editor=editor,
        change_note=(note or "")[:300],
    )


def _audit(user, article: KnowledgeArticle, action: str, before=None, after=None, reason: str = "") -> None:
    ModerationAction.objects.create(
        actor=user,
        community_id=article.community_id,
        action=action,
        target_type="article",
        target_id=str(article.pk),
        reason=(reason or "")[:1000],
        before=before or {},
        after=after or {},
    )


def default_body(thread: DiscussionThread, post: DiscussionPost | None) -> str:
    """Problem (the opening post) and solution (the chosen reply), as a starting point the curator edits."""
    opening = DiscussionPost.objects.filter(thread=thread, is_opening=True).first()
    parts = ["<h3>Problem</h3>", opening.body_html if opening else ""]
    if post is not None:
        parts += ["<h3>Solution</h3>", post.body_html]
    # Inline images of the discussion are not carried over: they stay attached to their posts.
    return strip_attachment_refs("".join(parts), set())


@transaction.atomic
def promote(user, thread: DiscussionThread, *, post_id=None, title=None, summary="", body=None, tags=None, category_id=None) -> KnowledgeArticle:
    if not can_curate(user, thread.community_id):
        raise NotAllowed("Only facility staff and moderators of this community can promote discussions.")
    if thread.visibility != Visibility.VISIBLE:
        raise ThreadError("Only visible discussions can be promoted.")
    post = None
    pid = post_id or thread.accepted_post_id
    if pid:
        post = DiscussionPost.objects.filter(pk=pid, thread=thread, visibility=Visibility.VISIBLE).first()
        if post is None:
            raise ThreadError("Choose a visible post of this discussion.", "post")
    html, text = _clean_body(body if body else default_body(thread, post))
    title = clean_plain(title or thread.title, max_length=200)
    if len(title) < TITLE_MIN:
        raise ThreadError(f"The title needs at least {TITLE_MIN} characters.", "title")
    category = None
    if category_id:
        category = DiscussionCategory.objects.filter(pk=category_id, community_id=thread.community_id, is_active=True).first()
        if category is None:
            raise ThreadError("Choose a category of this community.", "category")
    article = KnowledgeArticle.objects.create(
        community_id=thread.community_id,
        source_thread=thread,
        source_post=post,
        category=category or thread.category,
        equipment_id=thread.equipment_id,
        equipment_ref_id=thread.equipment_ref_id,
        title=title,
        summary=clean_plain(summary, max_length=600),
        body_html=html,
        body_text=text,
        author=user,
    )
    article.tags.set(resolve_tags(user, tags) if tags else [tt.tag for tt in thread.thread_tags.select_related("tag")])
    _revision(article, user, "Promoted from discussion")
    _audit(user, article, "article_promote", after={"thread_id": thread.pk, "post_id": post.pk if post else None})
    _refresh_search(article.pk)
    return article


def can_edit(user, article: KnowledgeArticle) -> bool:
    if article.status == ArticleStatus.ARCHIVED:
        return False
    if can_curate(user, article.community_id):
        return True
    return article.author_id == user.pk and article.status == ArticleStatus.DRAFT


@transaction.atomic
def edit(user, article: KnowledgeArticle, *, title=None, summary=None, body=None, tags=None, category_id=None, note: str = "") -> KnowledgeArticle:
    article = KnowledgeArticle.objects.select_for_update().get(pk=article.pk)
    if not can_edit(user, article):
        raise NotAllowed("You cannot edit this article.")
    changed = []
    if title is not None:
        t = clean_plain(title, max_length=200)
        if len(t) < TITLE_MIN:
            raise ThreadError(f"The title needs at least {TITLE_MIN} characters.", "title")
        if t != article.title:
            article.title = t
            changed.append("title")
    if summary is not None:
        s = clean_plain(summary, max_length=600)
        if s != article.summary:
            article.summary = s
            changed.append("summary")
    if body is not None:
        html, text = _clean_body(body)
        if html != article.body_html:
            article.body_html, article.body_text = html, text
            changed += ["body_html", "body_text"]
    if category_id is not None:
        category = DiscussionCategory.objects.filter(pk=category_id, community_id=article.community_id, is_active=True).first()
        if category is None:
            raise ThreadError("Choose a category of this community.", "category")
        if category.pk != article.category_id:
            article.category = category
            changed.append("category")
    if tags is not None:
        article.tags.set(resolve_tags(user, tags))
    if not changed:
        return article
    article.version = F("version") + 1
    article.save(update_fields=[*changed, "version", "updated_at"])
    article.refresh_from_db()
    _revision(article, user, note or "Edited")
    _audit(user, article, "article_edit", after={"version": article.version, "fields": changed}, reason=note)
    _refresh_search(article.pk)
    return article


@transaction.atomic
def submit(user, article: KnowledgeArticle) -> KnowledgeArticle:
    article = KnowledgeArticle.objects.select_for_update().get(pk=article.pk)
    if article.status != ArticleStatus.DRAFT:
        raise ThreadError("Only drafts can be sent for review.")
    if article.author_id != user.pk and not can_curate(user, article.community_id):
        raise NotAllowed("You cannot send this article for review.")
    article.status = ArticleStatus.UNDER_REVIEW
    article.save(update_fields=["status", "updated_at"])
    _audit(user, article, "article_submit")
    return article


@transaction.atomic
def review(user, article: KnowledgeArticle, decision: str, *, comment: str = "", institutional: bool = False) -> KnowledgeArticle:
    article = KnowledgeArticle.objects.select_for_update().get(pk=article.pk)
    if decision not in ReviewDecision.values:
        raise ThreadError("Choose approve, request changes or reject.", "decision")
    if not can_curate(user, article.community_id):
        raise NotAllowed("Only facility staff and moderators of this community can review articles.")
    if article.author_id == user.pk and not access.is_portal_admin(user):
        raise NotAllowed("Someone other than the author must review this article.")
    if article.status not in (ArticleStatus.UNDER_REVIEW, ArticleStatus.PUBLISHED):
        raise ThreadError("Send the article for review first.")
    comment = (comment or "").strip()[:4000]
    if decision != ReviewDecision.APPROVED and not comment:
        raise ThreadError("Explain what needs to change.", "comment")
    KnowledgeArticleReview.objects.create(article=article, reviewer=user, decision=decision, comment=comment, article_version=article.version)
    now = timezone.now()
    if decision == ReviewDecision.APPROVED:
        article.reviewer = user
        article.last_reviewed_at = now
        article.reviewed_version = article.version
        article.is_institutional_guidance = bool(institutional)
        fields = ["reviewer", "last_reviewed_at", "reviewed_version", "is_institutional_guidance", "updated_at"]
    else:
        article.status = ArticleStatus.DRAFT if article.status == ArticleStatus.UNDER_REVIEW else article.status
        fields = ["status", "updated_at"]
    article.save(update_fields=fields)
    _audit(user, article, f"article_review_{decision}", after={"version": article.version, "institutional": institutional}, reason=comment)
    return article


@transaction.atomic
def publish(user, article: KnowledgeArticle) -> KnowledgeArticle:
    article = KnowledgeArticle.objects.select_for_update().get(pk=article.pk)
    if not can_curate(user, article.community_id):
        raise NotAllowed("Only facility staff and moderators of this community can publish articles.")
    if article.status not in (ArticleStatus.UNDER_REVIEW, ArticleStatus.PUBLISHED):
        raise ThreadError("Only reviewed articles can be published.")
    if article.reviewed_version != article.version:
        raise ThreadError("An approved review of the current version is needed before publishing.")
    first = article.published_at is None
    article.status = ArticleStatus.PUBLISHED
    article.published_at = article.published_at or timezone.now()
    article.save(update_fields=["status", "published_at", "updated_at"])
    _audit(user, article, "article_publish", after={"version": article.version})
    if first:
        _notify_published(article, user)
    return article


@transaction.atomic
def archive(user, article: KnowledgeArticle, reason: str = "") -> KnowledgeArticle:
    article = KnowledgeArticle.objects.select_for_update().get(pk=article.pk)
    if not can_curate(user, article.community_id):
        raise NotAllowed("Only facility staff and moderators of this community can archive articles.")
    before = article.status
    article.status = ArticleStatus.ARCHIVED
    article.archived_at = timezone.now()
    article.save(update_fields=["status", "archived_at", "updated_at"])
    _audit(user, article, "article_archive", before={"status": before}, reason=reason)
    return article


@transaction.atomic
def restore(user, article: KnowledgeArticle) -> KnowledgeArticle:
    article = KnowledgeArticle.objects.select_for_update().get(pk=article.pk)
    if not can_curate(user, article.community_id) or article.status != ArticleStatus.ARCHIVED:
        raise NotAllowed("This article cannot be restored.")
    article.status = ArticleStatus.DRAFT
    article.archived_at = None
    article.save(update_fields=["status", "archived_at", "updated_at"])
    _audit(user, article, "article_restore")
    return article


def record_view(user, article: KnowledgeArticle) -> None:
    if article.status != ArticleStatus.PUBLISHED:
        return
    from django.core.cache import cache

    if cache.add(f"connect:article-view:{user.pk}:{article.pk}", 1, 1800):
        KnowledgeArticle.objects.filter(pk=article.pk).update(view_count=F("view_count") + 1)


def _notify_published(article, actor) -> None:
    try:
        from . import notifications
    except ImportError:
        return
    try:
        notifications.article_published(article, actor)
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning("IITR Connect: article notification failed", exc_info=True)

"""Accepted solutions and the Knowledge Hub API. Everything is loaded through ``access`` (404 when not readable)."""

from __future__ import annotations

from django.contrib.postgres.search import SearchQuery
from django.contrib.postgres.search import SearchRank
from django.db.models import F
from django.http import Http404
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from .. import access
from ..models import ArticleStatus
from ..models import DiscussionPost
from ..models import KnowledgeArticle
from ..models import KnowledgeArticleReview
from ..models import KnowledgeArticleRevision
from ..services import knowledge as ksvc
from ..services import solutions as ssvc
from ..services.threads import NotAllowed
from ..services.threads import ThreadError
from .base import ConnectAPIView
from .communities import ConnectPagination
from .presenters import annotate_cards
from .presenters import thread_detail
from .presenters import user_label
from .threads import _error
from .threads import _int

TAG = ["IITR Connect: knowledge"]


# --- Accepted solutions ---------------------------------------------------------------------------------------------


class ThreadSolutionView(ConnectAPIView):
    throttle_scope = "connect_write"

    @extend_schema(tags=TAG, summary="Accept or replace the solution (thread author or a moderator; audited)")
    def post(self, request, pk):
        thread = access.get_thread_or_404(request.user, pk)
        try:
            ssvc.accept(request.user, thread, _int(request.data.get("post_id")))
        except (ThreadError, NotAllowed) as exc:
            return _error(exc)
        return Response(_thread_out(request.user, pk))

    @extend_schema(tags=TAG, summary="Revoke the accepted solution (thread author or a moderator; audited)")
    def delete(self, request, pk):
        thread = access.get_thread_or_404(request.user, pk)
        try:
            ssvc.revoke(request.user, thread, str(request.data.get("reason") or ""))
        except (ThreadError, NotAllowed) as exc:
            return _error(exc)
        return Response(_thread_out(request.user, pk))


def _thread_out(user, pk):
    return thread_detail(annotate_cards(access.visible_threads(user).filter(pk=pk), user).get(), user)


# --- Articles -----------------------------------------------------------------------------------------------------------


def article_card(a: KnowledgeArticle, user) -> dict:
    return {
        "id": a.pk,
        "title": a.title,
        "summary": a.summary,
        "status": a.status,
        "community": {"id": a.community_id, "slug": a.community.slug, "name": a.community.name, "kind": a.community.kind},
        "category": {"id": a.category_id, "name": a.category.name} if a.category_id else None,
        "equipment": (
            {"id": a.equipment.equipment_id, "name": a.equipment.name, "code": a.equipment.code}
            if a.equipment_id and a.equipment is not None
            else ({"id": a.equipment_ref_id, "name": "", "code": ""} if a.equipment_ref_id else None)
        ),
        "tags": [{"slug": t.slug, "name": t.name} for t in a.tags.all()],
        "author": {"id": a.author_id, "name": user_label(a.author)},
        "reviewer": {"id": a.reviewer_id, "name": user_label(a.reviewer)} if a.reviewer_id else None,
        "is_institutional_guidance": ksvc.is_guidance(a),
        "needs_review": a.reviewed_version != a.version,
        "version": a.version,
        "view_count": a.view_count,
        "published_at": a.published_at,
        "last_reviewed_at": a.last_reviewed_at,
        "updated_at": a.updated_at,
        "source_thread_id": a.source_thread_id,
    }


def _articles(user):
    return access.visible_articles(user).select_related("community", "category", "equipment", "author", "reviewer").prefetch_related("tags")


def _article_or_404(user, pk) -> KnowledgeArticle:
    a = _articles(user).filter(pk=pk).first()
    if a is None:
        raise Http404
    return a


class ArticleListView(ConnectAPIView):
    def get_throttles(self):
        self.throttle_scope = "connect_write" if self.request.method == "POST" else "connect_read"
        return super().get_throttles()

    @extend_schema(tags=TAG, summary="Knowledge Hub articles the caller can read (published; drafts for authors and curators)")
    def get(self, request):
        user = request.user
        p = request.query_params
        qs = _articles(user)
        status = p.get("status") or "published"
        if status == "published":
            qs = qs.filter(status=ArticleStatus.PUBLISHED)
        elif status == "workflow":
            qs = qs.filter(status__in=[ArticleStatus.DRAFT, ArticleStatus.UNDER_REVIEW])
        elif status in ArticleStatus.values:
            qs = qs.filter(status=status)
        if p.get("community"):
            qs = qs.filter(community__slug=p.get("community"))
        equipment = _int(p.get("equipment"))
        if equipment:
            qs = qs.filter(equipment_ref_id=equipment)
        if p.get("tag"):
            qs = qs.filter(tags__slug=p.get("tag"))
        if p.get("guidance") in ("1", "true"):
            qs = qs.filter(is_institutional_guidance=True, reviewed_version=F("version"))
        q = (p.get("q") or "").strip()[:200]
        if q:
            query = SearchQuery(q, search_type="websearch", config="english")
            qs = qs.filter(search_vector=query).annotate(rank=SearchRank(F("search_vector"), query)).order_by("-rank", "-published_at")
        else:
            qs = qs.order_by(F("published_at").desc(nulls_last=True), "-updated_at")
        paginator = ConnectPagination()
        page = paginator.paginate_queryset(qs.distinct(), request, view=self)
        return paginator.get_paginated_response([article_card(a, user) for a in page])

    @extend_schema(tags=TAG, summary="Promote a discussion (and optionally one reply) to a draft article (curators)")
    def post(self, request):
        d = request.data
        thread = access.get_thread_or_404(request.user, _int(d.get("thread_id")))
        try:
            article = ksvc.promote(
                request.user,
                thread,
                post_id=_int(d.get("post_id")),
                title=d.get("title"),
                summary=d.get("summary") or "",
                body=d.get("body"),
                tags=d.get("tags"),
                category_id=_int(d.get("category_id")),
            )
        except (ThreadError, NotAllowed) as exc:
            return _error(exc)
        return Response(article_detail(_article_or_404(request.user, article.pk), request.user), status=201)


def article_detail(a: KnowledgeArticle, user) -> dict:
    data = article_card(a, user)
    curate = ksvc.can_curate(user, a.community_id)
    data["body_html"] = a.body_html
    source_post = None
    if a.source_post_id and access.visible_posts(user).filter(pk=a.source_post_id).exists():
        sp = DiscussionPost.objects.select_related("author").get(pk=a.source_post_id)
        source_post = {"id": sp.pk, "author": user_label(sp.author), "badge": sp.author_badge, "is_verified_response": sp.is_verified_response}
    data["source"] = {
        "thread_id": a.source_thread_id if a.source_thread_id and access.visible_threads(user).filter(pk=a.source_thread_id).exists() else None,
        "post": source_post,
    }
    data["permissions"] = {
        "can_edit": ksvc.can_edit(user, a),
        "can_submit": a.status == ArticleStatus.DRAFT and (a.author_id == user.pk or curate),
        # Only versions that still need a review (an approved current version is not offered again).
        "can_review": curate
        and a.status in (ArticleStatus.UNDER_REVIEW, ArticleStatus.PUBLISHED)
        and a.reviewed_version != a.version
        and (a.author_id != user.pk or access.is_portal_admin(user)),
        "can_publish": curate and a.status in (ArticleStatus.UNDER_REVIEW, ArticleStatus.PUBLISHED) and a.reviewed_version == a.version and a.status != ArticleStatus.PUBLISHED,
        "can_archive": curate and a.status != ArticleStatus.ARCHIVED,
        "can_restore": curate and a.status == ArticleStatus.ARCHIVED,
    }
    if curate or a.author_id == user.pk:
        data["reviews"] = [
            {"decision": r.decision, "comment": r.comment, "reviewer": user_label(r.reviewer), "version": r.article_version, "created_at": r.created_at}
            for r in KnowledgeArticleReview.objects.filter(article=a).select_related("reviewer")[:20]
        ]
    return data


class ArticleDetailView(ConnectAPIView):
    def get_throttles(self):
        self.throttle_scope = "connect_write" if self.request.method != "GET" else "connect_read"
        return super().get_throttles()

    @extend_schema(tags=TAG, summary="One article (404 unless the caller can read it)")
    def get(self, request, pk):
        a = _article_or_404(request.user, pk)
        ksvc.record_view(request.user, a)
        return Response(article_detail(_article_or_404(request.user, pk), request.user))

    @extend_schema(tags=TAG, summary="Edit an article (curators; authors while it is a draft). Creates a revision.")
    def patch(self, request, pk):
        a = _article_or_404(request.user, pk)
        d = request.data
        try:
            ksvc.edit(
                request.user, a, title=d.get("title"), summary=d.get("summary"), body=d.get("body"), tags=d.get("tags"),
                category_id=_int(d.get("category_id")) if d.get("category_id") else None, note=str(d.get("note") or ""),
            )
        except (ThreadError, NotAllowed) as exc:
            return _error(exc)
        return Response(article_detail(_article_or_404(request.user, pk), request.user))


class ArticleActionView(ConnectAPIView):
    throttle_scope = "connect_write"
    ACTIONS = ("submit", "review", "publish", "archive", "restore")

    @extend_schema(tags=TAG, summary="Workflow: submit, review {decision, comment, institutional}, publish, archive, restore")
    def post(self, request, pk, action):
        if action not in self.ACTIONS:
            raise Http404
        a = _article_or_404(request.user, pk)
        try:
            if action == "submit":
                ksvc.submit(request.user, a)
            elif action == "review":
                ksvc.review(
                    request.user, a, str(request.data.get("decision") or ""), comment=str(request.data.get("comment") or ""),
                    institutional=request.data.get("institutional") is True,
                )
            elif action == "publish":
                ksvc.publish(request.user, a)
            elif action == "archive":
                ksvc.archive(request.user, a, str(request.data.get("reason") or ""))
            else:
                ksvc.restore(request.user, a)
        except (ThreadError, NotAllowed) as exc:
            return _error(exc)
        return Response(article_detail(_article_or_404(request.user, pk), request.user))


class ArticleRevisionsView(ConnectAPIView):
    @extend_schema(tags=TAG, summary="Revision history (author and curators)")
    def get(self, request, pk):
        a = _article_or_404(request.user, pk)
        if a.author_id != request.user.pk and not ksvc.can_curate(request.user, a.community_id):
            raise Http404
        rows = KnowledgeArticleRevision.objects.filter(article=a).select_related("editor")[:50]
        return Response(
            {
                "results": [
                    {"version": r.version, "title": r.title, "summary": r.summary, "body_html": r.body_html, "status": r.status,
                     "editor": user_label(r.editor) if r.editor_id else "", "note": r.change_note, "created_at": r.created_at}
                    for r in rows
                ]
            }
        )


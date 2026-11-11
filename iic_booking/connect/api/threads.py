"""Threads, posts, reactions, follows, bookmarks, polls, drafts, categories, tags and attachments.

Every object is loaded through ``access`` (404 when the caller cannot read it). Writes additionally check posting
rights in the services (403 for readers who may not post: suspended, archived community, locked thread).
"""

from __future__ import annotations

from django.contrib.postgres.search import SearchQuery
from django.contrib.postgres.search import SearchRank
from django.db.models import Count
from django.db.models import F
from django.db.models import Q
from django.http import Http404
from django.utils.dateparse import parse_date
from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.parsers import FormParser
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response

from .. import access
from ..models import Community
from ..models import DiscussionAttachment
from ..models import DiscussionCategory
from ..models import DiscussionDraft
from ..models import DiscussionPost
from ..models import DiscussionPostRevision
from ..models import DiscussionTag
from ..models import ThreadTag
from ..models import ThreadType
from ..permissions import ConnectAdmin
from ..services import attachments as attachment_service
from ..services import threads as svc
from .base import ConnectAPIView
from .communities import ConnectPagination
from .presenters import annotate_cards
from .presenters import posts_payload
from .presenters import thread_card
from .presenters import thread_detail
from .presenters import user_label

TAG = ["IITR Connect: discussions"]
POSTS_PAGE = 50

SORTS = {
    "latest": ("-last_activity_at", "-id"),
    "new": ("-created_at", "-id"),
    "discussed": ("-reply_count", "-last_activity_at"),
    "viewed": ("-view_count", "-last_activity_at"),
    "helpful": ("-helpful_count", "-last_activity_at"),
    "unanswered": ("-created_at", "-id"),
    "solved": ("-solved_at", "-id"),
}


def _error(exc) -> Response:
    if isinstance(exc, svc.NotAllowed):
        return Response({"detail": str(exc)}, status=status.HTTP_403_FORBIDDEN)
    body = {"detail": str(exc)}
    if getattr(exc, "field", None):
        body["field"] = exc.field
    return Response(body, status=status.HTTP_400_BAD_REQUEST)


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _community_from(user, raw) -> Community:
    if raw in (None, ""):
        raise Http404
    if str(raw).isdigit():
        return access.get_community_or_404(user, pk=int(raw))
    return access.get_community_or_404(user, slug=str(raw))


def filtered_threads(request, *, apply_q: bool = True):
    """Thread list filters shared by the feed and search. Starts from the caller's visible threads."""
    user = request.user
    p = request.query_params
    qs = access.visible_threads(user)
    if p.get("community"):
        qs = qs.filter(community=_community_from(user, p.get("community")))
    scope = p.get("scope")
    if scope == "my_communities":
        qs = qs.filter(community__kind="group")
    elif scope == "general":
        qs = qs.filter(community__kind="general")
    if p.get("category"):
        qs = qs.filter(category__slug=p.get("category"))
    types = [t for t in (p.get("type") or "").split(",") if t in ThreadType.values]
    if types:
        qs = qs.filter(type__in=types)
    if p.get("tag"):
        qs = qs.filter(thread_tags__tag__slug=p.get("tag"))
    equipment_id = _int(p.get("equipment"))
    if equipment_id:
        qs = qs.filter(equipment_ref_id=equipment_id)
    author_id = _int(p.get("author"))
    if author_id:
        qs = qs.filter(author_id=author_id)
    if p.get("mine") in ("1", "true"):
        qs = qs.filter(author_id=user.pk)
    if p.get("bookmarked") in ("1", "true"):
        qs = qs.filter(bookmarks__user=user)
    if p.get("following") in ("1", "true"):
        qs = qs.filter(subscriptions__user=user, subscriptions__level="watching")
    state = p.get("status")
    if state == "solved":
        qs = qs.filter(solved_at__isnull=False)
    elif state == "unsolved":
        qs = qs.filter(type__in=list(svc.SOLVABLE_TYPES), solved_at__isnull=True)
    elif state == "unanswered" or p.get("sort") == "unanswered":
        qs = qs.filter(reply_count=0)
    if p.get("known_problem") in ("1", "true"):
        qs = qs.filter(is_known_problem=True)
    date_from = parse_date(p.get("from") or "")
    date_to = parse_date(p.get("to") or "")
    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)
    q = (p.get("q") or "").strip()[:200] if apply_q else ""
    rank = None
    if q:
        query = SearchQuery(q, search_type="websearch", config="english")
        qs = qs.filter(search_vector=query)
        rank = SearchRank(F("search_vector"), query)
    return qs.distinct(), rank


class ThreadListView(ConnectAPIView):
    def get_throttles(self):
        self.throttle_scope = "connect_post" if self.request.method == "POST" else "connect_read"
        return super().get_throttles()

    @extend_schema(tags=TAG, summary="Discussion feed (filters, sorts, pagination); only threads the caller can read")
    def get(self, request):
        qs, rank = filtered_threads(request)
        sort = request.query_params.get("sort") or "latest"
        order = list(SORTS.get(sort, SORTS["latest"]))
        if rank is not None and sort == "relevance":
            qs = qs.annotate(rank=rank)
            order = ["-rank", "-last_activity_at"]
        if request.query_params.get("community") and sort in ("latest", "relevance"):
            order = ["-is_pinned", *order]
        qs = annotate_cards(qs, request.user).order_by(*order)
        paginator = ConnectPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response([thread_card(t) for t in page])

    @extend_schema(tags=TAG, summary="Start a discussion")
    def post(self, request):
        d = request.data
        community = _community_from(request.user, d.get("community"))
        closes = parse_datetime(str(d.get("poll_closes_at") or "")) if d.get("poll_closes_at") else None
        try:
            thread = svc.create_thread(
                request.user,
                svc.ThreadInput(
                    community=community,
                    title=d.get("title"),
                    body=d.get("body"),
                    type=d.get("type") or ThreadType.GENERAL,
                    category_id=_int(d.get("category_id")),
                    equipment_id=d.get("equipment_id"),
                    tags=d.get("tags") or [],
                    attachments=d.get("attachments") or [],
                    poll_options=d.get("poll_options") or [],
                    poll_closes_at=closes,
                    follow=d.get("follow", True) is not False,
                ),
            )
        except (svc.ThreadError, svc.NotAllowed) as exc:
            return _error(exc)
        return Response(thread_detail(_annotated_thread(request.user, thread.pk), request.user), status=201)


def _annotated_thread(user, pk):
    access.get_thread_or_404(user, pk)
    return annotate_cards(access.visible_threads(user).filter(pk=pk), user).get()


class ThreadDetailView(ConnectAPIView):
    def get_throttles(self):
        self.throttle_scope = "connect_write" if self.request.method != "GET" else "connect_read"
        return super().get_throttles()

    @extend_schema(tags=TAG, summary="A thread with its opening post, poll and the caller's permissions")
    def get(self, request, pk):
        svc.record_view(request.user, access.get_thread_or_404(request.user, pk))
        return Response(thread_detail(_annotated_thread(request.user, pk), request.user))

    @extend_schema(tags=TAG, summary="Edit the thread (title, body, tags, category); author within the edit window or a moderator")
    def patch(self, request, pk):
        thread = access.get_thread_or_404(request.user, pk)
        opening = DiscussionPost.objects.select_related("thread", "thread__community").get(thread=thread, is_opening=True)
        d = request.data
        try:
            svc.edit_post(
                request.user,
                opening,
                body=d.get("body"),
                title=d.get("title"),
                tags=d.get("tags"),
                category_id=_int(d.get("category_id")) if d.get("category_id") else None,
                attachments=d.get("attachments"),
                reason=str(d.get("reason") or ""),
            )
        except (svc.ThreadError, svc.NotAllowed) as exc:
            return _error(exc)
        return Response(thread_detail(_annotated_thread(request.user, pk), request.user))

    @extend_schema(tags=TAG, summary="Delete your own thread (only while it has no replies)")
    def delete(self, request, pk):
        thread = access.get_thread_or_404(request.user, pk)
        opening = DiscussionPost.objects.select_related("thread", "thread__community").get(thread=thread, is_opening=True)
        try:
            svc.delete_own_post(request.user, opening)
        except svc.NotAllowed as exc:
            return _error(exc)
        return Response(status=204)


class ThreadPostsView(ConnectAPIView):
    def get_throttles(self):
        self.throttle_scope = "connect_post" if self.request.method == "POST" else "connect_read"
        return super().get_throttles()

    @extend_schema(tags=TAG, summary="Replies of a thread (oldest first, 50 per page)")
    def get(self, request, pk):
        thread = access.get_thread_or_404(request.user, pk)
        qs = (
            access.visible_posts(request.user)
            .filter(thread=thread, is_opening=False)
            .select_related("author")
            .order_by("created_at", "id")
        )
        paginator = ConnectPagination()
        paginator.page_size = POSTS_PAGE
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(posts_payload(list(page), request.user, thread))

    @extend_schema(tags=TAG, summary="Reply to the thread or to one of its posts")
    def post(self, request, pk):
        thread = access.get_thread_or_404(request.user, pk)
        d = request.data
        try:
            post = svc.create_reply(
                request.user, thread, d.get("body"), parent_id=_int(d.get("parent_id")), attachments=d.get("attachments") or []
            )
        except (svc.ThreadError, svc.NotAllowed) as exc:
            return _error(exc)
        post = DiscussionPost.objects.select_related("author").get(pk=post.pk)
        thread.refresh_from_db()
        return Response(posts_payload([post], request.user, thread)[0], status=201)


def _post_or_404(user, pk) -> DiscussionPost:
    post = access.visible_posts(user).select_related("thread", "thread__community", "author").filter(pk=pk).first()
    if post is None:
        raise Http404
    return post


class PostDetailView(ConnectAPIView):
    throttle_scope = "connect_write"

    @extend_schema(tags=TAG, summary="Edit a reply (author within the edit window, or a moderator with audit)")
    def patch(self, request, pk):
        post = _post_or_404(request.user, pk)
        if post.is_opening:
            return Response({"detail": "Edit the opening post through the thread."}, status=400)
        try:
            post = svc.edit_post(
                request.user, post, body=request.data.get("body"), attachments=request.data.get("attachments"),
                reason=str(request.data.get("reason") or ""),
            )
        except (svc.ThreadError, svc.NotAllowed) as exc:
            return _error(exc)
        post = DiscussionPost.objects.select_related("author").get(pk=post.pk)
        return Response(posts_payload([post], request.user, post.thread)[0])

    @extend_schema(tags=TAG, summary="Delete your own reply (while it has no replies)")
    def delete(self, request, pk):
        post = _post_or_404(request.user, pk)
        if post.is_opening:
            return Response({"detail": "Delete the thread instead."}, status=400)
        try:
            svc.delete_own_post(request.user, post)
        except svc.NotAllowed as exc:
            return _error(exc)
        return Response(status=204)


class PostRevisionsView(ConnectAPIView):
    @extend_schema(tags=TAG, summary="Edit history of a post (author and moderators)")
    def get(self, request, pk):
        post = _post_or_404(request.user, pk)
        if post.author_id != request.user.pk and not access.can_moderate(request.user, post.thread.community_id):
            raise Http404
        rows = DiscussionPostRevision.objects.filter(post=post).select_related("editor").order_by("-created_at")[:50]
        return Response(
            {
                "results": [
                    {
                        "id": r.pk,
                        "title": r.title,
                        "body_html": r.body_html,
                        "editor": user_label(r.editor) if r.editor_id else "",
                        "by_moderator": r.by_moderator,
                        "reason": r.reason,
                        "created_at": r.created_at,
                    }
                    for r in rows
                ]
            }
        )


class PostReactView(ConnectAPIView):
    throttle_scope = "connect_write"

    @extend_schema(tags=TAG, summary="Toggle a reaction (helpful, insightful, thanks) on someone else's post")
    def post(self, request, pk):
        post = _post_or_404(request.user, pk)
        try:
            return Response(svc.toggle_reaction(request.user, post, str(request.data.get("kind") or "")))
        except (svc.ThreadError, svc.NotAllowed) as exc:
            return _error(exc)


class ThreadBookmarkView(ConnectAPIView):
    throttle_scope = "connect_write"

    @extend_schema(tags=TAG, summary="Toggle bookmark")
    def post(self, request, pk):
        thread = access.get_thread_or_404(request.user, pk)
        return Response({"bookmarked": svc.toggle_bookmark(request.user, thread)})


class ThreadFollowView(ConnectAPIView):
    throttle_scope = "connect_write"

    @extend_schema(tags=TAG, summary="Follow (watching), mute or stop following a thread (level null)")
    def put(self, request, pk):
        thread = access.get_thread_or_404(request.user, pk)
        try:
            return Response({"level": svc.set_thread_follow(request.user, thread, request.data.get("level"))})
        except svc.ThreadError as exc:
            return _error(exc)


class CommunityFollowView(ConnectAPIView):
    throttle_scope = "connect_write"

    @extend_schema(tags=TAG, summary="Follow, mute or stop following a whole community")
    def put(self, request, slug):
        community = access.get_community_or_404(request.user, slug=slug)
        try:
            return Response({"level": svc.set_community_follow(request.user, community, request.data.get("level"))})
        except svc.ThreadError as exc:
            return _error(exc)


class ThreadVoteView(ConnectAPIView):
    throttle_scope = "connect_write"

    @extend_schema(tags=TAG, summary="Vote in a poll (changing your vote replaces it)")
    def post(self, request, pk):
        thread = access.get_thread_or_404(request.user, pk)
        try:
            return Response(svc.vote(request.user, thread, _int(request.data.get("option_id"))))
        except (svc.ThreadError, svc.NotAllowed) as exc:
            return _error(exc)


# --- Categories and tags ----------------------------------------------------------------------------------------


def _category_payload(c: DiscussionCategory) -> dict:
    return {
        "id": c.pk,
        "community_id": c.community_id,
        "slug": c.slug,
        "name": c.name,
        "description": c.description,
        "position": c.position,
        "is_active": c.is_active,
        "staff_only_posting": c.staff_only_posting,
    }


class CategoryListView(ConnectAPIView):
    def get_permissions(self):
        perms = super().get_permissions()
        if self.request.method == "POST":
            perms.append(ConnectAdmin())
        return perms

    @extend_schema(tags=TAG, summary="Categories of the readable communities (optionally one community)")
    def get(self, request):
        qs = DiscussionCategory.objects.filter(community_id__in=access.accessible_community_ids(request.user))
        if request.query_params.get("community"):
            qs = qs.filter(community=_community_from(request.user, request.query_params.get("community")))
        if not access.snapshot(request.user).is_admin:
            qs = qs.filter(is_active=True)
        return Response({"results": [_category_payload(c) for c in qs.order_by("community_id", "position", "name")]})

    @extend_schema(tags=TAG, summary="Create a category (Portal / Community Admin)")
    def post(self, request):
        from django.utils.text import slugify

        from ..sanitize import clean_plain

        community = _community_from(request.user, request.data.get("community"))
        name = clean_plain(request.data.get("name"), max_length=120)
        slug = slugify(request.data.get("slug") or name)[:80]
        if len(name) < 3 or not slug:
            return Response({"detail": "A category needs a name of at least 3 characters.", "field": "name"}, status=400)
        if DiscussionCategory.objects.filter(community=community, slug=slug).exists():
            return Response({"detail": "This community already has that category.", "field": "slug"}, status=400)
        c = DiscussionCategory.objects.create(
            community=community,
            name=name,
            slug=slug,
            description=clean_plain(request.data.get("description"), max_length=500),
            position=_int(request.data.get("position")) or 100,
            staff_only_posting=request.data.get("staff_only_posting") is True,
        )
        from ..models import ModerationAction

        ModerationAction.objects.create(
            actor=request.user, community=community, action="category_create", target_type="category", target_id=str(c.pk),
            after=_category_payload(c),
        )
        return Response(_category_payload(c), status=201)


class CategoryDetailView(ConnectAPIView):
    permission_classes = [*ConnectAPIView.permission_classes, ConnectAdmin]
    throttle_scope = "connect_write"

    @extend_schema(tags=TAG, summary="Rename, reorder, deactivate or restrict a category (Portal / Community Admin)")
    def patch(self, request, pk):
        from ..models import ModerationAction
        from ..sanitize import clean_plain

        c = DiscussionCategory.objects.filter(pk=pk, community_id__in=access.accessible_community_ids(request.user)).first()
        if c is None:
            raise Http404
        before = _category_payload(c)
        d = request.data
        if "name" in d:
            name = clean_plain(d.get("name"), max_length=120)
            if len(name) < 3:
                return Response({"detail": "A category needs a name of at least 3 characters.", "field": "name"}, status=400)
            c.name = name
        if "description" in d:
            c.description = clean_plain(d.get("description"), max_length=500)
        if "position" in d and _int(d.get("position")) is not None:
            c.position = _int(d.get("position"))
        if "is_active" in d:
            c.is_active = d.get("is_active") is True
        if "staff_only_posting" in d:
            c.staff_only_posting = d.get("staff_only_posting") is True
        c.save()
        ModerationAction.objects.create(
            actor=request.user, community=c.community, action="category_update", target_type="category", target_id=str(c.pk),
            before=before, after=_category_payload(c),
        )
        return Response(_category_payload(c))


class TagListView(ConnectAPIView):
    @extend_schema(tags=TAG, summary="Tags used in threads the caller can read, most used first (?q= prefix)")
    def get(self, request):
        threads = access.visible_threads(request.user).values("pk")
        qs = ThreadTag.objects.filter(thread_id__in=threads, tag__is_active=True)
        q = (request.query_params.get("q") or "").strip().lower()[:40]
        if q:
            qs = qs.filter(Q(tag__slug__startswith=q) | Q(tag__name__icontains=q))
        rows = qs.values("tag__slug", "tag__name").annotate(n=Count("thread_id", distinct=True)).order_by("-n", "tag__slug")[:50]
        return Response({"results": [{"slug": r["tag__slug"], "name": r["tag__name"], "count": r["n"]} for r in rows]})


class TagAdminView(ConnectAPIView):
    permission_classes = [*ConnectAPIView.permission_classes, ConnectAdmin]
    throttle_scope = "connect_write"

    @extend_schema(tags=TAG, summary="Rename or deactivate a tag (Portal / Community Admin)")
    def patch(self, request, slug):
        from ..models import ModerationAction
        from ..sanitize import clean_plain

        tag = DiscussionTag.objects.filter(slug=slug).first()
        if tag is None:
            raise Http404
        before = {"name": tag.name, "is_active": tag.is_active}
        if "name" in request.data:
            name = clean_plain(request.data.get("name"), max_length=40)
            if len(name) < 2:
                return Response({"detail": "Tag names need at least 2 characters."}, status=400)
            tag.name = name
        if "is_active" in request.data:
            tag.is_active = request.data.get("is_active") is True
        tag.save()
        ModerationAction.objects.create(
            actor=request.user, action="tag_update", target_type="tag", target_id=tag.slug,
            before=before, after={"name": tag.name, "is_active": tag.is_active},
        )
        return Response({"slug": tag.slug, "name": tag.name, "is_active": tag.is_active})


# --- Drafts -------------------------------------------------------------------------------------------------------


class DraftListView(ConnectAPIView):
    @extend_schema(tags=TAG, summary="Your autosaved drafts")
    def get(self, request):
        rows = DiscussionDraft.objects.filter(user=request.user).order_by("-updated_at")[:50]
        return Response({"results": [{"context_key": d.context_key, "payload": d.payload, "updated_at": d.updated_at} for d in rows]})


class DraftDetailView(ConnectAPIView):
    throttle_scope = "connect_write"

    @extend_schema(tags=TAG, summary="Get one draft")
    def get(self, request, key):
        try:
            key = svc.validate_context_key(key)
        except svc.ThreadError as exc:
            return _error(exc)
        draft = DiscussionDraft.objects.filter(user=request.user, context_key=key).first()
        if draft is None:
            raise Http404
        return Response({"context_key": draft.context_key, "payload": draft.payload, "updated_at": draft.updated_at})

    @extend_schema(tags=TAG, summary="Autosave a draft (title, body, type, tags, attachments...)")
    def put(self, request, key):
        try:
            draft = svc.save_draft(request.user, key, request.data.get("payload"))
        except svc.ThreadError as exc:
            return _error(exc)
        return Response({"context_key": draft.context_key, "updated_at": draft.updated_at})

    @extend_schema(tags=TAG, summary="Discard a draft")
    def delete(self, request, key):
        DiscussionDraft.objects.filter(user=request.user, context_key=str(key)[:80]).delete()
        return Response(status=204)


# --- Attachments ----------------------------------------------------------------------------------------------------


class AttachmentUploadView(ConnectAPIView):
    parser_classes = [MultiPartParser, FormParser]
    throttle_scope = "connect_upload"

    @extend_schema(tags=TAG, summary="Upload a file for a post (validated, stored privately); bind it by id when posting")
    def post(self, request):
        community = _community_from(request.user, request.data.get("community"))
        uploaded = request.FILES.get("file")
        if uploaded is None:
            return Response({"detail": "Choose a file.", "field": "file"}, status=400)
        try:
            a = attachment_service.upload(
                request.user, community, uploaded, inline=str(request.data.get("inline") or "").lower() in ("1", "true")
            )
        except (svc.ThreadError, svc.NotAllowed) as exc:
            return _error(exc)
        from .presenters import attachment_payload

        return Response(attachment_payload(a), status=201)


class AttachmentDetailView(ConnectAPIView):
    @extend_schema(tags=TAG, summary="Download an attachment (access re-checked on every request; never a public URL)")
    def get(self, request, pk):
        attachment = attachment_service.get_readable_attachment_or_404(request.user, pk)
        return attachment_service.download_response(attachment, inline=request.query_params.get("inline") in ("1", "true"))

    @extend_schema(tags=TAG, summary="Remove one of your uploads that is not attached to a post yet")
    def delete(self, request, pk):
        attachment = attachment_service.get_readable_attachment_or_404(request.user, pk)
        if attachment.post_id is not None or attachment.uploader_id != request.user.pk:
            return Response({"detail": "Attached files are removed by editing the post."}, status=400)
        from django.core.files.storage import default_storage

        default_storage.delete(attachment.storage_key)
        DiscussionAttachment.objects.filter(pk=attachment.pk).delete()
        return Response(status=204)


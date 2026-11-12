"""Global search and "find a solution before raising a query" suggestions. Both are permission-aware."""

from __future__ import annotations

from django.db.models import Count
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from .. import access
from ..services import search as search_service
from .base import ConnectAPIView
from .communities import ConnectPagination
from .presenters import annotate_cards
from .presenters import thread_card
from .threads import SORTS
from .threads import _int
from .threads import filtered_threads

TAG = ["IITR Connect: search"]


class SearchView(ConnectAPIView):
    throttle_scope = "connect_search"

    @extend_schema(
        tags=TAG,
        summary="Search titles, discussion text, replies, equipment, communities, tags and authors (readable threads only)",
    )
    def get(self, request):
        q = search_service.normalise_query(request.query_params.get("q") or "")
        base, _ = filtered_threads(request, apply_q=False)
        if not q:
            return Response({"count": 0, "next": None, "previous": None, "results": [], "facets": {}})
        qs = search_service.search_threads(request.user, q, base)
        sort = request.query_params.get("sort") or "relevance"
        order = ["-rank", "-last_activity_at"] if sort in ("relevance", "") else list(SORTS.get(sort, SORTS["latest"]))
        facets = {
            "types": dict(qs.order_by().values("type").annotate(n=Count("pk", distinct=True)).values_list("type", "n")),
            "solved": qs.filter(solved_at__isnull=False).values("pk").distinct().count(),
        }
        qs = annotate_cards(qs, request.user).order_by(*order)
        paginator = ConnectPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        previews = search_service.accepted_previews(request.user, page)
        openings = search_service.opening_texts([t.pk for t in page])
        results = []
        for t in page:
            card = thread_card(t)
            card["matched_in"], card["snippet_html"] = search_service.snippet_for(t, q, openings)
            card["accepted_answer"] = previews.get(t.pk)
            results.append(card)
        if (request.query_params.get("page") or "1") == "1":
            search_service.log_search(request.user, q, "search", paginator.page.paginator.count)
        response = paginator.get_paginated_response(results)
        response.data["facets"] = facets
        response.data["articles"] = search_service.search_articles(request.user, q) if (request.query_params.get("page") or "1") == "1" else []
        return response


class SuggestView(ConnectAPIView):
    throttle_scope = "connect_search"

    @extend_schema(
        tags=TAG,
        summary="Similar readable discussions for a draft title (advisory; never blocks posting)",
    )
    def get(self, request):
        title = request.query_params.get("title") or ""
        community_id = None
        if request.query_params.get("community"):
            community = access.visible_communities(request.user).filter(slug=request.query_params.get("community")).first()
            community_id = community.pk if community else None
        equipment_id = _int(request.query_params.get("equipment"))
        threads = search_service.suggest_similar(request.user, title, community_id=community_id, equipment_id=equipment_id)
        if not threads:
            if len(search_service.normalise_query(title)) >= 12:
                search_service.log_search(request.user, title, "suggest_none", 0)
            return Response({"results": []})
        cards = {t.pk: t for t in annotate_cards(access.visible_threads(request.user).filter(pk__in=[t.pk for t in threads]), request.user)}
        previews = search_service.accepted_previews(request.user, threads)
        results = []
        for t in threads:
            annotated = cards.get(t.pk)
            if annotated is None:
                continue
            card = thread_card(annotated)
            card["accepted_answer"] = previews.get(t.pk)
            results.append(card)
        return Response({"results": results})

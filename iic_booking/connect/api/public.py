"""Public preview of IITR Connect for visitors who are not signed in.

What a visitor may see (agreed 12 Nov 2026):

* The community directory: name, kind, description, member count and thread count of every active community that
  is not test-only.
* Recent thread titles from General Discussions only (title, category, reply count, last activity). No author,
  no post content, no equipment-group thread titles.

Opening a thread, posting and every other endpoint still need a signed-in user with access (``access``). The
preview answers 404, like every other Connect endpoint, when the module is off or limited to a pilot list.
"""

from __future__ import annotations

from django.db.models import Count
from django.db.models import Q
from django.http import Http404
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .. import access
from ..models import Community
from ..models import CommunityKind
from ..models import CommunityStatus
from ..models import DiscussionThread
from ..models import MembershipStatus
from ..models import Visibility
from .base import apply_private_headers

RECENT_TITLES = 12


def preview_enabled() -> bool:
    return access.feature_enabled() and not access.pilot_emails()


class PublicPreviewView(APIView):
    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "connect_public"

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        return apply_private_headers(response)

    @extend_schema(tags=["IITR Connect"], summary="Public preview: communities and General Discussions titles")
    def get(self, request):
        if not preview_enabled():
            raise Http404

        communities = (
            Community.objects.filter(status=CommunityStatus.ACTIVE, is_test_only=False)
            .annotate(
                member_count=Count(
                    "memberships", filter=Q(memberships__status=MembershipStatus.ACTIVE), distinct=True
                ),
                thread_count=Count("threads", filter=Q(threads__visibility=Visibility.VISIBLE), distinct=True),
            )
            .order_by("kind", "name")
        )
        rows = []
        general_id = None
        for c in communities:
            if c.kind == CommunityKind.GENERAL:
                general_id = c.pk
            rows.append(
                {
                    "slug": c.slug,
                    "name": c.name,
                    "kind": c.kind,
                    "description": c.description,
                    # General Discussions is open to every internal user; it has no member list to count.
                    "member_count": None if c.kind == CommunityKind.GENERAL else c.member_count,
                    "thread_count": c.thread_count,
                }
            )
        # General first, then equipment groups by name.
        rows.sort(key=lambda r: (r["kind"] != CommunityKind.GENERAL, r["name"].lower()))

        threads = []
        if general_id is not None:
            qs = (
                DiscussionThread.objects.filter(community_id=general_id, visibility=Visibility.VISIBLE)
                .select_related("category")
                .order_by("-is_pinned", "-last_activity_at")[:RECENT_TITLES]
            )
            threads = [
                {
                    "id": t.pk,
                    "title": t.title,
                    "category": t.category.name if t.category_id else "",
                    "reply_count": t.reply_count,
                    "last_activity_at": t.last_activity_at,
                    "is_pinned": t.is_pinned,
                }
                for t in qs
            ]
        return Response({"enabled": True, "communities": rows, "general_threads": threads})

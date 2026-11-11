"""Landing page sections and @mention suggestions. Everything is computed over what the caller can read."""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Count
from django.db.models import Q
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from .. import access
from ..models import CommunityMembership
from ..models import DiscussionReaction
from ..models import MembershipStatus
from ..models import ReactionKind
from ..models import ThreadType
from ..models import Visibility
from .base import ConnectAPIView
from .presenters import annotate_cards
from .presenters import thread_card
from .presenters import user_label

TAG = ["IITR Connect: home"]
SECTION = 6
TRENDING_DAYS = 14
CONTRIBUTOR_DAYS = 30


class HomeView(ConnectAPIView):
    @extend_schema(tags=TAG, summary="Landing page sections over the caller's readable threads")
    def get(self, request):
        user = request.user
        now = timezone.now()
        base = access.visible_threads(user).filter(visibility=Visibility.VISIBLE)

        def cards(qs, n=SECTION):
            return [thread_card(t) for t in annotate_cards(qs, user)[:n]]

        latest = cards(base.order_by("-last_activity_at", "-id"), 8)
        trending = cards(
            base.filter(last_activity_at__gte=now - timedelta(days=TRENDING_DAYS)).order_by(
                "-reply_count", "-helpful_count", "-view_count", "-last_activity_at"
            )
        )
        solved = cards(base.filter(solved_at__isnull=False).order_by("-solved_at"))
        technical = cards(
            base.filter(type__in=[ThreadType.TECHNICAL_ISSUE, ThreadType.QUESTION]).order_by("-last_activity_at")
        )
        announcement_qs = base.filter(type=ThreadType.ANNOUNCEMENT).filter(
            Q(announcement__isnull=True) | Q(announcement__expires_at__isnull=True) | Q(announcement__expires_at__gt=now)
        )
        announcements = cards(announcement_qs.order_by("-is_pinned", "-created_at"), 5)
        return Response(
            {
                "latest": latest,
                "trending": trending,
                "recently_solved": solved,
                "technical_support": technical,
                "announcements": announcements,
                "active_contributors": active_contributors(user, now),
            }
        )


def active_contributors(user, now, limit: int = 8) -> list[dict]:
    """People helping others in the caller's readable communities over the last 30 days.

    Ranked by helpful reactions received from other people and accepted answers, with replies as a tie-breaker, so
    posting volume alone does not rank anyone. Only visible posts in readable communities count.
    """
    since = now - timedelta(days=CONTRIBUTOR_DAYS)
    posts = access.visible_posts(user).filter(visibility=Visibility.VISIBLE, created_at__gte=since)
    replies = dict(posts.filter(is_opening=False).values("author_id").annotate(n=Count("pk")).values_list("author_id", "n"))
    helpful = dict(
        DiscussionReaction.objects.filter(post__in=posts, kind=ReactionKind.HELPFUL)
        .exclude(user_id=models_f("post__author_id"))
        .values("post__author_id")
        .annotate(n=Count("pk"))
        .values_list("post__author_id", "n")
    )
    accepted = dict(
        posts.filter(thread__accepted_post_id=models_f("pk")).values("author_id").annotate(n=Count("pk")).values_list("author_id", "n")
    )
    scores = {}
    for uid in set(replies) | set(helpful) | set(accepted):
        scores[uid] = (accepted.get(uid, 0) * 5 + helpful.get(uid, 0) * 2, replies.get(uid, 0))
    top = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    people = get_user_model().objects.in_bulk([uid for uid, _ in top])
    out = []
    for uid, (score, reply_count) in top:
        person = people.get(uid)
        if person is None or not person.is_active or (score == 0 and reply_count < 2):
            continue
        out.append(
            {
                "id": uid,
                "name": user_label(person),
                "accepted_answers": accepted.get(uid, 0),
                "helpful_received": helpful.get(uid, 0),
                "replies": reply_count,
            }
        )
    return out


def models_f(name):
    from django.db.models import F

    return F(name)


class MentionCandidatesView(ConnectAPIView):
    """People who can read the community, matching ``q`` by name. Nobody else is ever suggested."""

    @extend_schema(tags=TAG, summary="@mention suggestions limited to people who can read the community")
    def get(self, request, slug):
        community = access.get_community_or_404(request.user, slug=slug)
        q = (request.query_params.get("q") or "").strip()[:60]
        if len(q) < 2:
            return Response({"results": []})
        User = get_user_model()
        if community.is_general:
            from iic_booking.equipment.results_sharing_service import (
                INTERNAL_STUDENT_TYPES,
            )
            from iic_booking.users.models.department import DepartmentType

            qs = User.objects.filter(is_active=True, name__icontains=q).filter(
                Q(user_type__in=list(access.INTERNAL_STAFF_TYPES) + list(INTERNAL_STUDENT_TYPES))
                | Q(user_type="faculty", department__department_type=DepartmentType.INTERNAL)
            ).exclude(
                connect_memberships__community=community,
                connect_memberships__status__in=[MembershipStatus.REVOKED, MembershipStatus.LAPSED],
            )
        else:
            member_ids = CommunityMembership.objects.filter(
                community=community, status__in=[MembershipStatus.ACTIVE, MembershipStatus.SUSPENDED]
            ).values("user_id")
            qs = User.objects.filter(is_active=True, pk__in=member_ids, name__icontains=q)
        rows = list(qs.exclude(pk=request.user.pk).order_by("name").values("id", "name")[:8])
        # Final check per person keeps suggestions exact (test-only communities, pilot lists).
        out = []
        for row in rows:
            person = User.objects.get(pk=row["id"])
            if access.can_read_community(person, community.pk):
                out.append({"id": row["id"], "name": row["name"] or "Portal user", "badge": access.role_badge(person, community.pk)})
        return Response({"results": out})


"""Permission-aware analytics and operational insight for moderators and admins.

* Scope: the communities the caller moderates (Portal / Community Admins: all). Every figure is computed only over
  those communities' visible content; nothing about other communities is counted or named.
* Search insight (top searches, searches with no result) can reveal what people look for across communities, so it
  is shown to Portal / Community Admins only, and only for queries seen at least ``MIN_QUERY_COUNT`` times.
* Discussion volume is not equipment downtime: no figure here is labelled or used as availability.
"""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Count
from django.db.models import Max
from django.db.models import Q
from django.db.models.functions import TruncWeek
from django.utils import timezone

from .. import access
from ..models import ArticleStatus
from ..models import Community
from ..models import CommunityMembership
from ..models import ConnectSearchLog
from ..models import DiscussionPost
from ..models import DiscussionReport
from ..models import DiscussionThread
from ..models import KnowledgeArticle
from ..models import MembershipStatus
from ..models import ReportStatus
from ..models import ThreadType
from ..models import Visibility

MIN_QUERY_COUNT = 3
NOTE = "Discussion activity shows where users need help. It is not a measure of equipment downtime or availability."


def scope_ids(user, community_id=None) -> list[int]:
    ids = set(access.snapshot(user).moderated)
    if community_id:
        ids &= {int(community_id)}
    return sorted(ids)


def build(user, community_id=None, days: int = 90) -> dict:
    ids = scope_ids(user, community_id)
    snap = access.snapshot(user)
    now = timezone.now()
    since = now - timedelta(days=days)
    threads = DiscussionThread.objects.filter(community_id__in=ids, visibility=Visibility.VISIBLE)
    posts = DiscussionPost.objects.filter(thread__in=threads, visibility=Visibility.VISIBLE)
    solvable = threads.filter(type__in=[ThreadType.QUESTION, ThreadType.TECHNICAL_ISSUE])
    totals = {
        "communities": len(ids),
        "members": CommunityMembership.objects.filter(community_id__in=ids, status=MembershipStatus.ACTIVE).count(),
        "threads": threads.count(),
        "active_threads_30d": threads.filter(last_activity_at__gte=now - timedelta(days=30)).count(),
        "replies": posts.filter(is_opening=False).count(),
        "unanswered": threads.filter(reply_count=0).count(),
        "open_questions": solvable.filter(solved_at__isnull=True).count(),
        "accepted_answers": solvable.filter(solved_at__isnull=False).count(),
        "resolved_by_asker": solvable.filter(resolved_by_author_at__isnull=False).count(),
        "articles_published": KnowledgeArticle.objects.filter(community_id__in=ids, status=ArticleStatus.PUBLISHED).count(),
        "open_reports": DiscussionReport.objects.filter(post__thread__community_id__in=ids, status=ReportStatus.OPEN).count(),
    }
    names = dict(Community.objects.filter(pk__in=ids).values_list("pk", "name"))

    most_active = (
        posts.filter(created_at__gte=since).values("thread__community_id").annotate(n=Count("pk")).order_by("-n")[:10]
    )
    trends = (
        threads.filter(created_at__gte=now - timedelta(weeks=12))
        .annotate(week=TruncWeek("created_at"))
        .values("week")
        .annotate(threads=Count("pk"), solved=Count("pk", filter=Q(solved_at__isnull=False)))
        .order_by("week")
    )
    reply_trend = dict(
        posts.filter(is_opening=False, created_at__gte=now - timedelta(weeks=12))
        .annotate(week=TruncWeek("created_at"))
        .values("week")
        .annotate(n=Count("pk"))
        .values_list("week", "n")
    )

    equipment_rows = (
        threads.filter(created_at__gte=since, equipment_ref_id__isnull=False)
        .values("equipment_ref_id")
        .annotate(
            threads=Count("pk"),
            issues=Count("pk", filter=Q(type=ThreadType.TECHNICAL_ISSUE)),
            known=Count("pk", filter=Q(is_known_problem=True)),
            open=Count("pk", filter=Q(type__in=[ThreadType.QUESTION, ThreadType.TECHNICAL_ISSUE], solved_at__isnull=True)),
        )
        .order_by("-threads")[:10]
    )
    from iic_booking.equipment.models import Equipment

    eq_names = dict(Equipment.objects.filter(pk__in=[r["equipment_ref_id"] for r in equipment_rows]).values_list("pk", "name"))
    recurring = [
        {"equipment_id": r["equipment_ref_id"], "name": eq_names.get(r["equipment_ref_id"], f"Equipment {r['equipment_ref_id']}"),
         "technical_issues": r["issues"], "known_problems": r["known"], "open": r["open"]}
        for r in equipment_rows
        if r["issues"] >= 2 or r["known"]
    ]

    unanswered_q = solvable.filter(solved_at__isnull=True).order_by("-view_count", "created_at")[:10]
    useful = (
        posts.filter(pk__in=threads.filter(accepted_post__isnull=False).values("accepted_post_id"))
        .select_related("thread", "author")
        .order_by("-helpful_count", "-created_at")[:10]
    )
    articles = KnowledgeArticle.objects.filter(community_id__in=ids, status=ArticleStatus.PUBLISHED).order_by("-view_count")[:10]
    low = []
    if snap.is_admin:
        recent = dict(threads.filter(created_at__gte=since).values("community_id").annotate(n=Count("pk")).values_list("community_id", "n"))
        members = dict(
            CommunityMembership.objects.filter(community_id__in=ids, status=MembershipStatus.ACTIVE)
            .values("community_id")
            .annotate(n=Count("pk"))
            .values_list("community_id", "n")
        )
        low = sorted(
            (
                {"community_id": cid, "name": names.get(cid, ""), "members": members.get(cid, 0), "threads_90d": recent.get(cid, 0)}
                for cid in ids
                if members.get(cid, 0) >= 10 and recent.get(cid, 0) <= 1
            ),
            key=lambda r: (-r["members"], r["name"]),
        )[:10]

    def search_rows(kind):
        qs = ConnectSearchLog.objects.filter(kind=kind, created_at__gte=since).values("query").annotate(n=Count("pk"), last=Max("created_at"))
        return [{"query": r["query"], "count": r["n"]} for r in qs.filter(n__gte=MIN_QUERY_COUNT).order_by("-n")[:15]]

    return {
        "scope": {"community_ids": ids, "days": days, "is_admin": snap.is_admin},
        "note": NOTE,
        "totals": totals,
        "most_active_communities": [{"community_id": r["thread__community_id"], "name": names.get(r["thread__community_id"], ""), "posts": r["n"]} for r in most_active],
        "trends": [
            {"week": r["week"].date().isoformat(), "threads": r["threads"], "solved": r["solved"], "replies": reply_trend.get(r["week"], 0)}
            for r in trends
        ],
        "most_discussed_equipment": [
            {"equipment_id": r["equipment_ref_id"], "name": eq_names.get(r["equipment_ref_id"], f"Equipment {r['equipment_ref_id']}"), "threads": r["threads"]}
            for r in equipment_rows
        ],
        "recurring_problems": recurring,
        "frequently_unanswered": [
            {"thread_id": t.pk, "title": t.title, "views": t.view_count, "created_at": t.created_at, "community_id": t.community_id}
            for t in unanswered_q
        ],
        "useful_solutions": [
            {"thread_id": p.thread_id, "title": p.thread.title, "helpful": p.helpful_count, "author": (p.author.name or "").strip()}
            for p in useful
        ],
        "most_viewed_articles": [{"article_id": a.pk, "title": a.title, "views": a.view_count} for a in articles],
        "low_participation_groups": low,
        "top_searches": search_rows("search") if snap.is_admin else [],
        "searches_without_answers": search_rows("suggest_none") if snap.is_admin else [],
    }


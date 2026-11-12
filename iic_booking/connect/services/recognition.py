"""Optional, restrained recognition badges (``ConnectSettings.recognition_enabled``, off by default).

Badges are earned only from outcomes that other people confirm, never from posting volume:

* accepted answers: replies accepted as the solution by someone else (self-accepts never count);
* helpful: the number of *distinct* people who marked the user's posts helpful (one person counts once however
  many reactions they give; reacting to your own post is impossible);
* approved articles: published Knowledge Hub articles the user wrote that a different person reviewed.

Only visible content counts, so hidden or removed posts stop counting. Badges are recomputed nightly and withdrawn
(``revoked_at``) if the criteria stop being met.
"""

from __future__ import annotations

from collections import defaultdict

from django.db.models import Count
from django.db.models import F
from django.utils import timezone

from ..models import ArticleStatus
from ..models import ConnectSettings
from ..models import DiscussionReaction
from ..models import DiscussionThread
from ..models import KnowledgeArticle
from ..models import ReactionKind
from ..models import RecognitionBadge
from ..models import UserBadgeAward
from ..models import Visibility


def enabled() -> bool:
    return bool(ConnectSettings.load().recognition_enabled)


def stats(user_ids=None) -> dict[int, dict]:
    accepted = (
        DiscussionThread.objects.filter(visibility=Visibility.VISIBLE, accepted_post__isnull=False, accepted_post__visibility=Visibility.VISIBLE)
        .exclude(accepted_solution__accepted_by_id=F("accepted_post__author_id"))
        .values("accepted_post__author_id", "community_id", "equipment_ref_id")
    )
    helpful = (
        DiscussionReaction.objects.filter(kind=ReactionKind.HELPFUL, post__visibility=Visibility.VISIBLE)
        .exclude(user_id=F("post__author_id"))
        .values("post__author_id")
        .annotate(n=Count("user_id", distinct=True))
    )
    articles = (
        KnowledgeArticle.objects.filter(status=ArticleStatus.PUBLISHED, reviewer__isnull=False)
        .exclude(reviewer_id=F("author_id"))
        .values("author_id")
        .annotate(n=Count("pk"))
    )
    if user_ids is not None:
        ids = list(user_ids)
        accepted = accepted.filter(accepted_post__author_id__in=ids)
        helpful = helpful.filter(post__author_id__in=ids)
        articles = articles.filter(author_id__in=ids)
    out: dict[int, dict] = defaultdict(lambda: {"accepted_answers": 0, "communities": set(), "per_equipment": defaultdict(int),
                                                "distinct_helpful_users": 0, "approved_articles": 0})
    for row in accepted:
        s = out[row["accepted_post__author_id"]]
        s["accepted_answers"] += 1
        s["communities"].add(row["community_id"])
        if row["equipment_ref_id"]:
            s["per_equipment"][row["equipment_ref_id"]] += 1
    for row in helpful:
        out[row["post__author_id"]]["distinct_helpful_users"] = row["n"]
    for row in articles:
        out[row["author_id"]]["approved_articles"] = row["n"]
    final = {}
    for uid, s in out.items():
        final[uid] = {
            "accepted_answers": s["accepted_answers"],
            "communities": len(s["communities"]),
            "accepted_answers_on_one_equipment": max(s["per_equipment"].values(), default=0),
            "distinct_helpful_users": s["distinct_helpful_users"],
            "approved_articles": s["approved_articles"],
        }
    return final


def meets(criteria: dict, s: dict) -> bool:
    return all(s.get(key, 0) >= int(value) for key, value in (criteria or {}).items())


def recompute(user_ids=None) -> dict:
    """Award and withdraw badges. Returns counts. Does nothing while recognition is switched off."""
    if not enabled():
        return {"enabled": False, "awarded": 0, "withdrawn": 0}
    badges = list(RecognitionBadge.objects.filter(is_active=True))
    all_stats = stats(user_ids)
    current = {(a.user_id, a.badge_id): a for a in UserBadgeAward.objects.filter(community__isnull=True)}
    awarded = withdrawn = 0
    now = timezone.now()
    users = set(all_stats) | ({uid for uid, _ in current} if user_ids is None else set(user_ids))
    for uid in users:
        s = all_stats.get(uid, {})
        for badge in badges:
            ok = meets(badge.criteria, s)
            award = current.get((uid, badge.pk))
            if ok and (award is None or award.revoked_at):
                if award is None:
                    UserBadgeAward.objects.create(user_id=uid, badge=badge, evidence=s)
                else:
                    UserBadgeAward.objects.filter(pk=award.pk).update(revoked_at=None, evidence=s, awarded_at=now)
                awarded += 1
            elif not ok and award is not None and not award.revoked_at:
                UserBadgeAward.objects.filter(pk=award.pk).update(revoked_at=now)
                withdrawn += 1
    return {"enabled": True, "awarded": awarded, "withdrawn": withdrawn}


def badges_for(user_ids) -> dict[int, list[dict]]:
    if not enabled():
        return {}
    out: dict[int, list[dict]] = defaultdict(list)
    for a in UserBadgeAward.objects.filter(user_id__in=list(user_ids), revoked_at__isnull=True, badge__is_active=True).select_related("badge"):
        out[a.user_id].append({"code": a.badge.code, "name": a.badge.name, "description": a.badge.description})
    return out


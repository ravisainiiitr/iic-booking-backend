"""Permission-aware search for IITR Connect (PostgreSQL full-text search; no extra infrastructure).

* Every query starts from ``access.visible_threads`` / ``visible_posts`` / ``visible_articles``, so hidden, removed,
  deleted or other-group content can never match, count, rank or appear in a snippet.
* ``search_threads``: global search over titles, opening posts, tags (thread vector), reply text (post vectors of
  *visible* replies only), equipment names, community names and author names.
* ``suggest_similar``: "Find a solution before raising a query". Title words are OR-combined so partial overlaps
  still surface, ranked by text similarity with boosts for the same equipment / community and for solved threads.
  It is advisory only and never blocks posting.
* The similarity step sits behind ``SimilarityBackend`` so a semantic (embedding) backend can be added later
  without changing callers (``IITR_CONNECT_SIMILARITY_BACKEND``).
* Snippets use ``ts_headline`` with private markers that are HTML-escaped before ``<mark>`` is applied, so user
  text can never inject markup through a highlight.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Protocol

from django.conf import settings
from django.contrib.postgres.search import SearchQuery
from django.contrib.postgres.search import SearchRank
from django.db.models import Exists
from django.db.models import F
from django.db.models import FloatField
from django.db.models import OuterRef
from django.db.models import Q
from django.db.models import Subquery
from django.db.models import Value
from django.db.models.functions import Coalesce
from django.db.models.functions import Greatest

from .. import access
from ..models import ConnectSearchLog
from ..models import DiscussionPost
from ..models import Visibility

CONFIG = "english"
MAX_QUERY = 200
# Highlight markers: unusual printable brackets (stripped from source text first), escaped before <mark> is applied.
_START, _STOP = "⟦⟦", "⟧⟧"
_WORD = re.compile(r"[\w\-]{3,}", re.U)
STOPWORDS = frozenset(
    "the and for with from that this have has was were are not but how what why when where which who after before "
    "into onto about using use used can could should would does did any all our your their there here please help "
    "issue problem question".split()
)


def normalise_query(q: str) -> str:
    return " ".join((q or "").split())[:MAX_QUERY]


def _snippet(raw: str | None) -> str:
    if not raw:
        return ""
    escaped = html.escape(raw, quote=True)
    return escaped.replace(_START, "<mark>").replace(_STOP, "</mark>")


def log_search(user, query: str, kind: str, result_count: int) -> None:
    q = normalise_query(query).lower()
    if len(q) < 2:
        return
    ConnectSearchLog.objects.create(user=user if getattr(user, "pk", None) else None, query=q[:200], kind=kind, result_count=result_count)


# ---------------------------------------------------------------------------
# Global search
# ---------------------------------------------------------------------------


def search_threads(user, q: str, base_qs=None):
    """Annotated queryset of readable threads matching ``q`` (with ``rank``, ``matched_in`` and ``snippet_raw``)."""
    q = normalise_query(q)
    qs = base_qs if base_qs is not None else access.visible_threads(user)
    if not q:
        return qs.none()
    query = SearchQuery(q, search_type="websearch", config=CONFIG)
    replies = access.visible_posts(user).filter(
        thread_id=OuterRef("pk"), is_opening=False, visibility=Visibility.VISIBLE, search_vector=query
    )
    reply_rank = Subquery(
        replies.annotate(r=SearchRank(F("search_vector"), query)).order_by("-r").values("r")[:1], output_field=FloatField()
    )
    reply_text = Subquery(
        replies.annotate(r=SearchRank(F("search_vector"), query)).order_by("-r").values("body_text")[:1]
    )
    like = q[:80]
    name_match = (
        Q(equipment__name__icontains=like)
        | Q(equipment__code__iexact=like)
        | Q(author__name__icontains=like)
        | Q(thread_tags__tag__name__icontains=like)
        | Q(community__name__icontains=like)
    )
    qs = (
        qs.annotate(_reply_hit=Exists(replies))
        .filter(Q(search_vector=query) | Q(_reply_hit=True) | name_match)
        .annotate(
            _text_rank=Coalesce(SearchRank(F("search_vector"), query), Value(0.0)),
            _reply_rank=Coalesce(reply_rank, Value(0.0)),
        )
        .annotate(rank=Greatest(F("_text_rank"), F("_reply_rank") * Value(0.8)))
        .annotate(_reply_text=reply_text)
        .distinct()
    )
    return qs


def _headline(text: str, q: str) -> str:
    from django.db import connection

    # Marker characters that may already be in the text are removed so they can never fake a highlight.
    clean = (text or "")[:20000].replace("⟦", "").replace("⟧", "")
    options = f"StartSel={_START}, StopSel={_STOP}, MaxWords=28, MinWords=12, ShortWord=2, MaxFragments=2"
    with connection.cursor() as cur:
        cur.execute("SELECT ts_headline(%s::regconfig, %s, websearch_to_tsquery(%s::regconfig, %s), %s)", [CONFIG, clean, CONFIG, q, options])
        return _snippet(cur.fetchone()[0])


def opening_texts(thread_ids) -> dict[int, str]:
    """Opening-post text of several threads in one query (for snippets)."""
    return dict(DiscussionPost.objects.filter(thread_id__in=list(thread_ids), is_opening=True).values_list("thread_id", "body_text"))


def snippet_for(thread, q: str, openings: dict[int, str] | None = None) -> tuple[str, str]:
    """(matched_in, safe HTML snippet) for one result. Uses only text the caller can already read."""
    q = normalise_query(q)
    text_rank = float(getattr(thread, "_text_rank", 0) or 0)
    reply_rank = float(getattr(thread, "_reply_rank", 0) or 0)
    if text_rank and text_rank >= reply_rank * 0.8:
        body = (openings or {}).get(thread.pk)
        if body is None:
            body = opening_texts([thread.pk]).get(thread.pk, "")
        return "discussion", _headline(body or "", q)
    reply = getattr(thread, "_reply_text", None)
    if reply:
        return "reply", _headline(reply, q)
    return "details", ""


# ---------------------------------------------------------------------------
# Find a solution before raising a query
# ---------------------------------------------------------------------------


@dataclass
class Suggestion:
    thread: object
    score: float


class SimilarityBackend(Protocol):
    def similar(self, user, title: str, *, community_id: int | None, equipment_id: int | None, limit: int) -> list[Suggestion]: ...


def _title_query(title: str) -> SearchQuery | None:
    words = [w.lower() for w in _WORD.findall(normalise_query(title)) if w.lower() not in STOPWORDS][:10]
    if not words:
        return None
    query = None
    for w in dict.fromkeys(words):
        part = SearchQuery(w, search_type="plain", config=CONFIG)
        query = part if query is None else query | part
    return query


class PostgresFullTextBackend:
    """OR-combined title words against the thread vector (title > tags > body)."""

    def similar(self, user, title, *, community_id=None, equipment_id=None, limit=6):
        query = _title_query(title)
        if query is None:
            return []
        qs = (
            access.visible_threads(user)
            .filter(visibility=Visibility.VISIBLE, search_vector=query)
            .annotate(text_rank=SearchRank(F("search_vector"), query))
            .order_by("-text_rank")[:40]
        )
        out = []
        for t in qs:
            score = float(t.text_rank)
            if equipment_id and t.equipment_ref_id == equipment_id:
                score *= 1.6
            if community_id and t.community_id == community_id:
                score *= 1.25
            if t.solved_at:
                score *= 1.35
            out.append(Suggestion(t, score))
        out.sort(key=lambda s: s.score, reverse=True)
        return out[:limit]


_BACKENDS: dict[str, type] = {"postgres": PostgresFullTextBackend}


def similarity_backend() -> SimilarityBackend:
    """Configured backend (``IITR_CONNECT_SIMILARITY_BACKEND``); unknown names fall back to PostgreSQL."""
    name = getattr(settings, "IITR_CONNECT_SIMILARITY_BACKEND", "postgres") or "postgres"
    return _BACKENDS.get(name, PostgresFullTextBackend)()


def suggest_similar(user, title: str, *, community_id=None, equipment_id=None, limit: int = 6) -> list:
    return [s.thread for s in similarity_backend().similar(user, title, community_id=community_id, equipment_id=equipment_id, limit=limit)]


def accepted_previews(user, threads) -> dict[int, dict]:
    """Accepted answer preview per thread: only when the caller can read that post."""
    ids = [t.accepted_post_id for t in threads if t.accepted_post_id]
    if not ids:
        return {}
    posts = access.visible_posts(user).filter(pk__in=ids, visibility=Visibility.VISIBLE).select_related("author")
    out = {}
    by_post = {p.pk: p for p in posts}
    for t in threads:
        p = by_post.get(t.accepted_post_id)
        if p is None:
            continue
        text = " ".join((p.body_text or "").split())
        out[t.pk] = {
            "post_id": p.pk,
            "excerpt": text if len(text) <= 220 else text[:220].rsplit(" ", 1)[0] + "…",
            "author": (getattr(p.author, "name", "") or "").strip() or "Portal user",
            "badge": p.author_badge,
            "resolution_hours": round((t.solved_at - t.created_at).total_seconds() / 3600, 1) if t.solved_at else None,
        }
    return out


def search_articles(user, q: str, limit: int = 5) -> list[dict]:
    """Published Knowledge Hub articles the caller can read that match ``q``."""
    from ..models import ArticleStatus
    from .knowledge import is_guidance

    q = normalise_query(q)
    if not q:
        return []
    query = SearchQuery(q, search_type="websearch", config=CONFIG)
    rows = (
        access.visible_articles(user)
        .filter(status=ArticleStatus.PUBLISHED, search_vector=query)
        .select_related("community")
        .annotate(rank=SearchRank(F("search_vector"), query))
        .order_by("-rank")[:limit]
    )
    return [
        {"id": a.pk, "title": a.title, "summary": a.summary, "community": a.community.name, "is_institutional_guidance": is_guidance(a)}
        for a in rows
    ]

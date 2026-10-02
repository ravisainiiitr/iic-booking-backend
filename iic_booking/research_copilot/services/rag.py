"""Hybrid RAG retrieval pipeline (Phase AI.2).

User Question → Intent → Permission → Structured + Vector + Keyword → Re-rank → Citations
"""

from __future__ import annotations

import copy
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field

from django.db.models import Q

from iic_booking.research_copilot.models import (
    DocumentStatus,
    KnowledgeChunk,
    KnowledgeDocument,
    SearchQueryLog,
)
from iic_booking.research_copilot.services.embeddings import get_embedding_provider
from iic_booking.research_copilot.services.intent import detect_intent
from iic_booking.research_copilot.services.knowledge_permissions import allowed_security_levels
from iic_booking.research_copilot.services.structured_search import structured_search
from iic_booking.research_copilot.services.vector_store import get_vector_store

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9]{3,}", re.I)

# Words that appear in most portal articles; matching them says nothing about relevance.
_KEYWORD_STOPWORDS = {
    "the", "and", "for", "what", "how", "does", "can", "should", "with", "this", "that", "from", "are",
    "you", "your", "about", "tell", "please", "there", "which", "when", "where", "who", "why", "have",
    "has", "into", "its", "any", "per", "list", "show", "recent", "latest", "booking", "bookings",
    "portal", "want", "need", "get", "give", "all", "mine", "now", "today", "help", "iic", "kindly",
}
_UNSAFE_URL_PREFIXES = ("/admin", "/api/", "/django-admin")


def safe_citation_url(url: str | None) -> str:
    """A link the chat may render: a portal path or http(s). Internal URIs (seed://, s3://, admin pages) are dropped."""
    u = (url or "").strip()
    if not u:
        return ""
    low = u.lower()
    if low.startswith(("https://", "http://")):
        return u
    if u.startswith("/") and not u.startswith("//") and not low.startswith(_UNSAFE_URL_PREFIXES):
        return u
    return ""


def _query_tokens(query: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall((query or "").lower()) if t not in _KEYWORD_STOPWORDS]


@dataclass
class Citation:
    source_id: str
    title: str
    snippet: str
    score: float = 0.0
    url: str = ""
    category: str = ""
    source_type: str = "document"  # document|equipment|status|policy


@dataclass
class RetrievalResult:
    citations: list[Citation] = field(default_factory=list)
    intent: str = "general"
    latency_ms: int = 0
    low_confidence: bool = False
    context_block: str = ""


def _keyword_search(
    *,
    query: str,
    allowed_levels: set[str],
    department_id: int | None,
    limit: int = 8,
    equipment_id: int | None = None,
) -> list[Citation]:
    tokens = _query_tokens(query)
    if not tokens:
        return []
    q_obj = Q()
    for tok in tokens[:8]:
        q_obj |= Q(content__icontains=tok) | Q(document__title__icontains=tok) | Q(document__tags__icontains=tok)

    qs = KnowledgeChunk.objects.select_related("document").filter(
        q_obj,
        document__status=DocumentStatus.ACTIVE,
        document__security_level__in=list(allowed_levels),
    )
    if department_id is not None:
        qs = qs.filter(Q(document__department_id__isnull=True) | Q(document__department_id=department_id))
    if equipment_id is not None:
        qs = qs.filter(document__equipment_id=int(equipment_id))

    hits: list[Citation] = []
    for chunk in qs.order_by("chunk_index")[:40]:
        doc = chunk.document
        # Simple keyword density score
        lower = chunk.content.lower()
        score = sum(1 for t in tokens if t in lower) / max(len(tokens), 1)
        hits.append(
            Citation(
                source_id=str(doc.id),
                title=doc.title,
                snippet=chunk.content[:400],
                score=0.3 + 0.5 * score,
                url=safe_citation_url(doc.external_url) or safe_citation_url(doc.source_uri),
                category=doc.category,
                source_type="document",
            )
        )
    hits.sort(key=lambda c: c.score, reverse=True)
    return hits[:limit]


def _role_adjust(candidates: list[Citation], role_bucket: str) -> None:
    """Prefer articles tagged for the asker's role (tags like "role:faculty"); demote ones written for other roles."""
    import uuid

    ids = set()
    for c in candidates:
        try:
            ids.add(uuid.UUID(str(c.source_id)))
        except (ValueError, TypeError):
            continue
    if not ids:
        return
    try:
        tag_map = {str(d["id"]): d["tags"] or [] for d in KnowledgeDocument.objects.filter(id__in=ids).values("id", "tags")}
    except Exception:  # noqa: BLE001
        return
    mine = f"role:{role_bucket}"
    for c in candidates:
        roles = {t for t in tag_map.get(str(c.source_id), []) if isinstance(t, str) and t.startswith("role:")}
        if not roles:
            continue
        c.score = min(1.0, c.score + 0.1) if mine in roles else max(0.0, c.score - 0.2)


def _rerank(candidates: list[Citation], *, limit: int = 6) -> list[Citation]:
    """Deduplicate by source_id/title and blend scores."""
    best: dict[str, Citation] = {}
    for c in candidates:
        key = c.source_id or c.title
        if key not in best or c.score > best[key].score:
            best[key] = c
        else:
            # slight boost for multi-channel hits
            best[key].score = min(1.0, best[key].score + 0.05)
    ranked = sorted(best.values(), key=lambda c: c.score, reverse=True)
    return ranked[:limit]


_MEMO = threading.local()
_MEMO_TTL_SECONDS = 15.0


def retrieve(
    *,
    query: str,
    role_bucket: str,
    department_id: int | None = None,
    user=None,
    conversation=None,
    limit: int = 6,
) -> RetrievalResult:
    # One message can ask twice (the intelligence layer's "are the docs strong?" check, then RAG itself).
    # Only chat turns are memoised, keyed by conversation, so admin search / indexing always see fresh data.
    key = None
    if conversation is not None and getattr(conversation, "pk", None):
        key = (str(conversation.pk), query, role_bucket, department_id, getattr(user, "pk", None), limit)
        memo = getattr(_MEMO, "last", None)
        if memo and memo[0] == key and time.monotonic() - memo[1] < _MEMO_TTL_SECONDS:
            return copy.deepcopy(memo[2])
    result = _retrieve(
        query=query, role_bucket=role_bucket, department_id=department_id, user=user,
        conversation=conversation, limit=limit,
    )
    if key is not None:
        _MEMO.last = (key, time.monotonic(), copy.deepcopy(result))
    return result


def _retrieve(
    *,
    query: str,
    role_bucket: str,
    department_id: int | None = None,
    user=None,
    conversation=None,
    limit: int = 6,
) -> RetrievalResult:
    started = time.perf_counter()
    intent = detect_intent(query)
    levels = allowed_security_levels(role_bucket)

    candidates: list[Citation] = []

    # Structured
    for h in structured_search(query=query, intent=intent, limit=5):
        candidates.append(
            Citation(
                source_id=h.source_id,
                title=h.title,
                snippet=h.snippet,
                score=h.score,
                url=h.url,
                category=h.category,
                source_type="equipment" if h.category == "equipment" else "policy",
            )
        )

    # Vector
    try:
        provider = get_embedding_provider()
        qvec = provider.embed_query(query)
        store = get_vector_store()
        for hit in store.similarity_search(
            query_vector=qvec,
            allowed_levels=levels,
            department_id=department_id,
            limit=8,
            embedding_model=provider.name,
            embedding_version=provider.version,
        ):
            candidates.append(
                Citation(
                    source_id=hit.document_id,
                    title=hit.title,
                    snippet=hit.content[:400],
                    score=max(0.0, min(1.0, hit.score)),
                    url=safe_citation_url((hit.metadata or {}).get("external_url"))
                    or safe_citation_url((hit.metadata or {}).get("source_uri")),
                    category=(hit.metadata or {}).get("category") or "",
                    source_type="document",
                )
            )
    except Exception:
        logger.warning("Vector search failed", exc_info=True)

    # Keyword
    candidates.extend(_keyword_search(query=query, allowed_levels=levels, department_id=department_id, limit=8))

    _role_adjust(candidates, role_bucket)
    citations = _rerank(candidates, limit=limit)
    low = len(citations) == 0 or (citations and citations[0].score < 0.35)
    latency = int((time.perf_counter() - started) * 1000)

    # Build LLM context — never invent; only retrieved text
    lines = []
    for i, c in enumerate(citations, 1):
        lines.append(f"[{i}] {c.title} ({c.category or c.source_type})\n{c.snippet}")
    context_block = "\n\n".join(lines)

    try:
        SearchQueryLog.objects.create(
            user=user if getattr(user, "is_authenticated", False) else None,
            conversation=conversation,
            query=(query or "")[:1024],
            intent=intent,
            role_bucket=role_bucket[:32],
            hit_count=len(citations),
            top_score=citations[0].score if citations else None,
            latency_ms=latency,
            citation_ids=[c.source_id for c in citations],
            low_confidence=low,
        )
    except Exception:
        logger.warning("SearchQueryLog write failed", exc_info=True)

    return RetrievalResult(
        citations=citations,
        intent=intent,
        latency_ms=latency,
        low_confidence=low,
        context_block=context_block,
    )


def citations_as_dicts(citations: list[Citation]) -> list[dict]:
    return [{**asdict(c), "url": safe_citation_url(c.url)} for c in citations]


_PASSAGE_STOPWORDS = {
    "the", "and", "for", "what", "how", "does", "can", "should", "with", "this", "that", "from", "are",
    "you", "your", "about", "manual", "instrument", "equipment", "tell", "please", "use", "using",
    "there", "which", "when", "where", "who", "why", "have", "has", "into", "its", "any", "per",
}


def _passage(*, chunk_id, document, content: str, score: float, meta: dict) -> dict:
    return {
        "chunk_id": str(chunk_id),
        "document_id": str(document.id),
        "title": document.title,
        "version": document.version or "",
        "content": content,
        "page": meta.get("page"),
        "page_end": meta.get("page_end"),
        "has_file": bool(document.source_file_key),
        "score": float(score),
    }


def manual_passages(
    *,
    query: str,
    equipment_id: int,
    role_bucket: str,
    department_id: int | None = None,
    limit: int = 5,
) -> list[dict]:
    """
    Chunk-level retrieval restricted to one equipment's ACTIVE documents, with page numbers.

    Vector hits only compare against chunks embedded by the current embedding model; keyword
    hits cover documents that were indexed before an embedding-model switch.
    """
    levels = allowed_security_levels(role_bucket)
    base = KnowledgeChunk.objects.select_related("document").filter(
        document__status=DocumentStatus.ACTIVE,
        document__security_level__in=list(levels),
        document__equipment_id=int(equipment_id),
    )
    if department_id is not None:
        base = base.filter(Q(document__department_id__isnull=True) | Q(document__department_id=department_id))
    if not base.exists():
        return []

    passages: dict[str, dict] = {}
    try:
        provider = get_embedding_provider()
        qvec = provider.embed_query(query)
        store = get_vector_store()
        hits = store.similarity_search(
            query_vector=qvec,
            allowed_levels=levels,
            department_id=department_id,
            limit=limit * 2,
            equipment_id=int(equipment_id),
            embedding_model=provider.name,
            embedding_version=provider.version,
        )
        chunk_docs = {
            str(c.id): c
            for c in base.filter(id__in=[h.chunk_id for h in hits])
        }
        for hit in hits:
            chunk = chunk_docs.get(str(hit.chunk_id))
            if chunk is None:
                continue
            passages[str(chunk.id)] = _passage(
                chunk_id=chunk.id,
                document=chunk.document,
                content=chunk.content,
                score=max(0.0, min(1.0, hit.score)),
                meta=chunk.metadata or {},
            )
    except Exception:
        logger.warning("Manual vector search failed", exc_info=True)

    tokens = [t for t in _TOKEN_RE.findall((query or "").lower()) if t not in _PASSAGE_STOPWORDS]
    if tokens:
        q_obj = Q()
        for tok in tokens[:10]:
            q_obj |= Q(content__icontains=tok)
        for chunk in base.filter(q_obj).order_by("chunk_index")[:200]:
            lower = chunk.content.lower()
            density = sum(1 for t in tokens if t in lower) / max(len(tokens), 1)
            score = 0.3 + 0.5 * density
            key = str(chunk.id)
            if key in passages:
                passages[key]["score"] = min(1.0, max(passages[key]["score"], score) + 0.1)
            else:
                passages[key] = _passage(
                    chunk_id=chunk.id,
                    document=chunk.document,
                    content=chunk.content,
                    score=score,
                    meta=chunk.metadata or {},
                )

    ranked = sorted(passages.values(), key=lambda p: p["score"], reverse=True)
    return ranked[:limit]


def equipment_has_manual(*, equipment_id: int) -> bool:
    return KnowledgeChunk.objects.filter(
        document__status=DocumentStatus.ACTIVE,
        document__equipment_id=int(equipment_id),
    ).exists()

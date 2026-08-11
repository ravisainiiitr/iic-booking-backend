"""Hybrid RAG retrieval pipeline (Phase AI.2).

User Question → Intent → Permission → Structured + Vector + Keyword → Re-rank → Citations
"""

from __future__ import annotations

import logging
import re
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


def _keyword_search(*, query: str, allowed_levels: set[str], department_id: int | None, limit: int = 8) -> list[Citation]:
    tokens = _TOKEN_RE.findall((query or "").lower())
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
                score=0.45 + 0.4 * score,
                url=doc.external_url or doc.source_uri or f"/admin-settings/knowledge?doc={doc.id}",
                category=doc.category,
                source_type="document",
            )
        )
    hits.sort(key=lambda c: c.score, reverse=True)
    return hits[:limit]


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


def retrieve(
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
        ):
            candidates.append(
                Citation(
                    source_id=hit.document_id,
                    title=hit.title,
                    snippet=hit.content[:400],
                    score=max(0.0, min(1.0, hit.score)),
                    url=(hit.metadata or {}).get("external_url")
                    or (hit.metadata or {}).get("source_uri")
                    or "",
                    category=(hit.metadata or {}).get("category") or "",
                    source_type="document",
                )
            )
    except Exception:
        logger.warning("Vector search failed", exc_info=True)

    # Keyword
    candidates.extend(_keyword_search(query=query, allowed_levels=levels, department_id=department_id, limit=8))

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
    return [asdict(c) for c in citations]

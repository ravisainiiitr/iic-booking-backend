"""
Verified Copilot knowledge (approved articles only).

Retrieval combines keyword phrases, title/question token overlap and a local hash embedding
(no external calls). The score is mapped to an internal confidence band; numeric scores are never
shown to users.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from django.db.models import F
from django.utils import timezone

HIGH = "HIGH"
MEDIUM = "MEDIUM"
LOW = "LOW"
NO_VERIFIED_ANSWER = "NO_VERIFIED_ANSWER"

HIGH_SCORE = 6.0
MEDIUM_SCORE = 3.5
LOW_SCORE = 2.0
_MAX_CANDIDATES = 500

_STOP = {
    "a", "an", "the", "is", "are", "am", "was", "be", "to", "of", "in", "on", "for", "and", "or", "i", "me", "my",
    "we", "our", "you", "your", "it", "this", "that", "do", "does", "did", "can", "could", "should", "would", "will",
    "how", "what", "when", "where", "why", "which", "who", "with", "from", "at", "by", "as", "if", "please", "about",
    "there", "any", "some", "get", "want", "need", "tell", "know", "iic", "portal",
}
_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass
class Hit:
    article: Any
    score: float
    confidence: str

    def as_dict(self) -> dict[str, Any]:
        a = self.article
        return {
            "id": str(a.id),
            "title": a.title,
            "question": a.question,
            "answer": a.answer,
            "category": a.category,
            "confidence": self.confidence,
            "updated_at": a.updated_at.isoformat() if a.updated_at else None,
        }


def normalize(text: str) -> str:
    return " ".join(_TOKEN_RE.findall((text or "").lower()))


def tokens(text: str) -> set[str]:
    out = set()
    for tok in _TOKEN_RE.findall((text or "").lower()):
        if tok in _STOP or len(tok) < 2:
            continue
        out.add(tok[:-1] if len(tok) > 4 and tok.endswith("s") and not tok.endswith("ss") else tok)
    return out


def confidence_for(score: float) -> str:
    if score >= HIGH_SCORE:
        return HIGH
    if score >= MEDIUM_SCORE:
        return MEDIUM
    if score >= LOW_SCORE:
        return LOW
    return NO_VERIFIED_ANSWER


def audience_codes_for(user) -> set[str]:
    """Audiences whose articles this user may see."""
    from iic_booking.users.models.user_type import UserType

    codes = {"all"}
    ut = str(getattr(user, "user_type", "") or "")
    if getattr(user, "is_superuser", False) or UserType.is_management_user(ut):
        return {"all", "internal", "external", "student", "faculty", "staff"}
    if UserType.is_external_user(ut):
        codes.add("external")
    else:
        codes.add("internal")
    if ut in {UserType.STUDENT, UserType.INDIVIDUAL_STUDENT}:
        codes.add("student")
    if ut == UserType.FACULTY:
        codes.add("faculty")
    return codes


def _approved_for(user):
    from iic_booking.research_copilot.models import CopilotKnowledgeArticle, KnowledgeArticleStatus

    return CopilotKnowledgeArticle.objects.filter(
        status=KnowledgeArticleStatus.APPROVED, audience__in=sorted(audience_codes_for(user))
    ).order_by("-updated_at")[:_MAX_CANDIDATES]


def _embed(texts: list[str]) -> list[list[float]]:
    from iic_booking.research_copilot.services.embeddings import LocalHashEmbedding

    return LocalHashEmbedding().embed_texts(texts)


def score_article(query: str, article, *, query_vec: list[float] | None = None) -> float:
    from iic_booking.research_copilot.services.embeddings import cosine_similarity

    qn = normalize(query)
    qt = tokens(query)
    if not qn or not qt:
        return 0.0
    score = 0.0
    if normalize(article.question) == qn or normalize(article.title) == qn:
        score += 10.0
    for kw in article.keywords or []:
        kn = normalize(str(kw))
        if not kn:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(kn)}(?![a-z0-9])", qn):
            score += 4.0 if " " in kn else 2.5
    head = tokens(f"{article.title} {article.question}")
    overlap = qt & head
    if overlap:
        score += 5.0 * len(overlap) / max(len(qt), 1) + min(len(overlap), 4) * 0.5
    body_overlap = (qt - overlap) & tokens(article.answer)
    score += min(len(body_overlap) * 0.3, 1.5)
    vec = query_vec if query_vec is not None else _embed([query])[0]
    head_vec = _embed([f"{article.title} {article.question} {' '.join(map(str, article.keywords or []))}"])[0]
    score += 4.0 * max(cosine_similarity(vec, head_vec), 0.0)
    return round(score, 3)


def search(*, text: str, user, limit: int = 3) -> list[Hit]:
    """Approved articles visible to this user, best first. Never returns draft/pending/inactive articles."""
    if not (text or "").strip():
        return []
    query_vec = _embed([text])[0]
    hits = []
    for article in _approved_for(user):
        s = score_article(text, article, query_vec=query_vec)
        if s >= LOW_SCORE:
            hits.append(Hit(article=article, score=s, confidence=confidence_for(s)))
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[: max(1, int(limit))]


def best(*, text: str, user) -> tuple[str, list[Hit]]:
    hits = search(text=text, user=user, limit=3)
    if not hits:
        return NO_VERIFIED_ANSWER, []
    return hits[0].confidence, hits


def get_approved(*, article_id, user):
    from iic_booking.research_copilot.models import CopilotKnowledgeArticle, KnowledgeArticleStatus

    try:
        return CopilotKnowledgeArticle.objects.filter(
            pk=article_id, status=KnowledgeArticleStatus.APPROVED, audience__in=sorted(audience_codes_for(user))
        ).first()
    except Exception:  # noqa: BLE001 - malformed UUIDs raise ValidationError
        return None


def record_usage(article) -> None:
    from iic_booking.research_copilot.models import CopilotKnowledgeArticle

    CopilotKnowledgeArticle.objects.filter(pk=article.pk).update(
        usage_count=F("usage_count") + 1, last_used_at=timezone.now()
    )


def record_feedback(article_id, *, helpful: bool) -> None:
    from iic_booking.research_copilot.models import CopilotKnowledgeArticle

    field = "helpful_count" if helpful else "not_helpful_count"
    try:
        CopilotKnowledgeArticle.objects.filter(pk=article_id).update(**{field: F(field) + 1})
    except Exception:  # noqa: BLE001 - malformed UUIDs raise ValidationError
        return

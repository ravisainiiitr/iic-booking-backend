"""
Copilot knowledge article management: create, edit (versioned), approve, deactivate.

Approval rules:
  editors   = admin, dept_admin, OIC (manager), superuser
  approvers = admin, dept_admin, superuser
An article is shown to users only when status == approved. Editing an approved article as a
non-approver moves it back to pending_approval, so users never see unreviewed text.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.utils import timezone

EDITOR_TYPES = {"admin", "dept_admin", "manager"}
APPROVER_TYPES = {"admin", "dept_admin"}
_EDITABLE_FIELDS = ("title", "question", "answer", "category", "keywords", "audience", "related_feature")
MAX_KEYWORDS = 30


class ArticleError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def can_edit(user) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    return bool(getattr(user, "is_superuser", False) or getattr(user, "user_type", "") in EDITOR_TYPES)


def can_approve(user) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    return bool(getattr(user, "is_superuser", False) or getattr(user, "user_type", "") in APPROVER_TYPES)


def _clean(data: dict[str, Any], *, partial: bool) -> dict[str, Any]:
    from iic_booking.research_copilot.models import KnowledgeArticleAudience, KnowledgeArticleCategory

    out: dict[str, Any] = {}
    for key in _EDITABLE_FIELDS:
        if key not in data:
            continue
        value = data.get(key)
        if key == "keywords":
            if isinstance(value, str):
                value = [v for v in (s.strip() for s in value.split(",")) if v]
            if not isinstance(value, list):
                raise ArticleError("keywords must be a list of phrases.")
            value = [str(v).strip()[:80] for v in value if str(v).strip()][:MAX_KEYWORDS]
        elif key == "category":
            value = str(value or "general")
            if value not in KnowledgeArticleCategory.values:
                raise ArticleError("Unknown category.")
        elif key == "audience":
            value = str(value or "all")
            if value not in KnowledgeArticleAudience.values:
                raise ArticleError("Unknown audience.")
        else:
            value = str(value or "").strip()
        out[key] = value
    if not partial:
        if not out.get("title"):
            raise ArticleError("Title is required.")
        if not out.get("answer"):
            raise ArticleError("Answer is required.")
    if "title" in out and not out["title"]:
        raise ArticleError("Title is required.")
    if "answer" in out and not out["answer"]:
        raise ArticleError("Answer is required.")
    if "title" in out:
        out["title"] = out["title"][:255]
    if "related_feature" in out:
        out["related_feature"] = out["related_feature"][:64]
    return out


def _snapshot(article, *, change: str, user) -> None:
    from iic_booking.research_copilot.models import CopilotKnowledgeArticleVersion

    CopilotKnowledgeArticleVersion.objects.create(
        article=article,
        version=article.version,
        title=article.title,
        question=article.question,
        answer=article.answer,
        category=article.category,
        keywords=list(article.keywords or []),
        audience=article.audience,
        status=article.status,
        change=change,
        changed_by=user if getattr(user, "is_authenticated", False) else None,
    )


def _requested_status(user, requested: str | None, default: str) -> str:
    from iic_booking.research_copilot.models import KnowledgeArticleStatus as S

    requested = (requested or "").strip() or default
    if requested not in {S.DRAFT, S.PENDING_APPROVAL, S.APPROVED}:
        raise ArticleError("Status must be draft, pending_approval or approved.")
    if requested == S.APPROVED and not can_approve(user):
        return S.PENDING_APPROVAL
    return requested


def _set_equipment(article, equipment_ids) -> None:
    if equipment_ids is None:
        return
    from iic_booking.equipment.models import Equipment

    ids = []
    for raw in equipment_ids or []:
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    article.related_equipment.set(Equipment.objects.filter(pk__in=ids[:50]))


@transaction.atomic
def create(*, user, data: dict[str, Any], source: str = "manual", source_ticket=None):
    from iic_booking.research_copilot.models import CopilotKnowledgeArticle, KnowledgeArticleStatus as S

    if not can_edit(user):
        raise ArticleError("You cannot manage Copilot knowledge.", status=403)
    fields = _clean(data, partial=False)
    status = _requested_status(user, data.get("status"), S.PENDING_APPROVAL)
    article = CopilotKnowledgeArticle.objects.create(
        **fields,
        source=source,
        source_ticket=source_ticket,
        status=status,
        created_by=user,
        updated_by=user,
        approved_by=user if status == S.APPROVED else None,
        approved_at=timezone.now() if status == S.APPROVED else None,
    )
    _set_equipment(article, data.get("related_equipment_ids"))
    _snapshot(article, change="created", user=user)
    return article


@transaction.atomic
def update(*, user, article, data: dict[str, Any]):
    from iic_booking.research_copilot.models import KnowledgeArticleStatus as S

    if not can_edit(user):
        raise ArticleError("You cannot manage Copilot knowledge.", status=403)
    fields = _clean(data, partial=True)
    changed = [k for k, v in fields.items() if getattr(article, k) != v]
    for k, v in fields.items():
        setattr(article, k, v)
    status_default = article.status
    if changed and article.status == S.APPROVED and not can_approve(user):
        status_default = S.PENDING_APPROVAL
    if "status" in data:
        status = _requested_status(user, data.get("status"), status_default)
    else:
        status = status_default if article.status != S.INACTIVE else S.INACTIVE
    if status == S.APPROVED and article.status != S.APPROVED:
        article.approved_by = user
        article.approved_at = timezone.now()
    article.status = status
    article.updated_by = user
    if changed:
        article.version += 1
    article.save()
    _set_equipment(article, data.get("related_equipment_ids"))
    _snapshot(article, change="edited" if changed else "status", user=user)
    return article


@transaction.atomic
def approve(*, user, article):
    from iic_booking.research_copilot.models import KnowledgeArticleStatus as S

    if not can_approve(user):
        raise ArticleError("Only an admin can approve Copilot answers.", status=403)
    article.status = S.APPROVED
    article.approved_by = user
    article.approved_at = timezone.now()
    article.updated_by = user
    article.save(update_fields=["status", "approved_by", "approved_at", "updated_by", "updated_at"])
    _snapshot(article, change="approved", user=user)
    return article


@transaction.atomic
def deactivate(*, user, article):
    from iic_booking.research_copilot.models import KnowledgeArticleStatus as S

    if not can_edit(user):
        raise ArticleError("You cannot manage Copilot knowledge.", status=403)
    article.status = S.INACTIVE
    article.updated_by = user
    article.save(update_fields=["status", "updated_by", "updated_at"])
    _snapshot(article, change="deactivated", user=user)
    return article


def from_ticket(*, user, ticket, data: dict[str, Any]):
    """Save a resolved ticket's answer as a Copilot article (pending approval unless an approver publishes)."""
    from iic_booking.research_copilot.models import KnowledgeArticleSource

    merged = {
        "title": data.get("title") or (ticket.subject or "")[:255],
        "question": data.get("question") or ticket.description or ticket.subject or "",
        "answer": data.get("answer") or getattr(ticket, "resolution_notes", "") or "",
        "category": data.get("category") or _category_for_ticket_type(ticket.ticket_type),
        "keywords": data.get("keywords") or [],
        "audience": data.get("audience") or "all",
        "status": data.get("status") or "pending_approval",
    }
    if ticket.related_equipment_id and "related_equipment_ids" not in data:
        merged["related_equipment_ids"] = [ticket.related_equipment_id]
    elif "related_equipment_ids" in data:
        merged["related_equipment_ids"] = data["related_equipment_ids"]
    article = create(user=user, data=merged, source=KnowledgeArticleSource.TICKET, source_ticket=ticket)
    _resolve_linked_gaps(ticket=ticket, article=article, user=user)
    return article


def _category_for_ticket_type(ticket_type: str) -> str:
    return {
        "booking": "booking",
        "payment": "wallet",
        "equipment": "equipment",
        "account": "account",
        "laboratory": "results",
    }.get(ticket_type or "", "general")


def _resolve_linked_gaps(*, ticket, article, user) -> None:
    from iic_booking.research_copilot.models import CopilotEscalation, KnowledgeGap

    conv_ids = list(
        CopilotEscalation.objects.filter(ticket=ticket).exclude(conversation=None).values_list("conversation_id", flat=True)
    )
    if conv_ids:
        KnowledgeGap.objects.filter(conversation_id__in=conv_ids, status="open").update(
            status="answered", resolved_article=article, resolved_by=user, resolved_at=timezone.now()
        )


def resolve_gap(*, user, gap, article=None, dismiss: bool = False):
    if not can_edit(user):
        raise ArticleError("You cannot manage Copilot knowledge.", status=403)
    gap.status = "dismissed" if dismiss else "answered"
    gap.resolved_article = article
    gap.resolved_by = user
    gap.resolved_at = timezone.now()
    gap.save(update_fields=["status", "resolved_article", "resolved_by", "resolved_at"])
    return gap


def serialize(article, *, detail: bool = False) -> dict[str, Any]:
    out = {
        "id": str(article.id),
        "title": article.title,
        "question": article.question,
        "answer": article.answer,
        "category": article.category,
        "keywords": list(article.keywords or []),
        "audience": article.audience,
        "related_feature": article.related_feature,
        "source": article.source,
        "source_ticket_id": article.source_ticket_id,
        "status": article.status,
        "version": article.version,
        "created_by": getattr(article.created_by, "email", None),
        "updated_by": getattr(article.updated_by, "email", None),
        "approved_by": getattr(article.approved_by, "email", None),
        "approved_at": article.approved_at.isoformat() if article.approved_at else None,
        "usage_count": article.usage_count,
        "helpful_count": article.helpful_count,
        "not_helpful_count": article.not_helpful_count,
        "last_used_at": article.last_used_at.isoformat() if article.last_used_at else None,
        "created_at": article.created_at.isoformat() if article.created_at else None,
        "updated_at": article.updated_at.isoformat() if article.updated_at else None,
    }
    if detail:
        out["related_equipment"] = [{"id": e.pk, "name": e.name} for e in article.related_equipment.all()[:50]]
        out["versions"] = [
            {
                "version": v.version,
                "title": v.title,
                "question": v.question,
                "answer": v.answer,
                "status": v.status,
                "change": v.change,
                "changed_by": getattr(v.changed_by, "email", None),
                "created_at": v.created_at.isoformat() if v.created_at else None,
            }
            for v in article.versions.select_related("changed_by").order_by("-created_at")[:50]
        ]
    return out

"""Knowledge Center admin + search APIs (Phase AI.2)."""

from __future__ import annotations

from django.db.models import Avg, Count, Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response

from iic_booking.research_copilot.models import (
    DocumentCategory,
    DocumentStatus,
    EmbeddingJob,
    IndexStatus,
    KnowledgeDocument,
    KnowledgeGap,
    SearchQueryLog,
    SecurityLevel,
)
from iic_booking.research_copilot.services.context_builder import build_context
from iic_booking.research_copilot.services.ingestion import index_document, rebuild_all_indexes, upsert_document
from iic_booking.research_copilot.services import conversation as conv_svc
from iic_booking.research_copilot.services import rag as rag_svc
from iic_booking.research_copilot.services.seed_knowledge import seed_baseline_knowledge
from iic_booking.users.models.user_type import UserType


class IsCopilotKnowledgeAdmin(BasePermission):
    def has_permission(self, request, view):
        user = request.user
        if not user or not getattr(user, "is_authenticated", False):
            return False
        if getattr(user, "is_superuser", False):
            return True
        ut = str(getattr(user, "user_type", "") or "").lower()
        return ut in {UserType.ADMIN, "admin"}


def _feature_gate(request=None):
    user = getattr(request, "user", None) if request is not None else None
    if not conv_svc.feature_enabled(user=user):
        return Response(
            {
                "error": {
                    "code": "research_copilot_disabled",
                    "message": "IIC Research Copilot is not enabled on this environment.",
                }
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return None


def _ser_doc(doc: KnowledgeDocument) -> dict:
    return {
        "id": str(doc.id),
        "title": doc.title,
        "category": doc.category,
        "security_level": doc.security_level,
        "status": doc.status,
        "index_status": doc.index_status,
        "version": doc.version,
        "language": doc.language,
        "tags": doc.tags or [],
        "department_id": doc.department_id,
        "equipment_id": doc.equipment_id,
        "source_type": doc.source_type,
        "source_uri": doc.source_uri,
        "external_url": doc.external_url,
        "chunk_count": doc.chunk_count,
        "embedding_version": doc.embedding_version,
        "error_message": doc.error_message,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
        "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
        "indexed_at": doc.indexed_at.isoformat() if doc.indexed_at else None,
    }


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, IsCopilotKnowledgeAdmin])
def knowledge_documents(request):
    gated = _feature_gate(request)
    if gated:
        return gated
    if request.method == "GET":
        qs = KnowledgeDocument.objects.all()
        cat = request.query_params.get("category")
        st = request.query_params.get("status")
        idx = request.query_params.get("index_status")
        q = request.query_params.get("search")
        if cat:
            qs = qs.filter(category=cat)
        if st:
            qs = qs.filter(status=st)
        if idx:
            qs = qs.filter(index_status=idx)
        if q:
            qs = qs.filter(Q(title__icontains=q) | Q(content_text__icontains=q) | Q(tags__icontains=q))
        rows = [_ser_doc(d) for d in qs[:200]]
        return Response({"count": len(rows), "results": rows})

    title = (request.data.get("title") or "").strip()
    content = request.data.get("content_text") or request.data.get("content") or ""
    if not title or not str(content).strip():
        return Response(
            {"error": {"code": "invalid", "message": "title and content_text required"}},
            status=400,
        )
    doc = upsert_document(
        title=title,
        content_text=str(content),
        category=request.data.get("category") or DocumentCategory.OTHER,
        security_level=request.data.get("security_level") or SecurityLevel.AUTHENTICATED,
        source_type=request.data.get("source_type") or "markdown",
        version=request.data.get("version") or "1.0",
        language=request.data.get("language") or "en",
        tags=request.data.get("tags") or [],
        department_id=request.data.get("department_id"),
        equipment_id=request.data.get("equipment_id"),
        source_uri=request.data.get("source_uri") or "",
        external_url=request.data.get("external_url") or "",
        created_by=request.user,
        index_now=bool(request.data.get("index_now", True)),
    )
    return Response(_ser_doc(doc), status=status.HTTP_201_CREATED)


@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated, IsCopilotKnowledgeAdmin])
def knowledge_document_detail(request, document_id):
    gated = _feature_gate(request)
    if gated:
        return gated
    doc = get_object_or_404(KnowledgeDocument, id=document_id)
    if request.method == "GET":
        data = _ser_doc(doc)
        data["content_text"] = doc.content_text
        return Response(data)
    if request.method == "DELETE":
        doc.status = DocumentStatus.ARCHIVED
        doc.save(update_fields=["status", "updated_at"])
        return Response({"ok": True})
    doc = upsert_document(
        title=request.data.get("title") or doc.title,
        content_text=request.data.get("content_text", doc.content_text),
        category=request.data.get("category") or doc.category,
        security_level=request.data.get("security_level") or doc.security_level,
        source_type=request.data.get("source_type") or doc.source_type,
        version=request.data.get("version") or doc.version,
        language=request.data.get("language") or doc.language,
        tags=request.data.get("tags") if request.data.get("tags") is not None else doc.tags,
        department_id=request.data.get("department_id", doc.department_id),
        equipment_id=request.data.get("equipment_id", doc.equipment_id),
        source_uri=request.data.get("source_uri", doc.source_uri),
        external_url=request.data.get("external_url", doc.external_url),
        created_by=request.user,
        document_id=doc.id,
        index_now=bool(request.data.get("index_now", True)),
    )
    return Response(_ser_doc(doc))


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsCopilotKnowledgeAdmin])
def knowledge_document_reindex(request, document_id):
    gated = _feature_gate(request)
    if gated:
        return gated
    doc = get_object_or_404(KnowledgeDocument, id=document_id)
    job = index_document(doc)
    return Response({"job_id": str(job.id), "status": job.status, "document": _ser_doc(doc)})


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsCopilotKnowledgeAdmin])
def knowledge_rebuild_index(request):
    gated = _feature_gate(request)
    if gated:
        return gated
    result = rebuild_all_indexes()
    return Response(result)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsCopilotKnowledgeAdmin])
def knowledge_seed(request):
    gated = _feature_gate(request)
    if gated:
        return gated
    force = bool(request.data.get("force", False))
    return Response(seed_baseline_knowledge(force=force))


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsCopilotKnowledgeAdmin])
def knowledge_jobs(request):
    gated = _feature_gate(request)
    if gated:
        return gated
    rows = [
        {
            "id": str(j.id),
            "document_id": str(j.document_id) if j.document_id else None,
            "job_type": j.job_type,
            "status": j.status,
            "provider": j.provider,
            "error_message": j.error_message,
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "finished_at": j.finished_at.isoformat() if j.finished_at else None,
        }
        for j in EmbeddingJob.objects.order_by("-created_at")[:100]
    ]
    return Response({"count": len(rows), "results": rows})


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsCopilotKnowledgeAdmin])
def knowledge_analytics(request):
    gated = _feature_gate(request)
    if gated:
        return gated
    total_docs = KnowledgeDocument.objects.count()
    indexed = KnowledgeDocument.objects.filter(index_status=IndexStatus.INDEXED).count()
    failed = KnowledgeDocument.objects.filter(index_status=IndexStatus.FAILED).count()
    top_queries = list(
        SearchQueryLog.objects.values("query")
        .annotate(c=Count("id"), avg_score=Avg("top_score"))
        .order_by("-c")[:20]
    )
    low = SearchQueryLog.objects.filter(low_confidence=True).count()
    gaps = [
        {
            "id": str(g.id),
            "query_summary": g.query_summary,
            "reason": g.reason,
            "suggested_faq": g.suggested_faq,
            "created_at": g.created_at.isoformat() if g.created_at else None,
        }
        for g in KnowledgeGap.objects.order_by("-created_at")[:50]
    ]
    from iic_booking.research_copilot.models import Conversation, CopilotAuditEvent, Message, MessageFeedback
    from iic_booking.research_copilot.services.llm_gateway import provider_health

    tool_counts = list(
        CopilotAuditEvent.objects.filter(action__in=["tool_executed", "tool_denied"])
        .values("action")
        .annotate(c=Count("id"))
        .order_by("-c")[:20]
    )
    llm = provider_health().as_public_dict()
    return Response(
        {
            "documents": {"total": total_docs, "indexed": indexed, "failed": failed},
            "search": {
                "total_logs": SearchQueryLog.objects.count(),
                "low_confidence": low,
                "top_queries": top_queries,
            },
            "knowledge_gaps": gaps,
            "copilot_usage": {
                "conversations": Conversation.objects.count(),
                "messages": Message.objects.count(),
                "feedback": MessageFeedback.objects.count(),
                "audit_events": CopilotAuditEvent.objects.count(),
                "tool_actions": tool_counts,
            },
            "llm_provider": llm,
            "categories": [{"value": c.value, "label": c.label} for c in DocumentCategory],
            "security_levels": [{"value": s.value, "label": s.label} for s in SecurityLevel],
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def knowledge_search(request):
    """Authenticated hybrid search (permission-filtered) for Copilot / debugging."""
    from iic_booking.research_copilot.services import conversation as conv_svc

    if not conv_svc.feature_enabled(user=request.user):
        return Response(
            {"error": {"code": "research_copilot_disabled", "message": "Copilot disabled"}},
            status=503,
        )
    query = (request.data.get("query") or request.data.get("q") or "").strip()
    if not query:
        return Response({"error": {"code": "empty_query"}}, status=400)
    ctx = build_context(request.user)
    result = rag_svc.retrieve(
        query=query,
        role_bucket=ctx.role_bucket,
        department_id=ctx.department_id,
        user=request.user,
    )
    return Response(
        {
            "intent": result.intent,
            "latency_ms": result.latency_ms,
            "low_confidence": result.low_confidence,
            "citations": rag_svc.citations_as_dicts(result.citations),
        }
    )

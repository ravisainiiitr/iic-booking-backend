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
    # Knowledge administration only needs the global flag: admins curate manuals while the
    # chat itself may still be limited to a pilot allowlist.
    from django.conf import settings

    if not bool(getattr(settings, "RESEARCH_COPILOT_ENABLED", False)):
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
        from iic_booking.research_copilot.services.manuals import archive_manual

        archive_manual(doc)
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
    if doc.source_file_key:
        from iic_booking.research_copilot.services.manuals import reindex_manual

        reindex_manual(doc)
        return Response({"job_id": None, "status": "queued", "document": _ser_doc(doc)}, status=202)
    job = index_document(doc)
    return Response({"job_id": str(job.id), "status": job.status, "document": _ser_doc(doc)})


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsCopilotKnowledgeAdmin])
def knowledge_rebuild_index(request):
    gated = _feature_gate(request)
    if gated:
        return gated
    from iic_booking.research_copilot.tasks import rebuild_all_indexes_task

    try:
        async_result = rebuild_all_indexes_task.delay()
    except Exception:  # noqa: BLE001 - broker down: fall back to the synchronous rebuild
        return Response(rebuild_all_indexes())
    return Response({"status": "queued", "task_id": str(async_result.id)}, status=202)


def _equipment_names(ids) -> dict[int, str]:
    from iic_booking.equipment.models import Equipment

    clean = {int(i) for i in ids if i is not None}
    return dict(Equipment.objects.filter(pk__in=clean).values_list("pk", "name")) if clean else {}


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, IsCopilotKnowledgeAdmin])
def knowledge_manuals(request):
    """GET ?equipment_id= lists manuals; POST multipart (file, equipment_id, title?, security_level?, version?)."""
    gated = _feature_gate(request)
    if gated:
        return gated
    from iic_booking.equipment.models import Equipment
    from iic_booking.research_copilot.services import manuals as manual_svc
    from iic_booking.research_copilot.services import pdf_extract

    if request.method == "GET":
        eq_raw = request.query_params.get("equipment_id")
        include_archived = str(request.query_params.get("include_archived", "")).lower() in {"1", "true", "yes"}
        if eq_raw:
            try:
                qs = manual_svc.manuals_for_equipment(int(eq_raw), include_archived=include_archived)
            except (TypeError, ValueError):
                return Response({"error": {"code": "invalid_equipment_id"}}, status=400)
        else:
            qs = KnowledgeDocument.objects.filter(category=manual_svc.MANUAL_CATEGORY).exclude(source_file_key="")
            if not include_archived:
                qs = qs.exclude(status=DocumentStatus.ARCHIVED)
            qs = qs.order_by("-created_at")
        docs = list(qs[:500])
        names = _equipment_names(d.equipment_id for d in docs)
        rows = [manual_svc.serialize_manual(d, equipment_name=names.get(d.equipment_id)) for d in docs]
        return Response({"count": len(rows), "results": rows, "storage_configured": manual_svc.storage_configured()})

    upload = request.FILES.get("file")
    if upload is None:
        return Response({"error": {"code": "file_required", "message": "Attach a PDF as 'file'."}}, status=400)
    if upload.size and upload.size > pdf_extract.max_bytes():
        return Response(
            {"error": {"code": "FILE_TOO_LARGE", "message": f"The file exceeds the {pdf_extract.max_bytes() // (1024 * 1024)} MB limit."}},
            status=400,
        )
    try:
        equipment = Equipment.objects.get(pk=int(request.data.get("equipment_id")))
    except (TypeError, ValueError, Equipment.DoesNotExist):
        return Response({"error": {"code": "equipment_not_found", "message": "Select a valid equipment."}}, status=400)

    data = upload.read(pdf_extract.max_bytes() + 1)
    try:
        doc, duplicate = manual_svc.upload_manual(
            data=data,
            filename=upload.name or "manual.pdf",
            equipment=equipment,
            title=(request.data.get("title") or "").strip(),
            security_level=(request.data.get("security_level") or SecurityLevel.AUTHENTICATED).strip(),
            version=(request.data.get("version") or "").strip(),
            created_by=request.user,
        )
    except manual_svc.ManualError as exc:
        return Response({"error": {"code": exc.code, "message": exc.message}}, status=exc.http_status)
    payload = manual_svc.serialize_manual(doc, equipment_name=equipment.name)
    payload["duplicate"] = duplicate
    return Response(payload, status=status.HTTP_200_OK if duplicate else status.HTTP_201_CREATED)


def _manual_or_404(document_id):
    from iic_booking.research_copilot.services.manuals import MANUAL_CATEGORY

    return get_object_or_404(KnowledgeDocument, id=document_id, category=MANUAL_CATEGORY)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsCopilotKnowledgeAdmin])
def knowledge_manual_reindex(request, document_id):
    gated = _feature_gate(request)
    if gated:
        return gated
    from iic_booking.research_copilot.services import manuals as manual_svc

    doc = _manual_or_404(document_id)
    if not doc.source_file_key:
        return Response({"error": {"code": "no_file", "message": "This document has no stored PDF."}}, status=400)
    manual_svc.reindex_manual(doc)
    return Response(manual_svc.serialize_manual(doc), status=202)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsCopilotKnowledgeAdmin])
def knowledge_manual_archive(request, document_id):
    gated = _feature_gate(request)
    if gated:
        return gated
    from iic_booking.research_copilot.services import manuals as manual_svc

    doc = manual_svc.archive_manual(_manual_or_404(document_id))
    return Response(manual_svc.serialize_manual(doc))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def knowledge_document_file(request, document_id):
    """Short-lived presigned link to a manual PDF, after the same permission checks as retrieval."""
    from iic_booking.equipment.api_views import user_can_see_equipment
    from iic_booking.equipment.models import Equipment
    from iic_booking.research_copilot.services import manuals as manual_svc
    from iic_booking.research_copilot.services.knowledge_permissions import can_access_document

    is_admin = IsCopilotKnowledgeAdmin().has_permission(request, None)
    if is_admin:
        gated = _feature_gate(request)
        if gated:
            return gated
    elif not conv_svc.feature_enabled(user=request.user):
        return Response({"error": {"code": "research_copilot_disabled", "message": "Copilot disabled"}}, status=503)

    not_found = Response({"error": {"code": "not_found", "message": "Document not found."}}, status=404)
    doc = KnowledgeDocument.objects.filter(id=document_id).first()
    if doc is None or not doc.source_file_key:
        return not_found
    if not is_admin:
        ctx = build_context(request.user)
        if doc.status != DocumentStatus.ACTIVE or not can_access_document(
            role_bucket=ctx.role_bucket,
            security_level=doc.security_level,
            department_id=doc.department_id,
            user_department_id=ctx.department_id,
        ):
            return not_found
        if doc.equipment_id:
            eq = Equipment.objects.filter(pk=doc.equipment_id).first()
            if eq is None or not user_can_see_equipment(request.user, eq):
                return not_found
    try:
        url = manual_svc.presigned_url(doc)
    except Exception:  # noqa: BLE001
        return Response({"error": {"code": "storage_unavailable", "message": "The file is temporarily unavailable."}}, status=503)
    from django.conf import settings as dj_settings

    response = Response(
        {
            "url": url,
            "expires_in": int(getattr(dj_settings, "RESEARCH_COPILOT_MANUAL_URL_EXPIRY_SECONDS", 300) or 300),
            "filename": doc.original_filename or "manual.pdf",
            "page_count": doc.page_count,
        }
    )
    response["Cache-Control"] = "private, no-store"
    return response


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

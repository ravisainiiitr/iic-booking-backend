"""Manufacturer references of instruments (IITR Equipment Booking Assistant knowledge), for the staff who look after them.

GET    /api/v1/research-copilot/equipment-references/                      instruments the user looks after, with make,
                                                                            model and how many references they have
GET    /api/v1/research-copilot/equipment/<id>/references/                 the instrument's references
POST   /api/v1/research-copilot/equipment/<id>/references/                 {"url", "kind"}: fetch and add one
POST   /api/v1/research-copilot/equipment/<id>/references/<doc>/refresh/   fetch it again now
DELETE /api/v1/research-copilot/equipment/<id>/references/<doc>/           stop using it (archived)

Who: Main Administrator (all), Department Administrator (their department), Officer in Charge and Lab Operator
(their instruments), as for other per-instrument settings. At most ``ADDS_PER_HOUR`` fetches per user per hour.
"""

from __future__ import annotations

from django.core.cache import cache
from django.db.models import Count, Q
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.equipment import portal_actions as P

from .models import DocumentCategory, DocumentStatus, IndexStatus, KnowledgeDocument
from .services import equipment_references as refs

ADDS_PER_HOUR = 20


def _managed(user):
    if not (P.roles_of(user) & P.STAFF):
        return None
    return P.equipment_scope(user, P.BY_KEY["change_slot_status"])


def _index_later(doc) -> None:
    """Index in the Celery worker (embedding a long PDF takes a while); index here if the queue is unavailable."""
    from .tasks import index_document_task

    try:
        index_document_task.delay(str(doc.id))
    except Exception:  # noqa: BLE001
        from .services.ingestion import index_document

        index_document(doc)


def _row(d: KnowledgeDocument) -> dict:
    kind = next((t for t in (d.tags or []) if t in refs.KINDS), "other")
    return {
        "id": str(d.id), "title": d.title, "url": d.source_uri or d.external_url, "kind": kind,
        "kind_label": refs.KINDS[kind], "official": d.is_official, "source_type": d.source_type,
        "retrieved_at": d.retrieved_at.isoformat() if d.retrieved_at else None, "index_status": d.index_status,
        "chunks": d.chunk_count, "error": d.error_message,
    }


def _refs(eq_id: int):
    return KnowledgeDocument.objects.filter(equipment_id=eq_id, category=DocumentCategory.MANUFACTURER,
                                            status=DocumentStatus.ACTIVE).order_by("-retrieved_at")


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def equipment_overview(request):
    qs = _managed(request.user)
    if qs is None:
        return Response({"error": "Not available for your account."}, status=status.HTTP_403_FORBIDDEN)
    q = " ".join((request.query_params.get("q") or "").split())[:80]
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(code__icontains=q) | Q(make__icontains=q) | Q(model_information__icontains=q))
    rows = list(qs.order_by("name").values("equipment_id", "name", "code", "make", "model_information")[:300])
    counts = dict(KnowledgeDocument.objects.filter(
        equipment_id__in=[r["equipment_id"] for r in rows], category=DocumentCategory.MANUFACTURER,
        status=DocumentStatus.ACTIVE).values_list("equipment_id").annotate(n=Count("id")))
    return Response({"results": [
        {"id": r["equipment_id"], "name": r["name"], "code": r["code"], "make": r["make"],
         "model": r["model_information"], "references": counts.get(r["equipment_id"], 0)} for r in rows]})


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def references(request, equipment_id: int):
    qs = _managed(request.user)
    eq = qs.filter(pk=equipment_id).first() if qs is not None else None
    if eq is None:
        return Response({"error": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    if request.method == "POST":
        key = f"copilot:refs:adds:{request.user.pk}"
        if int(cache.get(key) or 0) >= ADDS_PER_HOUR:
            return Response({"error": "Too many references added in the last hour. Try again later."},
                            status=status.HTTP_429_TOO_MANY_REQUESTS)
        cache.set(key, int(cache.get(key) or 0) + 1, 3600)
        try:
            doc = refs.add(eq, str(request.data.get("url") or ""), str(request.data.get("kind") or "other"),
                           user=request.user, index_now=False)
        except refs.ReferenceError_ as exc:
            return Response({"error": exc.message, "code": exc.code}, status=status.HTTP_400_BAD_REQUEST)
        _index_later(doc)
        return Response(_row(doc), status=status.HTTP_201_CREATED)
    return Response({
        "equipment": {"id": eq.pk, "name": eq.name, "code": eq.code, "make": eq.make, "model": eq.model_information},
        "kinds": refs.KINDS, "results": [_row(d) for d in _refs(eq.pk)],
    })


def _ref_or_404(request, equipment_id: int, doc_id):
    qs = _managed(request.user)
    if qs is None or not qs.filter(pk=equipment_id).exists():
        return None
    return _refs(equipment_id).filter(id=doc_id).first()


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def reference_refresh(request, equipment_id: int, doc_id):
    doc = _ref_or_404(request, equipment_id, doc_id)
    if doc is None:
        return Response({"error": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    ok = refs.refresh(doc, index_now=False)
    doc.refresh_from_db()
    if ok and doc.index_status == IndexStatus.STALE:
        _index_later(doc)
    return Response({**_row(doc), "refreshed": ok}, status=status.HTTP_200_OK if ok else status.HTTP_502_BAD_GATEWAY)


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def reference_remove(request, equipment_id: int, doc_id):
    doc = _ref_or_404(request, equipment_id, doc_id)
    if doc is None:
        return Response({"error": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    doc.status = DocumentStatus.ARCHIVED
    doc.save(update_fields=["status", "updated_at"])
    return Response(status=status.HTTP_204_NO_CONTENT)

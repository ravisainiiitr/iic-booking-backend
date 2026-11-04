"""API for publication-to-facility mapping (``/api/v1/impact/``): facility aliases (editable by the Main Admin,
Department Admins for their department, OICs for their equipment), OpenAlex author candidates (confirmed by the
researcher or the Main Admin) and the evidence-backed department / equipment publication mappings."""

from __future__ import annotations

from django.shortcuts import get_object_or_404
from rest_framework.response import Response

from . import author_linking
from . import service as svc
from .models import (
    AliasSource,
    FacilityAlias,
    MappingLevel,
    PublicationFacilityMapping,
    ResearcherAuthorCandidate,
)
from .views import impact_api


# --------------------------------------------------------------------------- access
def _alias_scope(user):
    """(all?, department ids, equipment ids) the user may edit aliases for."""
    from iic_booking.equipment.models import Equipment
    from iic_booking.equipment.reports import get_equipment_ids_managed_by_oic
    from iic_booking.users.models.user_type import UserType

    if svc.is_admin(user):
        return True, set(), set()
    t = getattr(user, "user_type", None)
    if t == UserType.DEPT_ADMIN and user.department_id:
        eq = set(Equipment.objects.filter(internal_department_id=user.department_id).values_list("pk", flat=True))
        return False, {user.department_id}, eq
    if t == UserType.MANAGER:
        return False, set(), {int(x) for x in get_equipment_ids_managed_by_oic(user.id)}
    return False, set(), set()


def _can_edit(user, department_id, equipment_id) -> bool:
    every, depts, eqs = _alias_scope(user)
    return every or (department_id in depts if department_id else int(equipment_id or 0) in eqs)


def alias_out(a: FacilityAlias) -> dict:
    return {
        "id": a.pk, "phrase": a.phrase, "case_sensitive": a.case_sensitive, "source": a.source,
        "source_label": a.get_source_display(), "is_active": a.is_active,
        "department": {"id": a.department_id, "name": a.department.name, "code": a.department.code} if a.department_id else None,
        "equipment": {"id": a.equipment_id, "name": a.equipment.name, "code": a.equipment.code} if a.equipment_id else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
    }


def _clean_phrase(raw) -> str:
    phrase = " ".join(str(raw or "").split())
    if len(phrase) < 2 or len(phrase) > 200:
        raise svc.ImpactError("The phrase must be 2 to 200 characters.", code="invalid_phrase")
    return phrase


# --------------------------------------------------------------------------- aliases
@impact_api(["GET", "POST"])
def facility_aliases(request):
    every, depts, eqs = _alias_scope(request.user)
    svc.require(every or depts or eqs, "Facility aliases are for the Main Admin, Department Admins and OICs.")
    if request.method == "POST":
        dept_id = request.data.get("department_id") or None
        eq_id = request.data.get("equipment_id") or None
        if bool(dept_id) == bool(eq_id):
            raise svc.ImpactError("Give either department_id or equipment_id.", code="one_target")
        svc.require(_can_edit(request.user, int(dept_id) if dept_id else None, eq_id))
        phrase = _clean_phrase(request.data.get("phrase"))
        qs = FacilityAlias.objects.filter(phrase__iexact=phrase)
        qs = qs.filter(department_id=dept_id) if dept_id else qs.filter(equipment_id=eq_id)
        if qs.exists():
            raise svc.ImpactError("This phrase is already listed.", status=409, code="duplicate")
        a = FacilityAlias.objects.create(
            department_id=dept_id, equipment_id=eq_id, phrase=phrase, source=AliasSource.MANUAL,
            case_sensitive=bool(request.data.get("case_sensitive")), created_by=request.user,
        )
        return Response(alias_out(a), status=201)
    qs = FacilityAlias.objects.select_related("department", "equipment")
    if not every:
        from django.db.models import Q

        qs = qs.filter(Q(department_id__in=depts) | Q(equipment_id__in=eqs))
    for key in ("department_id", "equipment_id"):
        if request.query_params.get(key):
            qs = qs.filter(**{key: request.query_params[key]})
    return Response({"results": [alias_out(a) for a in qs[:2000]]})


@impact_api(["PATCH", "DELETE"])
def facility_alias_detail(request, pk: int):
    a = get_object_or_404(FacilityAlias.objects.select_related("department", "equipment"), pk=pk)
    svc.require(_can_edit(request.user, a.department_id, a.equipment_id))
    if request.method == "DELETE":
        a.delete()
        return Response(status=204)
    if "phrase" in request.data:
        a.phrase = _clean_phrase(request.data["phrase"])
    for key in ("is_active", "case_sensitive"):
        if key in request.data:
            setattr(a, key, request.data[key] in (True, "true", "1", 1))
    a.save()
    return Response(alias_out(a))


# --------------------------------------------------------------------------- author candidates
def candidate_out(c: ResearcherAuthorCandidate) -> dict:
    return {
        "id": c.pk, "user_id": c.user_id, "openalex_author_id": c.openalex_author_id,
        "openalex_url": f"https://openalex.org/{c.openalex_author_id}", "display_name": c.display_name,
        "orcid": c.orcid, "works_count": c.works_count, "institutions": c.institutions,
        "confidence": float(c.confidence), "rationale": c.rationale, "status": c.status,
        "status_label": c.get_status_display(),
    }


@impact_api(["GET"])
def author_candidates(request):
    """The researcher's own candidates; the Main Admin may pass ?user_id= or ?status=."""
    qs = ResearcherAuthorCandidate.objects.all()
    if svc.is_admin(request.user):
        if request.query_params.get("user_id"):
            qs = qs.filter(user_id=request.query_params["user_id"])
    else:
        qs = qs.filter(user=request.user)
    if request.query_params.get("status"):
        qs = qs.filter(status=request.query_params["status"])
    return Response({"results": [candidate_out(c) for c in qs[:500]]})


@impact_api(["POST"])
def author_candidate_decide(request, pk: int):
    c = get_object_or_404(ResearcherAuthorCandidate, pk=pk)
    svc.require(c.user_id == request.user.pk or svc.is_admin(request.user))
    confirm = request.data.get("confirm") in (True, "true", "1", 1)
    return Response(candidate_out(author_linking.decide_candidate(c, request.user, confirm=confirm)))


# --------------------------------------------------------------------------- mappings
def mapping_out(m: PublicationFacilityMapping) -> dict:
    ev = m.evidence
    return {
        "id": m.pk, "level": m.level, "status": m.status, "status_label": m.get_status_display(),
        "confidence": float(m.confidence), "matched_phrases": m.matched_phrases, "rationale": m.rationale,
        "department": {"id": m.department_id, "name": m.department.name, "code": m.department.code},
        "equipment": {"id": m.equipment_id, "name": m.equipment.name, "code": m.equipment.code} if m.equipment_id else None,
        "claim_id": m.claim_id,
        "publication": {"title": ev.title, "doi": ev.doi, "year": ev.year, "user_id": ev.user_id},
        "evidence": evidence_out(ev),
    }


def evidence_out(ev) -> dict:
    return {
        "source_url": ev.source_url, "oa_status": ev.oa_status, "licence": ev.licence,
        "acknowledgement_snippet": ev.acknowledgement_snippet, "detail": ev.detail,
        "checked_at": ev.checked_at.isoformat() if ev.checked_at else None,
    }


@impact_api(["GET"])
def publication_mappings(request):
    """Department-level and equipment-level mappings (?level=DEPARTMENT|EQUIPMENT, ?department_id=, ?equipment_id=).
    Main Admin: all; Department Admin: their department; OIC: their equipment; others: their own papers."""
    from django.db.models import Q

    qs = PublicationFacilityMapping.objects.select_related("evidence", "department", "equipment")
    every, depts, eqs = _alias_scope(request.user)
    if not every:
        own = Q(evidence__user=request.user)
        qs = qs.filter(own | Q(department_id__in=depts) | Q(equipment_id__in=eqs))
    level = request.query_params.get("level")
    if level in MappingLevel.values:
        qs = qs.filter(level=level)
    for key in ("department_id", "equipment_id"):
        if request.query_params.get(key):
            qs = qs.filter(**{key: request.query_params[key]})
    return Response({"results": [mapping_out(m) for m in qs.order_by("-evidence__year", "pk")[:1000]]})


def claim_evidence(claim) -> list[dict]:
    """Evidence shown to the OIC beside a claim the mapping created (empty for hand-made claims)."""
    rows = PublicationFacilityMapping.objects.filter(claim=claim).select_related("evidence", "department", "equipment")
    return [{
        "equipment": {"id": m.equipment_id, "name": m.equipment.name, "code": m.equipment.code},
        "confidence": float(m.confidence), "matched_phrases": m.matched_phrases, "rationale": m.rationale,
        **evidence_out(m.evidence),
    } for m in rows]

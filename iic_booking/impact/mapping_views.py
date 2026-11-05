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


# --------------------------------------------------------------------------- probable matches (review)
def _review_call(fn):
    from . import match_review

    try:
        return fn()
    except match_review.ReviewError as exc:
        return Response({"detail": exc.message, "code": exc.code}, status=exc.status)


@impact_api(["GET"])
def publication_matches(request):
    """Probable matches for the Main Administrator (all) or a Department Administrator (their department).
    ?status=PENDING|APPROVED|REJECTED, ?q=, ?department_id= (Main Administrator)."""
    from . import match_review
    from .models import ReviewStatus

    svc.require(match_review.is_reviewer(request.user),
                "Publication matches are reviewed by the Main Administrator and Department Administrators.")
    st = request.query_params.get("status") or ReviewStatus.PENDING
    if st not in (ReviewStatus.PENDING, ReviewStatus.APPROVED, ReviewStatus.REJECTED):
        st = ReviewStatus.PENDING
    dept = request.query_params.get("department_id")
    qs = match_review.queue(request.user, st, request.query_params.get("q") or "",
                            int(dept) if dept and str(dept).isdigit() else None)
    rows = list(qs[:200])
    return Response({"results": [match_review.evidence_out(e, request.user) for e in rows],
                     "count": qs.count(), "pending_count": match_review.pending_count(request.user)})


@impact_api(["GET"])
def publication_matches_count(request):
    from . import match_review

    return Response({"pending_count": match_review.pending_count(request.user)})


@impact_api(["POST"])
def publication_match_approve(request, pk: int):
    from . import match_review

    def run():
        raw = request.data.get("equipment_ids") or []
        ids = raw if isinstance(raw, list) else [x for x in str(raw).split(",") if x.strip()]
        ev = match_review.approve(pk, request.user, ids, str(request.data.get("note") or ""))
        ev = match_review.queue(request.user, ev.review_status).filter(pk=ev.pk).first() or ev
        return Response(match_review.evidence_out(ev, request.user))

    return _review_call(run)


@impact_api(["POST"])
def publication_match_reject(request, pk: int):
    from . import match_review

    def run():
        ev = match_review.reject(pk, request.user, str(request.data.get("note") or ""))
        return Response(match_review.evidence_out(ev, request.user))

    return _review_call(run)


# --------------------------------------------------------------------------- confirming lookalike author records
def _author_review_call(fn):
    from .author_review import AuthorReviewError

    try:
        return fn()
    except AuthorReviewError as exc:
        return Response({"detail": exc.message, "code": exc.code}, status=exc.status)


@impact_api(["GET"])
def author_confirmations(request):
    """Researchers with lookalike OpenAlex records to decide: Main Admin (all), Department Admin (their faculty)."""
    from django.db.models import Q

    from . import author_review

    svc.require(author_review.reviewer_department(request.user) is not None)
    qs = author_review.waiting_users(request.user)
    if request.query_params.get("unlinked") in ("1", "true"):
        qs = qs.filter(has_link=False)
    q = (request.query_params.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(email__icontains=q) | Q(department__code__iexact=q))
    rows = [author_review.researcher_out(u, request.user) for u in qs[:300]]
    return Response({"results": rows, "count": len(rows), "waiting_count": author_review.waiting_count(request.user)})


@impact_api(["GET"])
def author_confirmations_count(request):
    from . import author_review

    if author_review.reviewer_department(request.user) is None:
        return Response({"waiting_count": 0})
    return Response({"waiting_count": author_review.waiting_count(request.user)})


@impact_api(["GET"])
def author_confirmations_mine(request):
    """The signed-in researcher's own lookalike records (the "Is this you?" prompt)."""
    from . import author_review

    return Response(author_review.researcher_out(request.user, request.user))


@impact_api(["POST"])
def author_confirmations_decide(request, user_id: int):
    from django.contrib.auth import get_user_model

    from . import author_review

    researcher = get_object_or_404(get_user_model(), pk=user_id)
    data = request.data or {}
    return _author_review_call(lambda: Response(author_review.decide(
        request.user, researcher, confirm_ids=_ids(data.get("confirm")), reject_ids=_ids(data.get("reject")))))


@impact_api(["GET"])
def author_candidate_works(request, pk: int):
    """A record's most recent papers, to tell lookalikes apart (cached for a day)."""
    from . import author_review
    from .openalex import OpenAlexError

    c = get_object_or_404(ResearcherAuthorCandidate, pk=pk)
    svc.require(author_review.can_decide_for(request.user, c.user))
    try:
        return Response({"results": author_review.recent_works(c)})
    except OpenAlexError:
        return Response({"detail": "OpenAlex did not answer; try again shortly.", "code": "upstream"}, status=502)


def _ids(raw) -> list[int]:
    if not isinstance(raw, list):
        return []
    try:
        return [int(x) for x in raw][:50]
    except (TypeError, ValueError):
        raise svc.ImpactError("Record ids must be numbers.", status=400, code="invalid") from None

"""Review of probable publication matches by the Main Administrator and Department Administrators.

A paper found automatically (PublicationEvidence with review_status PENDING) is shown with its acknowledgement
excerpt, source and the facilities it names. Nothing is public until a reviewer approves it:

* The Main Administrator can review every match.
* A Department Administrator can review a match whose suggested facilities are all in their own department; a paper
  that also names another department's facilities is left to the Main Administrator.

Approval creates (or extends) one approved EquipmentPublicationClaim for the researcher with the instruments the
reviewer ticks. The usual EquipmentPublication rows follow, so the paper appears on the Publications page and the
equipment pages and counts towards Researcher of the Month. A department-level approval with no instrument ticked is
still listed on the Publications page (as the department's paper).
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    MappingLevel,
    MappingStatus,
    PublicationEvidence,
    PublicationFacilityMapping,
    ReviewStatus,
)


class ReviewError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "invalid"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


def reviewer_department(user) -> int | None | bool:
    """True for the Main Administrator (every department), a department id for a Department Administrator,
    None for everyone else."""
    from iic_booking.users.models.user_type import UserType

    t = getattr(user, "user_type", None)
    if t == UserType.ADMIN:
        return True
    if t == UserType.DEPT_ADMIN and getattr(user, "department_id", None):
        return user.department_id
    return None


def is_reviewer(user) -> bool:
    return reviewer_department(user) is not None


def _suggested(ev: PublicationEvidence):
    return [m for m in ev.mappings.all() if m.status == MappingStatus.SUGGESTED]


def can_review(user, ev: PublicationEvidence) -> tuple[bool, str]:
    scope = reviewer_department(user)
    if scope is True:
        return True, ""
    if scope is None:
        return False, "Only the Main Administrator and Department Administrators review publication matches."
    depts = {m.department_id for m in _suggested(ev)}
    if not depts:
        return False, "Nothing in this match is waiting for review."
    if depts != {scope}:
        return False, "This paper also names another department's facilities; the Main Administrator reviews it."
    return True, ""


def queue(user, status: str = ReviewStatus.PENDING, q: str = "", department_id: int | None = None):
    scope = reviewer_department(user)
    qs = (PublicationEvidence.objects.filter(review_status=status)
          .select_related("user", "user__department", "suggestion", "reviewed_by", "claim")
          .prefetch_related("mappings__department", "mappings__equipment")
          .order_by("-checked_at", "-pk"))
    if scope is None:
        return qs.none()
    if scope is not True:
        qs = qs.filter(mappings__department_id=scope).distinct()
    elif department_id:
        qs = qs.filter(mappings__department_id=department_id).distinct()
    q = (q or "").strip()
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(doi__icontains=q) | Q(user__name__icontains=q)).distinct()
    return qs


def pending_count(user) -> int:
    return queue(user).count() if is_reviewer(user) else 0


def _equipment_choices(user, ev: PublicationEvidence) -> list[dict]:
    """Instruments the reviewer may tick: the suggested/matched ones, then every other instrument of the
    departments the paper names, all within the reviewer's own scope."""
    from iic_booking.equipment.models import Equipment

    scope = reviewer_department(user)
    maps = list(ev.mappings.all())
    dept_ids = {m.department_id for m in maps}
    if scope is not True:
        dept_ids &= {scope} if scope else set()
    matched = {m.equipment_id: m for m in maps if m.equipment_id}
    eqs = Equipment.objects.filter(internal_department_id__in=dept_ids).order_by("name")
    rows = []
    for e in eqs:
        m = matched.get(e.pk)
        rows.append({
            "id": e.pk, "name": e.name, "code": e.code, "department_id": e.internal_department_id,
            "suggested": bool(m and m.status in (MappingStatus.SUGGESTED, MappingStatus.APPROVED)),
            "confidence": float(m.confidence) if m else None,
        })
    rows.sort(key=lambda r: (not r["suggested"], -(r["confidence"] or 0), r["name"].lower()))
    return rows


def evidence_out(ev: PublicationEvidence, user) -> dict:
    s = ev.suggestion
    researcher = ev.user
    ok, why = can_review(user, ev)
    return {
        "id": ev.pk,
        "review_status": ev.review_status,
        "paper": {
            "title": ev.title, "doi": ev.doi, "year": ev.year,
            "journal": s.journal if s else "", "authors": s.authors if s else "",
            "url": f"https://doi.org/{ev.doi}" if ev.doi else ev.source_url,
        },
        "researcher": {
            "id": researcher.pk, "name": getattr(researcher, "name", "") or researcher.email,
            "department": getattr(getattr(researcher, "department", None), "name", "") or "",
            "photo_url": researcher.get_profile_picture_url_or_none() or "",
        },
        "evidence": {
            "source_url": ev.source_url, "oa_status": ev.oa_status, "licence": ev.licence,
            "acknowledgement_snippet": ev.acknowledgement_snippet, "detail": ev.detail,
            "checked_at": ev.checked_at.isoformat() if ev.checked_at else None,
        },
        "mappings": [
            {
                "id": m.pk, "level": m.level, "status": m.status, "confidence": float(m.confidence),
                "matched_phrases": m.matched_phrases, "rationale": m.rationale,
                "department": {"id": m.department_id, "name": m.department.name, "code": m.department.code or ""},
                "equipment": {"id": m.equipment_id, "name": m.equipment.name, "code": m.equipment.code} if m.equipment_id else None,
            }
            for m in sorted(ev.mappings.all(), key=lambda m: (m.level != MappingLevel.DEPARTMENT, -float(m.confidence)))
        ],
        "equipment_choices": _equipment_choices(user, ev) if ev.review_status == ReviewStatus.PENDING else [],
        "can_review": ok,
        "review_blocked_reason": why,
        "reviewed_by": getattr(ev.reviewed_by, "name", "") if ev.reviewed_by_id else "",
        "reviewed_at": ev.reviewed_at.isoformat() if ev.reviewed_at else None,
        "review_note": ev.review_note,
        "claim_id": ev.claim_id,
    }


def _lock(ev_id: int) -> PublicationEvidence:
    ev = (PublicationEvidence.objects.select_for_update(of=("self",)).select_related("user", "suggestion").filter(pk=ev_id).first())
    if ev is None:
        raise ReviewError("Match not found.", status=404, code="not_found")
    if ev.review_status != ReviewStatus.PENDING:
        raise ReviewError("This match has already been reviewed.", status=409, code="already_reviewed")
    return ev


def approve(ev_id: int, actor, equipment_ids: list[int], note: str = "") -> PublicationEvidence:
    from iic_booking.equipment.models import Equipment, EquipmentPublicationClaim, EquipmentPublicationClaimStatus
    from iic_booking.equipment.publication_claim_views import _approve_claim_to_publications, build_citation
    from iic_booking.equipment.publication_dedupe import invalidate

    with transaction.atomic():
        ev = _lock(ev_id)
        ok, why = can_review(actor, ev)
        if not ok:
            raise ReviewError(why, status=403, code="forbidden")
        scope = reviewer_department(actor)
        ids = sorted({int(i) for i in equipment_ids or [] if str(i).isdigit()})
        eqs = Equipment.objects.filter(pk__in=ids)
        if scope is not True:
            eqs = eqs.filter(internal_department_id=scope)
        eqs = list(eqs)
        if len(eqs) != len(ids):
            raise ReviewError("Choose instruments from the departments you administer.", code="equipment_scope")

        s = ev.suggestion
        authors, journal = (s.authors if s else ""), (s.journal if s else "")
        claim = (EquipmentPublicationClaim.objects.filter(submitted_by=ev.user, doi__iexact=ev.doi)
                 .exclude(status=EquipmentPublicationClaimStatus.REJECTED).first() if ev.doi else None)
        if claim is None:
            claim = EquipmentPublicationClaim.objects.create(
                submitted_by=ev.user, title=ev.title[:500], authors=authors[:1000], journal=journal[:500], year=ev.year,
                doi=ev.doi[:200], url=(f"https://doi.org/{ev.doi}" if ev.doi else ev.source_url)[:500],
                facility_note=(f"Found automatically in the open-access acknowledgement ({ev.source_url}): "
                               f"“{ev.acknowledgement_snippet}”"),
                citation=build_citation(authors=authors, title=ev.title, journal=journal, year=ev.year,
                                        volume_pages="", doi=ev.doi),
                status=EquipmentPublicationClaimStatus.PENDING,
            )
        if eqs:
            claim.equipments.add(*eqs)
        _approve_claim_to_publications(claim, actor)
        claim.status = EquipmentPublicationClaimStatus.APPROVED
        claim.reviewed_by, claim.reviewed_at = actor, timezone.now()
        claim.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])

        chosen = {e.pk for e in eqs}
        existing = set()
        for m in ev.mappings.all():
            if m.level == MappingLevel.EQUIPMENT:
                existing.add(m.equipment_id)
                m.status = MappingStatus.APPROVED if m.equipment_id in chosen else MappingStatus.NOT_SELECTED
                m.claim = claim if m.equipment_id in chosen else None
            elif m.status == MappingStatus.SUGGESTED:
                m.status, m.claim = MappingStatus.APPROVED, claim
            m.save(update_fields=["status", "claim"])
        for e in eqs:
            if e.pk not in existing:
                PublicationFacilityMapping.objects.create(
                    evidence=ev, level=MappingLevel.EQUIPMENT, department_id=e.internal_department_id, equipment=e,
                    matched_phrases=[], confidence=0, status=MappingStatus.APPROVED, claim=claim,
                    rationale=f"Chosen by {getattr(actor, 'name', '') or 'the reviewer'} when approving the paper.",
                )
        ev.review_status, ev.reviewed_by, ev.reviewed_at = ReviewStatus.APPROVED, actor, timezone.now()
        ev.review_note, ev.claim = (note or "").strip()[:2000], claim
        ev.save(update_fields=["review_status", "reviewed_by", "reviewed_at", "review_note", "claim"])
        transaction.on_commit(invalidate)
    _notify_researcher(ev, approved=True)
    return ev


def reject(ev_id: int, actor, note: str = "") -> PublicationEvidence:
    with transaction.atomic():
        ev = _lock(ev_id)
        ok, why = can_review(actor, ev)
        if not ok:
            raise ReviewError(why, status=403, code="forbidden")
        ev.mappings.filter(status__in=[MappingStatus.SUGGESTED, MappingStatus.BELOW_THRESHOLD]).update(
            status=MappingStatus.REJECTED)
        ev.review_status, ev.reviewed_by, ev.reviewed_at = ReviewStatus.REJECTED, actor, timezone.now()
        ev.review_note = (note or "").strip()[:2000]
        ev.save(update_fields=["review_status", "reviewed_by", "reviewed_at", "review_note"])
    return ev


def _notify_researcher(ev: PublicationEvidence, *, approved: bool) -> None:
    from iic_booking.communication.in_app import notify_in_app

    if not approved:
        return
    title = ev.title if len(ev.title) <= 120 else ev.title[:117] + "…"
    notify_in_app([ev.user], title="Publication added",
                  message=f"“{title}” acknowledges the facility and is now listed on the portal.",
                  link="/my-publications", event="publication_match.approved", extra={"evidence_id": ev.pk})


def notify_reviewers(evidence_ids: list[int]) -> int:
    """Tell the Main Administrators and the Department Administrators of the departments concerned how many new
    matches wait for them (one notice per person)."""
    from django.contrib.auth import get_user_model

    from iic_booking.communication.in_app import notify_in_app
    from iic_booking.users.models.user_type import UserType

    if not evidence_ids:
        return 0
    rows = (PublicationFacilityMapping.objects.filter(evidence_id__in=evidence_ids, status=MappingStatus.SUGGESTED)
            .values_list("evidence_id", "department_id").distinct())
    per_dept: dict[int, set[int]] = {}
    for ev_id, dept in rows:
        per_dept.setdefault(dept, set()).add(ev_id)
    User = get_user_model()
    counts: dict[int, set[int]] = {}
    for admin in User.objects.filter(user_type=UserType.ADMIN, is_active=True):
        counts[admin.pk] = set(evidence_ids)
    for dept, evs in per_dept.items():
        for da in User.objects.filter(user_type=UserType.DEPT_ADMIN, is_active=True, department_id=dept):
            counts.setdefault(da.pk, set()).update(evs)
    users = {u.pk: u for u in User.objects.filter(pk__in=counts)}
    for uid, evs in counts.items():
        n = len(evs)
        notify_in_app([users[uid]], title="Publications found for review",
                      message=f"{n} paper{'s' if n != 1 else ''} acknowledging the facilities {'are' if n != 1 else 'is'} waiting for your approval.",
                      link="/publication-matches", notification_type="warning", event="publication_match.found",
                      extra={"action_required": True, "count": n})
    return len(counts)

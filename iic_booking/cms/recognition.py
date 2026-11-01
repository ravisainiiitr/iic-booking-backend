"""Hall of Frames and Researcher of the Month, worked out from approved publications (no manual entry).

A publication counts once its claim is approved (``EquipmentPublicationClaim``, status approved): the researcher is
the user who submitted it, the date is when it was approved, and the acknowledged facility is the equipment named.

* Hall of Frames: every active researcher with an approved publication, most publications first, then the most
  recent approval. Updated as soon as a publication is approved.
* Researcher of the Month, for the institute and for each internal department: on the first request of a month,
  the researcher with the most publications approved in the previous 12 months (then the most recent approval) is
  chosen and stored, so earlier months stay as history. Someone featured in either of the two previous months is
  passed over while anyone else qualifies, so the recognition moves around.
* The Main Administrator may take anyone off the home page (``RecognitionHide``): from the Hall of Frames until shown
  again, or as a month's Researcher of the Month, in which case the next eligible researcher is chosen at once.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional

from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Q
from django.utils import timezone

HALL_LIMIT = 24
WINDOW_DAYS = 365
ROTATION_MONTHS = 2


@dataclass
class Candidate:
    user_id: int
    count: int
    last_approved: datetime


def _approved_claims():
    from iic_booking.equipment.models import EquipmentPublicationClaim, EquipmentPublicationClaimStatus

    return EquipmentPublicationClaim.objects.filter(
        status=EquipmentPublicationClaimStatus.APPROVED, submitted_by__is_active=True,
    )


def _ranked(claims) -> list[Candidate]:
    rows = (claims.values("submitted_by_id")
            .annotate(n=Count("claim_id"), last=Max("reviewed_at"))
            .order_by("-n", "-last", "submitted_by_id"))
    return [Candidate(r["submitted_by_id"], r["n"], r["last"]) for r in rows]


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _months_before(month: date, n: int) -> date:
    y, m = month.year, month.month - n
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1)


def latest_publication(user_id: int, claims=None):
    """The researcher's most recently approved publication (title, journal, year, link, equipment)."""
    claims = claims if claims is not None else _approved_claims()
    return (claims.filter(submitted_by_id=user_id).prefetch_related("equipments")
            .order_by("-reviewed_at", "-claim_id").first())


def card(user, *, count: int, claim, request=None) -> dict:
    """What the home page shows for one researcher (same shape for both features)."""
    from iic_booking.users.display import get_user_display_name

    link = ""
    if claim is not None:
        link = (claim.url or "").strip() or (f"https://doi.org/{claim.doi.strip()}" if (claim.doi or "").strip() else "")
    photo = None
    if getattr(user, "profile_picture", None):
        path = f"/api/users/{user.pk}/profile-picture/"
        photo = request.build_absolute_uri(path) if request is not None else path
    equipment = list(claim.equipments.all()) if claim is not None else []
    dept = getattr(user, "department", None)
    return {
        "id": user.pk,
        "user_id": user.pk,
        "researcher_name": get_user_display_name(user),
        "photo_url": photo,
        "affiliation": dept.name if dept is not None else "",
        "publication_count": count,
        "publication_title": claim.title if claim is not None else "",
        "research_summary": "",
        "journal": (claim.journal or "") if claim is not None else "",
        "year": claim.year if claim is not None else None,
        "publication_url": link,
        "facility": ", ".join(e.name for e in equipment),
        "equipment_id": equipment[0].pk if equipment else None,
    }


# --- Hall of Frames ------------------------------------------------------------------------------------------------

def hidden_from_hall() -> set[int]:
    from .models import RecognitionHide

    return set(RecognitionHide.objects.filter(scope=RecognitionHide.Scope.HALL).values_list("user_id", flat=True))


def hall_of_frames(*, request=None, include_hidden: bool = False, limit: int = HALL_LIMIT) -> list[dict]:
    from iic_booking.users.models import User

    claims = _approved_claims()
    hidden = hidden_from_hall()
    ranked = [c for c in _ranked(claims) if include_hidden or c.user_id not in hidden][:limit]
    users = User.objects.select_related("department").in_bulk([c.user_id for c in ranked])
    out = []
    for c in ranked:
        user = users.get(c.user_id)
        if user is None:
            continue
        row = card(user, count=c.count, claim=latest_publication(c.user_id, claims), request=request)
        if include_hidden:
            row["hidden"] = c.user_id in hidden
        out.append(row)
    return out


# --- Researcher of the Month ---------------------------------------------------------------------------------------

def _scopes() -> list[Optional[int]]:
    """None (institute-wide) and every internal department that has an approved publication."""
    from iic_booking.users.models.department import DepartmentType

    depts = (_approved_claims().filter(submitted_by__department__department_type=DepartmentType.INTERNAL)
             .values_list("submitted_by__department_id", flat=True).distinct())
    return [None, *sorted(d for d in depts if d is not None)]


def choose(month: date, department_id: Optional[int], *, exclude: set[int] = frozenset()) -> Optional[Candidate]:
    """The researcher for ``month`` and department (None: institute-wide): see the module docstring."""
    from .models import RecognitionHide, ResearcherOfTheMonth

    tz = timezone.get_current_timezone()
    end = timezone.make_aware(datetime.combine(month, time.min), tz)
    claims = _approved_claims().filter(reviewed_at__gte=end - timedelta(days=WINDOW_DAYS), reviewed_at__lt=end)
    if department_id is not None:
        claims = claims.filter(submitted_by__department_id=department_id)
    hidden = set(RecognitionHide.objects.filter(scope=RecognitionHide.Scope.MONTH, month=month,
                                                department_id=department_id).values_list("user_id", flat=True))
    candidates = [c for c in _ranked(claims) if c.user_id not in hidden and c.user_id not in exclude]
    if not candidates:
        return None
    recent = set(ResearcherOfTheMonth.objects.filter(
        department_id=department_id, auto=True, user__isnull=False,
        month__gte=_months_before(month, ROTATION_MONTHS), month__lt=month,
    ).values_list("user_id", flat=True))
    fresh = [c for c in candidates if c.user_id not in recent]
    return (fresh or candidates)[0]


def _store(month: date, department_id: Optional[int], cand: Optional[Candidate]):
    from iic_booking.users.models import User

    from .models import ResearcherOfTheMonth

    user = User.objects.select_related("department").get(pk=cand.user_id) if cand else None
    claim = latest_publication(cand.user_id) if cand else None
    fields = {
        "user": user, "auto": True, "is_approved": cand is not None,
        "publication_count": cand.count if cand else 0,
        "researcher_name": card(user, count=cand.count, claim=claim)["researcher_name"] if user else "",
        "affiliation": user.department.name if user and user.department_id else "",
        "publication_title": claim.title if claim else "",
        "journal": (claim.journal or "") if claim else "",
        "year": claim.year if claim else None,
        "publication_url": card(user, count=0, claim=claim)["publication_url"] if user else "",
        "facility": ", ".join(e.name for e in claim.equipments.all()) if claim else "",
    }
    row, created = ResearcherOfTheMonth.objects.get_or_create(month=month, department_id=department_id, defaults=fields)
    if not created and row.auto:
        for k, v in fields.items():
            setattr(row, k, v)
        row.save()
    return row


def ensure_month(month: Optional[date] = None) -> None:
    """Choose and store this month's researchers where not done yet (idempotent; safe under concurrent requests)."""
    from .models import ResearcherOfTheMonth

    month = _month_start(month or timezone.localdate())
    done = set(ResearcherOfTheMonth.objects.filter(month=month).values_list("department_id", flat=True))
    for dept in _scopes():
        if dept in done:
            continue
        cand = choose(month, dept)
        if cand is None:
            continue
        try:
            with transaction.atomic():
                _store(month, dept, cand)
        except IntegrityError:
            pass  # another request stored it first


def month_card(row, *, request=None) -> dict:
    """A stored Researcher of the Month for the home page (automatic rows use the researcher's profile photo)."""
    out = {
        "id": row.pk, "researcher_name": row.researcher_name, "photo_url": None, "affiliation": row.affiliation,
        "publication_title": row.publication_title, "research_summary": row.research_summary, "journal": row.journal,
        "year": row.year, "publication_url": row.publication_url, "facility": row.facility,
        "equipment_id": row.equipment_id, "publication_count": row.publication_count,
        "month": row.month.isoformat(), "department_id": row.department_id,
        "department_name": row.department.name if row.department_id else "Institute-wide",
        "user_id": row.user_id, "auto": row.auto, "shown": row.is_approved,
    }
    if row.photo:
        url = row.photo.url
        out["photo_url"] = request.build_absolute_uri(url) if request is not None and url.startswith("/") else url
    elif row.user_id and getattr(row.user, "profile_picture", None):
        path = f"/api/users/{row.user_id}/profile-picture/"
        out["photo_url"] = request.build_absolute_uri(path) if request is not None else path
    return out


# --- Main Administrator: take off / put back ----------------------------------------------------------------------

def hide_from_hall(user_id: int, *, actor, reason: str = "") -> None:
    from .models import RecognitionHide

    RecognitionHide.objects.get_or_create(scope=RecognitionHide.Scope.HALL, user_id=user_id,
                                          defaults={"created_by": actor, "reason": reason[:300]})


def show_in_hall(user_id: int) -> None:
    from .models import RecognitionHide

    RecognitionHide.objects.filter(scope=RecognitionHide.Scope.HALL, user_id=user_id).delete()


def disable_month(row_id: int, *, actor, reason: str = ""):
    """Take this month's researcher off and choose the next eligible one for the same month and department (the
    card is hidden when nobody else qualifies). Returns the updated row."""
    from .models import RecognitionHide, ResearcherOfTheMonth

    with transaction.atomic():
        row = ResearcherOfTheMonth.objects.select_for_update().get(pk=row_id)
        if row.user_id:
            RecognitionHide.objects.get_or_create(
                scope=RecognitionHide.Scope.MONTH, user_id=row.user_id, month=row.month,
                department_id=row.department_id, defaults={"created_by": actor, "reason": reason[:300]},
            )
        if not row.auto:
            row.is_approved = False
            row.save(update_fields=["is_approved", "updated_at"])
            return row
        return _store(row.month, row.department_id, choose(row.month, row.department_id))

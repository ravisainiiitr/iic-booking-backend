"""Publication claim APIs: user submit + faculty/OIC/Admin review → EquipmentPublication."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

from django.db import transaction
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response

from iic_booking.users.api.token_auth import TokenAuthenticationWithInactivity
from iic_booking.users.models.user_type import UserType

from .models import (
    Equipment,
    EquipmentPublication,
    EquipmentPublicationClaim,
    EquipmentPublicationClaimStatus,
)
from .reports import get_equipment_ids_managed_by_oic

logger = logging.getLogger(__name__)

_AUTH = (TokenAuthenticationWithInactivity, SessionAuthentication)

SUBMITTER_USER_TYPES = {
    UserType.STUDENT,
    UserType.INDIVIDUAL_STUDENT,
    UserType.FACULTY,
    UserType.EXTERNAL,
    UserType.RND,
    UserType.INSTITUTE,
    UserType.STARTUP_INCUBATED_IITR,
    UserType.EXTERNAL_STARTUP_MSME,
    UserType.OTHER,
}

_DOI_PREFIX_RE = re.compile(
    r"^(?:https?://)?(?:dx\.)?doi\.org/",
    re.IGNORECASE,
)


def normalize_doi(raw: str | None) -> str:
    if not raw:
        return ""
    s = str(raw).strip()
    s = _DOI_PREFIX_RE.sub("", s)
    s = s.strip().rstrip("/")
    return s


def build_citation(
    *,
    authors: str,
    title: str,
    journal: str,
    year,
    volume_pages: str,
    doi: str,
) -> str:
    parts: list[str] = []
    if authors:
        parts.append(authors.rstrip(".") + ".")
    if title:
        parts.append(title.rstrip(".") + ".")
    mid = " ".join(x for x in [journal, f"({year})" if year else "", volume_pages] if x).strip()
    if mid:
        parts.append(mid.rstrip(".") + ".")
    if doi:
        parts.append(f"https://doi.org/{doi}")
    return " ".join(parts).strip()


def _user_type(user) -> str:
    return str(getattr(user, "user_type", "") or "").lower()


def _is_admin(user) -> bool:
    return _user_type(user) == UserType.ADMIN


def _is_submitter(user) -> bool:
    return _user_type(user) in SUBMITTER_USER_TYPES


def _is_faculty(user) -> bool:
    return _user_type(user) == UserType.FACULTY


def _is_student(user) -> bool:
    return _user_type(user) in {UserType.STUDENT, UserType.INDIVIDUAL_STUDENT}


def _managed_equipment_ids(user) -> set[int]:
    if _is_admin(user):
        return set()
    try:
        return set(int(x) for x in get_equipment_ids_managed_by_oic(user.id))
    except Exception:
        logger.exception("Failed resolving OIC equipment for user %s", getattr(user, "id", None))
        return set()


def _resolve_faculty_supervisor(user):
    """Faculty wallet owner / join-request faculty for student publication review."""
    from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus

    if getattr(user, "supervisor_id", None):
        return getattr(user, "supervisor", None)

    jr = (
        WalletJoinRequest.objects.filter(
            student=user,
            status=WalletJoinRequestStatus.APPROVED,
        )
        .select_related("faculty", "wallet", "wallet__user")
        .order_by("-id")
        .first()
    )
    if jr:
        if jr.faculty_id and jr.faculty_id != user.id:
            return jr.faculty
        if jr.wallet and jr.wallet.user_id and jr.wallet.user_id != user.id:
            return jr.wallet.user
    get_wallet = getattr(user, "get_accessible_wallet", None)
    wallet = get_wallet() if callable(get_wallet) else None
    if wallet and wallet.user_id and wallet.user_id != user.id:
        from iic_booking.users.models import User

        return User.objects.filter(pk=wallet.user_id).first()
    return None


def _parse_impact_factor(raw):
    if raw in (None, ""):
        return None
    try:
        val = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError, TypeError):
        return "invalid"
    if val < 0:
        return "invalid"
    return val


def _can_review_claim(user, claim: EquipmentPublicationClaim) -> bool:
    """
    Faculty: student claims assigned to them.
    Admin/OIC: external-path claims only (no faculty-assigned student claims).
    """
    if claim.assigned_reviewer_id:
        return _is_faculty(user) and claim.assigned_reviewer_id == user.id

    if _is_admin(user):
        return True
    managed = _managed_equipment_ids(user)
    if not managed:
        return False
    claim_eq_ids = set(claim.equipments.values_list("equipment_id", flat=True))
    return bool(claim_eq_ids & managed)


def _filter_review_queryset(user, qs):
    """Scope review queue by role."""
    if _is_faculty(user) and not _is_admin(user):
        return qs.filter(assigned_reviewer=user)
    qs = qs.filter(assigned_reviewer__isnull=True)
    if _is_admin(user):
        return qs
    managed = _managed_equipment_ids(user)
    if not managed:
        return qs.none()
    return qs.filter(equipments__equipment_id__in=managed).distinct()


def _serialize_equipment(eq: Equipment) -> dict:
    return {
        "id": eq.equipment_id,
        "code": eq.code,
        "name": eq.name,
    }


def serialize_claim(claim: EquipmentPublicationClaim) -> dict:
    eqs = list(claim.equipments.all())
    submitter = claim.submitted_by
    reviewer = claim.reviewed_by
    return {
        "id": claim.claim_id,
        "title": claim.title,
        "authors": claim.authors,
        "journal": claim.journal,
        "year": claim.year,
        "volume_pages": claim.volume_pages,
        "doi": claim.doi,
        "url": claim.url,
        "facility_note": claim.facility_note,
        "impact_factor": str(claim.impact_factor) if claim.impact_factor is not None else None,
        "citation": claim.citation,
        "status": claim.status,
        "assigned_reviewer_id": claim.assigned_reviewer_id,
        "rejection_reason": claim.rejection_reason,
        "created_at": claim.created_at.isoformat() if claim.created_at else None,
        "updated_at": claim.updated_at.isoformat() if claim.updated_at else None,
        "reviewed_at": claim.reviewed_at.isoformat() if claim.reviewed_at else None,
        "equipments": [_serialize_equipment(e) for e in eqs],
        "submitted_by": {
            "id": submitter.id,
            "name": getattr(submitter, "name", None)
            or (submitter.get_full_name() if hasattr(submitter, "get_full_name") else "")
            or submitter.email,
            "email": submitter.email,
            "department": getattr(getattr(submitter, "department", None), "name", None),
        }
        if submitter
        else None,
        "reviewed_by": {
            "id": reviewer.id,
            "name": getattr(reviewer, "name", None) or reviewer.email,
        }
        if reviewer
        else None,
    }


def _parse_equipment_ids(raw) -> list[int]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return []
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = [x.strip() for x in raw.split(",") if x.strip()]
    if not isinstance(raw, (list, tuple)):
        raw = [raw]
    out: list[int] = []
    for item in raw:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    seen: set[int] = set()
    uniq: list[int] = []
    for i in out:
        if i not in seen:
            seen.add(i)
            uniq.append(i)
    return uniq


@api_view(["GET"])
@authentication_classes(_AUTH)
@permission_classes([permissions.IsAuthenticated])
def publication_doi_lookup(request):
    """Optional Crossref metadata fill for DOI-first forms."""
    doi = normalize_doi(request.query_params.get("doi") or request.query_params.get("q"))
    if not doi:
        return Response({"error": "DOI is required."}, status=status.HTTP_400_BAD_REQUEST)
    url = f"https://api.crossref.org/works/{urllib.parse.quote(doi)}"
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "IIC-Booking/1.0 (mailto:equip@iitr.ac.in)"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return Response({"error": "DOI not found."}, status=status.HTTP_404_NOT_FOUND)
        logger.warning("Crossref HTTP error for %s: %s", doi, exc)
        return Response({"error": "Could not look up DOI."}, status=status.HTTP_502_BAD_GATEWAY)
    except Exception:
        logger.exception("Crossref lookup failed for %s", doi)
        return Response({"error": "Could not look up DOI."}, status=status.HTTP_502_BAD_GATEWAY)

    msg = (payload or {}).get("message") or {}
    title_list = msg.get("title") or []
    title = title_list[0] if title_list else ""
    author_bits = []
    for a in msg.get("author") or []:
        given = (a.get("given") or "").strip()
        family = (a.get("family") or "").strip()
        name = f"{given} {family}".strip() or (a.get("name") or "").strip()
        if name:
            author_bits.append(name)
    container = msg.get("container-title") or []
    journal = container[0] if container else ""
    year = None
    for key in ("published-print", "published-online", "created"):
        parts = ((msg.get(key) or {}).get("date-parts") or [[]])[0]
        if parts:
            try:
                year = int(parts[0])
                break
            except (TypeError, ValueError):
                pass
    volume = (msg.get("volume") or "").strip()
    page = (msg.get("page") or "").strip()
    volume_pages = " ".join(x for x in [f"vol. {volume}" if volume else "", page] if x).strip()
    url_out = ""
    for link in msg.get("link") or []:
        if link.get("URL"):
            url_out = link["URL"]
            break
    if not url_out:
        url_out = f"https://doi.org/{doi}"

    return Response(
        {
            "doi": doi,
            "title": title,
            "authors": ", ".join(author_bits),
            "journal": journal,
            "year": year,
            "volume_pages": volume_pages,
            "url": url_out,
            "citation": build_citation(
                authors=", ".join(author_bits),
                title=title,
                journal=journal,
                year=year,
                volume_pages=volume_pages,
                doi=doi,
            ),
        }
    )


@api_view(["GET", "POST"])
@authentication_classes(_AUTH)
@permission_classes([permissions.IsAuthenticated])
def my_publication_claims(request):
    if request.method == "GET":
        qs = (
            EquipmentPublicationClaim.objects.filter(submitted_by=request.user)
            .prefetch_related("equipments")
            .select_related("submitted_by", "reviewed_by", "assigned_reviewer")
            .order_by("-created_at")
        )
        return Response({"results": [serialize_claim(c) for c in qs]})

    if not _is_submitter(request.user) and not _is_admin(request.user):
        return Response(
            {"error": "Only facility users can submit publication claims."},
            status=status.HTTP_403_FORBIDDEN,
        )

    data = request.data if hasattr(request, "data") else {}
    title = str(data.get("title") or "").strip()
    if not title:
        return Response({"error": "Title is required."}, status=status.HTTP_400_BAD_REQUEST)

    equipment_ids = _parse_equipment_ids(data.get("equipment_ids") or data.get("equipments"))
    if not equipment_ids:
        return Response({"error": "Select at least one instrument."}, status=status.HTTP_400_BAD_REQUEST)

    eqs = list(Equipment.objects.filter(equipment_id__in=equipment_ids))
    if len(eqs) != len(set(equipment_ids)):
        return Response({"error": "One or more instruments were not found."}, status=status.HTTP_400_BAD_REQUEST)

    doi = normalize_doi(data.get("doi"))
    authors = str(data.get("authors") or "").strip()
    journal = str(data.get("journal") or "").strip()
    volume_pages = str(data.get("volume_pages") or "").strip()
    url = str(data.get("url") or "").strip()
    facility_note = str(data.get("facility_note") or "").strip()
    year_raw = data.get("year")
    year = None
    if year_raw not in (None, ""):
        try:
            year = int(year_raw)
        except (TypeError, ValueError):
            return Response({"error": "Year must be a number."}, status=status.HTTP_400_BAD_REQUEST)

    impact_factor = _parse_impact_factor(data.get("impact_factor"))
    if impact_factor == "invalid":
        return Response(
            {"error": "Impact factor must be a non-negative number."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    citation = str(data.get("citation") or "").strip()
    if not citation:
        citation = build_citation(
            authors=authors,
            title=title,
            journal=journal,
            year=year,
            volume_pages=volume_pages,
            doi=doi,
        )
    if doi and not url:
        url = f"https://doi.org/{doi}"

    assigned_reviewer = None
    auto_approve = False
    approval_path = "external"
    if _is_faculty(request.user):
        auto_approve = True
        approval_path = "faculty_auto"
    elif _is_student(request.user):
        assigned_reviewer = _resolve_faculty_supervisor(request.user)
        if not assigned_reviewer:
            return Response(
                {
                    "error": "No faculty supervisor found. Link your wallet to a faculty member before submitting publications."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        approval_path = "faculty"

    with transaction.atomic():
        claim = EquipmentPublicationClaim.objects.create(
            submitted_by=request.user,
            title=title[:500],
            authors=authors[:1000],
            journal=journal[:500],
            year=year,
            volume_pages=volume_pages[:200],
            doi=doi[:200],
            url=url[:500],
            facility_note=facility_note,
            impact_factor=impact_factor,
            citation=citation,
            status=EquipmentPublicationClaimStatus.PENDING,
            assigned_reviewer=assigned_reviewer,
        )
        claim.equipments.set(eqs)
        if auto_approve:
            _approve_claim_to_publications(claim, request.user)
            claim.status = EquipmentPublicationClaimStatus.APPROVED
            claim.reviewed_by = request.user
            claim.reviewed_at = timezone.now()
            claim.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])

    claim = (
        EquipmentPublicationClaim.objects.filter(pk=claim.pk)
        .prefetch_related("equipments")
        .select_related("submitted_by", "reviewed_by", "assigned_reviewer")
        .get()
    )
    payload = serialize_claim(claim)
    payload["approval_path"] = approval_path
    return Response(payload, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@authentication_classes(_AUTH)
@permission_classes([permissions.IsAuthenticated])
def publication_claims_review_queue(request):
    """Pending (default) or filtered claims for faculty / OIC / Admin."""
    if not (
        _is_admin(request.user)
        or _is_faculty(request.user)
        or _managed_equipment_ids(request.user)
    ):
        return Response({"results": [], "pending_count": 0})

    status_filter = str(request.query_params.get("status") or EquipmentPublicationClaimStatus.PENDING).lower()
    qs = EquipmentPublicationClaim.objects.prefetch_related("equipments").select_related(
        "submitted_by", "reviewed_by", "assigned_reviewer", "submitted_by__department"
    )

    if status_filter and status_filter != "all":
        qs = qs.filter(status=status_filter)

    qs = _filter_review_queryset(request.user, qs).order_by("created_at")
    results = [serialize_claim(c) for c in qs]

    pending_qs = _filter_review_queryset(
        request.user,
        EquipmentPublicationClaim.objects.filter(status=EquipmentPublicationClaimStatus.PENDING),
    )
    return Response({"results": results, "pending_count": pending_qs.count()})


@api_view(["GET"])
@authentication_classes(_AUTH)
@permission_classes([permissions.IsAuthenticated])
def publication_claims_pending_count(request):
    if not (
        _is_admin(request.user)
        or _is_faculty(request.user)
        or _managed_equipment_ids(request.user)
    ):
        return Response({"pending_count": 0})
    pending_qs = _filter_review_queryset(
        request.user,
        EquipmentPublicationClaim.objects.filter(status=EquipmentPublicationClaimStatus.PENDING),
    )
    return Response({"pending_count": pending_qs.count()})


def _approve_claim_to_publications(claim: EquipmentPublicationClaim, reviewer) -> list[EquipmentPublication]:
    created: list[EquipmentPublication] = []
    doi = normalize_doi(claim.doi)
    url = (claim.url or "").strip()
    if doi and not url:
        url = f"https://doi.org/{doi}"
    citation = (claim.citation or "").strip() or build_citation(
        authors=claim.authors,
        title=claim.title,
        journal=claim.journal,
        year=claim.year,
        volume_pages=claim.volume_pages,
        doi=doi,
    )

    for eq in claim.equipments.all():
        if doi:
            exists = EquipmentPublication.objects.filter(equipment=eq, doi__iexact=doi).exists()
            if exists:
                continue
        pub = EquipmentPublication.objects.create(
            equipment=eq,
            title=claim.title[:500],
            citation=citation,
            url=url[:500],
            doi=doi[:200],
            year=claim.year,
            impact_factor=claim.impact_factor,
            display_order=0,
            submitted_by=claim.submitted_by,
            source_claim=claim,
        )
        created.append(pub)
    return created


@api_view(["POST"])
@authentication_classes(_AUTH)
@permission_classes([permissions.IsAuthenticated])
def publication_claim_approve(request, claim_id: int):
    try:
        claim = (
            EquipmentPublicationClaim.objects.prefetch_related("equipments")
            .select_related("submitted_by", "assigned_reviewer")
            .get(pk=claim_id)
        )
    except EquipmentPublicationClaim.DoesNotExist:
        return Response({"error": "Claim not found."}, status=status.HTTP_404_NOT_FOUND)

    if not _can_review_claim(request.user, claim):
        return Response({"error": "Not allowed to review this claim."}, status=status.HTTP_403_FORBIDDEN)

    if claim.status == EquipmentPublicationClaimStatus.APPROVED:
        return Response(serialize_claim(claim))

    if claim.status == EquipmentPublicationClaimStatus.REJECTED:
        return Response(
            {"error": "Rejected claims cannot be approved. Ask the user to resubmit."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        _approve_claim_to_publications(claim, request.user)
        claim.status = EquipmentPublicationClaimStatus.APPROVED
        claim.reviewed_by = request.user
        claim.reviewed_at = timezone.now()
        claim.rejection_reason = ""
        claim.save(
            update_fields=["status", "reviewed_by", "reviewed_at", "rejection_reason", "updated_at"]
        )

    claim = (
        EquipmentPublicationClaim.objects.filter(pk=claim.pk)
        .prefetch_related("equipments")
        .select_related("submitted_by", "reviewed_by", "assigned_reviewer")
        .get()
    )
    return Response(serialize_claim(claim))


@api_view(["POST"])
@authentication_classes(_AUTH)
@permission_classes([permissions.IsAuthenticated])
def publication_claim_reject(request, claim_id: int):
    try:
        claim = (
            EquipmentPublicationClaim.objects.prefetch_related("equipments")
            .select_related("submitted_by", "assigned_reviewer")
            .get(pk=claim_id)
        )
    except EquipmentPublicationClaim.DoesNotExist:
        return Response({"error": "Claim not found."}, status=status.HTTP_404_NOT_FOUND)

    if not _can_review_claim(request.user, claim):
        return Response({"error": "Not allowed to review this claim."}, status=status.HTTP_403_FORBIDDEN)

    if claim.status == EquipmentPublicationClaimStatus.APPROVED:
        return Response(
            {"error": "Approved claims cannot be rejected."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    reason = str((request.data or {}).get("rejection_reason") or (request.data or {}).get("reason") or "").strip()
    claim.status = EquipmentPublicationClaimStatus.REJECTED
    claim.reviewed_by = request.user
    claim.reviewed_at = timezone.now()
    claim.rejection_reason = reason
    claim.save(update_fields=["status", "reviewed_by", "reviewed_at", "rejection_reason", "updated_at"])

    claim = (
        EquipmentPublicationClaim.objects.filter(pk=claim.pk)
        .prefetch_related("equipments")
        .select_related("submitted_by", "reviewed_by", "assigned_reviewer")
        .get()
    )
    return Response(serialize_claim(claim))
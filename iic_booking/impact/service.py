"""Research impact services: access, funding lines, acknowledgement check, ORCID discovery and the funder report.

Publication discovery sends only the ORCID iD the researcher entered to OpenAlex (a free, public scholarly index);
nothing is claimed automatically: the researcher confirms each paper through the usual publication claim form.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date

from .models import (
    AcknowledgementCheck,
    AcknowledgementPolicy,
    EquipmentFunding,
    PublicationSuggestion,
    ResearcherOrcid,
    SuggestionStatus,
)

logger = logging.getLogger(__name__)

ORCID_RE = re.compile(r"^(\d{4})-?(\d{4})-?(\d{4})-?(\d{3}[\dX])$")
OPENALEX_URL = "https://api.openalex.org/works"
LOOKBACK_YEARS = 4
CHECK_COOLDOWN = timedelta(hours=1)
UNFUNDED = "Not recorded"


class ImpactError(Exception):
    def __init__(self, message: str, *, status: int = 400, code: str = "invalid"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


def require(ok: bool, message: str = "You do not have access to this.") -> None:
    if not ok:
        raise ImpactError(message, status=403, code="forbidden")


# --------------------------------------------------------------------------- access
def _type(user):
    from iic_booking.users.models.user_type import UserType

    return UserType, getattr(user, "user_type", None)


def is_admin(user) -> bool:
    T, t = _type(user)
    return bool(user and user.is_authenticated and t == T.ADMIN)


def report_equipment_ids(user) -> set[int] | None:
    """Funder reports and funding lines: Main Admin (all), OIC / temporary OIC (theirs), Dept Admin (department)."""
    from iic_booking.equipment.models import Equipment
    from iic_booking.equipment.reports import get_equipment_ids_managed_by_oic

    T, t = _type(user)
    if not user or not user.is_authenticated:
        return set()
    if t == T.ADMIN:
        return None
    if t == T.MANAGER:
        return {int(x) for x in get_equipment_ids_managed_by_oic(user.id)}
    if t == T.DEPT_ADMIN and getattr(user, "department_id", None):
        return set(Equipment.objects.filter(internal_department_id=user.department_id).values_list("pk", flat=True))
    return set()


def can_report(user, equipment_id=None) -> bool:
    ids = report_equipment_ids(user)
    if equipment_id is None:
        return ids is None or bool(ids)
    return ids is None or int(equipment_id) in ids


def can_manage_funding(user, equipment_id: int) -> bool:
    from iic_booking.equipment.reports import get_equipment_ids_managed_by_oic

    T, t = _type(user)
    return is_admin(user) or (t == T.MANAGER and int(equipment_id) in set(get_equipment_ids_managed_by_oic(user.id)))


# --------------------------------------------------------------------------- funding lines
def _money(raw):
    if raw in (None, ""):
        return None
    try:
        v = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise ImpactError("amount must be a number.") from None
    if v < 0:
        raise ImpactError("amount cannot be negative.")
    return v.quantize(Decimal("0.01"))


def _apply_funding(row: EquipmentFunding, data: dict, creating: bool) -> None:
    if creating or "funder" in data:
        funder = str(data.get("funder") or "").strip()
        if not funder:
            raise ImpactError("The funder is required.")
        row.funder = funder[:120]
    for f, n in (("scheme", 160), ("sanction_reference", 160), ("acknowledgement_phrase", 300), ("notes", 5000)):
        if f in data:
            setattr(row, f, str(data.get(f) or "").strip()[:n])
    if "sanction_date" in data:
        raw = data.get("sanction_date")
        row.sanction_date = parse_date(str(raw)) if raw else None
        if raw and row.sanction_date is None:
            raise ImpactError("sanction_date must be a date (YYYY-MM-DD).")
    if "amount" in data:
        row.amount = _money(data.get("amount"))


def add_funding(equipment, actor, data: dict) -> EquipmentFunding:
    require(can_manage_funding(actor, equipment.pk), "Only the equipment's OIC or the Main Admin can record funding.")
    row = EquipmentFunding(equipment=equipment, created_by=actor)
    _apply_funding(row, data, True)
    row.save()
    return row


def update_funding(row: EquipmentFunding, actor, data: dict) -> EquipmentFunding:
    require(can_manage_funding(actor, row.equipment_id), "Only the equipment's OIC or the Main Admin can change funding.")
    _apply_funding(row, data, False)
    if "is_active" in data:
        row.is_active = data["is_active"] in (True, "true", "1", 1)
    row.save()
    return row


# --------------------------------------------------------------------------- acknowledgement
def policy() -> AcknowledgementPolicy | None:
    return AcknowledgementPolicy.objects.order_by("-updated_at").first()


def save_policy(actor, data: dict) -> AcknowledgementPolicy:
    require(is_admin(actor), "Only the Main Admin can set the acknowledgement wording.")
    row = policy() or AcknowledgementPolicy(wording="")
    if "wording" in data:
        row.wording = str(data.get("wording") or "").strip()[:4000]
    if "required_phrases" in data:
        phrases = data.get("required_phrases") or []
        if isinstance(phrases, str):
            phrases = [p for p in phrases.splitlines()]
        if not isinstance(phrases, list):
            raise ImpactError("required_phrases must be a list.")
        row.required_phrases = [str(p).strip()[:300] for p in phrases if str(p).strip()][:20]
    if "include_equipment_name" in data:
        row.include_equipment_name = data["include_equipment_name"] in (True, "true", "1", 1)
    if not row.wording:
        raise ImpactError("The acknowledgement wording is required.")
    row.updated_by = actor
    row.save()
    return row


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _present(text_n: str, phrase: str) -> bool:
    return any(_norm(alt) and f" {_norm(alt)} " in f" {text_n} " for alt in phrase.split("|"))


def check_acknowledgement(text: str, equipment_ids=()) -> dict:
    """Which required phrases the acknowledgement contains: facility wording, funders of the instruments used, and
    (when the policy asks) an instrument name. Matching ignores case, punctuation and spacing."""
    from iic_booking.equipment.models import Equipment

    text_n = _norm(text)
    pol = policy()
    required: list[tuple[str, str]] = [(p, "Facility") for p in (pol.required_phrases if pol else [])]
    eqs = list(Equipment.objects.filter(pk__in=list(equipment_ids or [])))
    for f in EquipmentFunding.objects.filter(equipment__in=eqs, is_active=True).exclude(acknowledgement_phrase=""):
        label = f"{f.funder} {f.scheme}".strip()
        if (f.acknowledgement_phrase, label) not in required:
            required.append((f.acknowledgement_phrase, label))
    if pol and pol.include_equipment_name and eqs:
        required.append(("|".join(x for e in eqs for x in (e.name, e.code) if x), "Instrument"))
    found = [{"phrase": p, "for": who} for p, who in required if _present(text_n, p)]
    missing = [{"phrase": p, "for": who} for p, who in required if not _present(text_n, p)]
    if not text_n:
        result = "MISSING"
    elif not required:
        result = "COMPLETE"
    else:
        result = "COMPLETE" if not missing else ("PARTIAL" if found else "MISSING")
    return {"result": result, "found": found, "missing": missing, "wording": pol.wording if pol else ""}


def store_claim_check(claim, actor, text: str) -> AcknowledgementCheck:
    """Submitter or a reviewer of the claim stores the acknowledgement text and its check on the claim."""
    is_owner = claim.submitted_by_id == getattr(actor, "pk", None)
    ids = report_equipment_ids(actor)
    eq_ids = list(claim.equipments.values_list("pk", flat=True))
    reviewer = ids is None or bool(ids and set(eq_ids) & ids) or claim.assigned_reviewer_id == getattr(actor, "pk", None)
    require(is_owner or reviewer, "Only the submitter or a reviewer can check this claim.")
    res = check_acknowledgement(text, eq_ids)
    row, _ = AcknowledgementCheck.objects.update_or_create(
        claim=claim, defaults={"text": (text or "")[:10000], "result": res["result"], "found": res["found"],
                               "missing": res["missing"], "checked_by": actor},
    )
    return row


# --------------------------------------------------------------------------- ORCID and discovery
def normalise_orcid(raw: str) -> str:
    value = (raw or "").strip().upper()
    value = re.sub(r"^HTTPS?://(WWW\.)?ORCID\.ORG/", "", value)
    m = ORCID_RE.match(value)
    if not m:
        raise ImpactError("Enter an ORCID iD like 0000-0002-1825-0097.", code="invalid_orcid")
    digits = "".join(m.groups())
    total = 0
    for ch in digits[:-1]:
        total = (total + int(ch)) * 2
    check = (12 - total % 11) % 11
    if digits[-1] != ("X" if check == 10 else str(check)):
        raise ImpactError("This ORCID iD has a typing mistake (the check digit does not match).", code="invalid_orcid")
    return "-".join(m.groups())


def set_orcid(user, raw: str, *, discovery_enabled: bool = True) -> ResearcherOrcid:
    orcid = normalise_orcid(raw)
    taken = ResearcherOrcid.objects.filter(orcid=orcid).exclude(user=user).exists()
    if taken:
        raise ImpactError("This ORCID iD is already linked to another account.", status=409, code="orcid_taken")
    row, _ = ResearcherOrcid.objects.update_or_create(user=user, defaults={"orcid": orcid, "discovery_enabled": discovery_enabled})
    return row


def remove_orcid(user) -> None:
    ResearcherOrcid.objects.filter(user=user).delete()
    PublicationSuggestion.objects.filter(user=user, status=SuggestionStatus.NEW).delete()


def _fetch_openalex(orcid: str, since: date) -> list[dict]:
    params = {
        "filter": f"author.orcid:https://orcid.org/{orcid},from_publication_date:{since.isoformat()}",
        "per-page": "50",
        "select": "id,doi,title,publication_year,primary_location,authorships",
    }
    mailto = getattr(settings, "OPENALEX_MAILTO", "")
    if mailto:
        params["mailto"] = mailto
    url = f"{OPENALEX_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "facility-booking-portal (publication discovery)"})
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - fixed https host
        body = json.loads(resp.read().decode("utf-8"))
    return list(body.get("results") or [])


def _claimed_dois() -> set[str]:
    from iic_booking.equipment.models import EquipmentPublication, EquipmentPublicationClaim
    from iic_booking.equipment.publication_dedupe import normalize_doi

    dois = {normalize_doi(d) for d in EquipmentPublicationClaim.objects.exclude(doi="").values_list("doi", flat=True)}
    dois |= {normalize_doi(d) for d in EquipmentPublication.objects.exclude(doi="").exclude(doi__isnull=True).values_list("doi", flat=True)}
    return {d for d in dois if d}


def _booked_equipment(user, before_year: int | None) -> list[int]:
    from iic_booking.equipment.models import Booking

    qs = Booking.objects.filter(user=user)
    if before_year:
        qs = qs.filter(created_at__year__lte=before_year)
    return sorted(set(qs.values_list("equipment_id", flat=True)))[:20]


def discover(user, *, force: bool = False) -> dict:
    """Ask OpenAlex for the researcher's recent works; store new ones as suggestions."""
    from iic_booking.equipment.publication_dedupe import normalize_doi

    row = ResearcherOrcid.objects.filter(user=user).first()
    if row is None:
        raise ImpactError("Add your ORCID iD first.", code="orcid_required")
    now = timezone.now()
    if not force and row.last_checked_at and now - row.last_checked_at < CHECK_COOLDOWN:
        raise ImpactError("Checked less than an hour ago; try again later.", status=429, code="too_soon")
    since = date(timezone.localdate().year - LOOKBACK_YEARS, 1, 1)
    try:
        works = _fetch_openalex(row.orcid, since)
    except Exception as exc:  # network, HTTP or JSON error
        logger.warning("openalex lookup failed user=%s: %s", user.pk, exc)
        row.last_checked_at, row.last_error = now, str(exc)[:300]
        row.save(update_fields=["last_checked_at", "last_error", "updated_at"])
        raise ImpactError("The publication index could not be reached; try again later.", status=502, code="lookup_failed") from None
    claimed = _claimed_dois()
    added = 0
    for w in works:
        ext = str(w.get("id") or "")[:120]
        title = (w.get("title") or "").strip()
        if not ext or not title:
            continue
        doi = normalize_doi(w.get("doi") or "")
        if doi and doi in claimed:
            continue
        if PublicationSuggestion.objects.filter(user=user, source="openalex", external_id=ext).exists():
            continue
        if doi and PublicationSuggestion.objects.filter(user=user, doi=doi).exists():
            continue
        loc = w.get("primary_location") or {}
        journal = ((loc.get("source") or {}) or {}).get("display_name") or ""
        names = [((a.get("author") or {}).get("display_name") or "") for a in (w.get("authorships") or [])]
        year = w.get("publication_year")
        PublicationSuggestion.objects.create(
            user=user, source="openalex", external_id=ext, doi=doi, title=title[:500], journal=journal[:500],
            year=year if isinstance(year, int) else None, authors=", ".join(n for n in names if n)[:5000],
            equipment_ids=_booked_equipment(user, year if isinstance(year, int) else None),
        )
        added += 1
    row.last_checked_at, row.last_error = now, ""
    row.save(update_fields=["last_checked_at", "last_error", "updated_at"])
    sync_claimed(user)
    return {"found": len(works), "added": added}


def sync_claimed(user) -> None:
    """Suggestions whose DOI has since been claimed are marked claimed."""
    claimed = _claimed_dois()
    for s in PublicationSuggestion.objects.filter(user=user, status=SuggestionStatus.NEW).exclude(doi=""):
        if s.doi in claimed:
            s.status, s.decided_at = SuggestionStatus.CLAIMED, timezone.now()
            s.save(update_fields=["status", "decided_at"])


def dismiss(suggestion: PublicationSuggestion, user) -> PublicationSuggestion:
    require(suggestion.user_id == user.pk)
    suggestion.status, suggestion.decided_at = SuggestionStatus.DISMISSED, timezone.now()
    suggestion.save(update_fields=["status", "decided_at"])
    return suggestion


def weekly_discovery(limit: int = 500) -> dict:
    done = failed = added = 0
    stale = timezone.now() - timedelta(days=6)
    rows = ResearcherOrcid.objects.filter(discovery_enabled=True, user__is_active=True).filter(
        Q(last_checked_at__isnull=True) | Q(last_checked_at__lt=stale)).select_related("user")[:limit]
    for row in rows:
        try:
            added += discover(row.user, force=True)["added"]
            done += 1
        except ImpactError:
            failed += 1
    return {"checked": done, "failed": failed, "added": added}


# --------------------------------------------------------------------------- funder report
def _period(date_from, date_to) -> tuple[date, date]:
    today = timezone.localdate()
    start = parse_date(str(date_from)) if date_from else date(today.year - 1, 4, 1) if today.month < 4 else date(today.year, 4, 1)
    end = parse_date(str(date_to)) if date_to else today
    if start is None or end is None:
        raise ImpactError("date_from and date_to must be dates (YYYY-MM-DD).")
    if end < start:
        raise ImpactError("date_to is before date_from.")
    if (end - start).days > 366 * 5:
        raise ImpactError("The period can be at most five years.")
    return start, end


def funder_report(user, *, date_from=None, date_to=None, funder: str = "", scheme: str = "", equipment_ids=None) -> dict:
    from iic_booking.equipment.models import Equipment, EquipmentPublicationClaim, EquipmentPublicationClaimStatus
    from iic_booking.equipment.publication_dedupe import normalize_doi, unique_publications
    from iic_booking.equipment.reports import get_equipment_report_data

    require(can_report(user), "Funder reports are for the Main Admin, OICs and Department Admins.")
    start, end = _period(date_from, date_to)
    allowed = report_equipment_ids(user)
    lines = EquipmentFunding.objects.filter(is_active=True).select_related("equipment")
    if funder:
        lines = lines.filter(funder__iexact=funder.strip())
    if scheme:
        lines = lines.filter(scheme__iexact=scheme.strip())
    eq_qs = Equipment.objects.all()
    if allowed is not None:
        eq_qs = eq_qs.filter(pk__in=allowed)
        lines = lines.filter(equipment_id__in=allowed)
    if equipment_ids:
        wanted = {int(x) for x in equipment_ids}
        eq_qs = eq_qs.filter(pk__in=wanted)
        lines = lines.filter(equipment_id__in=wanted)
    if funder or scheme:
        eq_qs = eq_qs.filter(pk__in=lines.values("equipment_id"))
    eq_list = list(eq_qs.order_by("name"))
    if not eq_list:
        return {"date_from": start.isoformat(), "date_to": end.isoformat(), "groups": [], "totals": {}, "equipment_count": 0}

    usage = get_equipment_report_data(start.isoformat(), end.isoformat(), [e.pk for e in eq_list], include_revenue=True)
    by_eq = {row["equipment_id"]: row for row in usage.get("equipment", [])}
    revenue = {}
    for r in (usage.get("financial") or {}).get("revenue_by_equipment", []) or []:
        key = r.get("equipment_id")
        if key is not None:
            revenue[key] = float(r.get("total") or r.get("revenue") or 0)

    # Unique publications in the period's years, per instrument; impact factor from approved claims by DOI.
    years = set(range(start.year, end.year + 1))
    from iic_booking.equipment.models import EquipmentPublication

    ifs = {}
    for doi, impact in EquipmentPublicationClaim.objects.filter(status=EquipmentPublicationClaimStatus.APPROVED).exclude(doi="").values_list("doi", "impact_factor"):
        if impact is not None:
            ifs[normalize_doi(doi)] = float(impact)
    for doi, impact in EquipmentPublication.objects.exclude(doi="").exclude(impact_factor__isnull=True).values_list("doi", "impact_factor"):
        ifs[normalize_doi(doi)] = float(impact)
    pubs_by_eq: dict[int, list[dict]] = {}
    for p in unique_publications():
        if p.get("year") not in years:
            continue
        for inst in p.get("equipment", []):
            pubs_by_eq.setdefault(inst["id"], []).append(p)

    trained = {}
    try:
        from iic_booking.training.models import CertificationAward

        for eq_id in CertificationAward.objects.filter(equipment__in=eq_list, awarded_at__date__gte=start,
                                                        awarded_at__date__lte=end).values_list("equipment_id", flat=True):
            trained[eq_id] = trained.get(eq_id, 0) + 1
    except Exception:  # pragma: no cover - training app always installed
        pass

    def eq_row(e) -> dict:
        u = by_eq.get(e.pk, {})
        pubs = pubs_by_eq.get(e.pk, [])
        return {
            "equipment_id": e.pk, "name": e.name, "code": e.code,
            "bookings": u.get("completed_in_period", u.get("total_bookings_in_period", 0)) or 0,
            "booking_hours": round(float(u.get("total_booking_hours") or 0), 1),
            "users_internal": u.get("distinct_users_internal", 0) or 0,
            "users_external": u.get("distinct_users_external", 0) or 0,
            "samples": u.get("total_samples", 0) or 0,
            "revenue": round(revenue.get(e.pk, 0.0), 2),
            "publications": len(pubs),
            "publication_keys": [p.get("doi") or f"id:{p['id']}" for p in pubs],
            "impact_factor_sum": round(sum(ifs.get(p.get("doi") or "", 0.0) for p in pubs), 2),
            "people_authorised": trained.get(e.pk, 0),
            "downtime_hours": round(float(u.get("other_disruption_hours") or 0) + float(u.get("blocked_hours") or 0), 1),
        }

    rows = {e.pk: eq_row(e) for e in eq_list}
    groups: dict[tuple[str, str], dict] = {}
    funded = set()
    for line in lines:
        if line.equipment_id not in rows:
            continue
        funded.add(line.equipment_id)
        g = groups.setdefault((line.funder, line.scheme), {"funder": line.funder, "scheme": line.scheme, "lines": [], "equipment": []})
        g["lines"].append({"equipment_id": line.equipment_id, "sanction_reference": line.sanction_reference,
                           "sanction_date": line.sanction_date.isoformat() if line.sanction_date else None,
                           "amount": float(line.amount) if line.amount is not None else None})
        if rows[line.equipment_id] not in g["equipment"]:
            g["equipment"].append(rows[line.equipment_id])
    if not (funder or scheme):
        rest = [rows[i] for i in rows if i not in funded]
        if rest:
            groups[(UNFUNDED, "")] = {"funder": UNFUNDED, "scheme": "", "lines": [], "equipment": rest}

    def total(eqs: list[dict]) -> dict:
        keys = {k for e in eqs for k in e.get("publication_keys", [])}
        return {
            "equipment": len(eqs),
            "bookings": sum(e["bookings"] for e in eqs),
            "booking_hours": round(sum(e["booking_hours"] for e in eqs), 1),
            "users_internal": sum(e["users_internal"] for e in eqs),
            "users_external": sum(e["users_external"] for e in eqs),
            "samples": sum(e["samples"] for e in eqs),
            "revenue": round(sum(e["revenue"] for e in eqs), 2),
            "publications_unique": len(keys),
            "impact_factor_sum": round(sum(ifs.get(k, 0.0) for k in keys), 2),
            "people_authorised": sum(e["people_authorised"] for e in eqs),
            "sanctioned_amount": None,
        }

    out_groups = []
    for (fu, sc), g in sorted(groups.items(), key=lambda kv: (kv[0][0] == UNFUNDED, kv[0][0].lower(), kv[0][1].lower())):
        t = total(g["equipment"])
        amounts = [ln["amount"] for ln in g["lines"] if ln["amount"] is not None]
        t["sanctioned_amount"] = round(sum(amounts), 2) if amounts else None
        out_groups.append({**g, "totals": t})
    return {
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "groups": out_groups,
        "totals": total(list(rows.values())),
        "equipment_count": len(rows),
        "notes": [
            "Users are distinct people per instrument; the overall total counts a person once per instrument used.",
            "Publications are unique papers (by DOI, else title and year) with a year inside the period.",
            "Impact factor is the sum over unique papers whose approved claim records one.",
        ],
    }


EXPORT_COLUMNS = [
    ("funder", "Funder"), ("scheme", "Scheme"), ("code", "Equipment code"), ("name", "Equipment"),
    ("bookings", "Bookings completed"), ("booking_hours", "Hours used"), ("users_internal", "Internal users"),
    ("users_external", "External users"), ("samples", "Samples"), ("revenue", "Revenue (INR)"),
    ("publications", "Publications"), ("impact_factor_sum", "Impact factor (sum)"),
    ("people_authorised", "People authorised"), ("downtime_hours", "Downtime (h)"),
]


def export_rows(report: dict) -> list[list]:
    rows = []
    for g in report["groups"]:
        for e in g["equipment"]:
            rows.append([g["funder"], g["scheme"]] + [e[k] for k, _ in EXPORT_COLUMNS[2:]])
    return rows

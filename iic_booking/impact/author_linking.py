"""Find a portal user's OpenAlex author record by name and the institute's ROR ID (for faculty without an ORCID iD).

Only an unambiguous match is linked automatically: one candidate at confidence >= AUTO_LINK_AT whose runner-up is below
RUNNER_UP_BELOW (or whose ORCID equals the user's saved ORCID iD). Every other plausible candidate is stored as
NEEDS_CONFIRMATION for the researcher or an administrator to confirm or reject. Linked author records feed publication
discovery like an ORCID iD does.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
import urllib.parse
import urllib.request
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import AuthorLinkStatus, ResearcherAuthorCandidate, ResearcherOrcid

logger = logging.getLogger(__name__)

AUTHORS_URL = "https://api.openalex.org/authors"
WORKS_URL = "https://api.openalex.org/works"
USER_AGENT = "facility-booking-portal (author matching)"
AUTO_LINK_AT = 0.9
RUNNER_UP_BELOW = 0.6
KEEP_AT = 0.5
TITLES = {"prof", "professor", "dr", "mr", "mrs", "ms", "miss", "shri", "smt", "er", "sir"}


class AuthorLinkError(Exception):
    pass


def institute_ror() -> str:
    raw = (getattr(settings, "INSTITUTE_ROR_ID", "") or "").strip()
    return raw if not raw or raw.startswith("https://ror.org/") else f"https://ror.org/{raw.rsplit('/', 1)[-1]}"


def name_tokens(name: str) -> list[str]:
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    tokens = [t for t in re.split(r"[^a-z]+", text.lower()) if t]
    return [t for t in tokens if t not in TITLES]


def name_score(user_name: str, candidate_names: list[str]) -> tuple[float, str]:
    """1.0 same name; 0.95 same words in another order; 0.75 initials that fit; 0.4 surname only; else 0."""
    u = name_tokens(user_name)
    if not u:
        return 0.0, "no usable name"
    best, why = 0.0, "names differ"
    for raw in candidate_names:
        c = name_tokens(raw)
        if not c:
            continue
        if c == u:
            return 1.0, f'same name as "{raw}"'
        if sorted(c) == sorted(u):
            best, why = max(best, 0.95), f'same words as "{raw}"'
            continue
        if c[-1] == u[-1]:
            fu, fc = u[:-1], c[:-1]
            fits = bool(fu) and bool(fc) and len(fu) == len(fc) and all(
                a == b or (len(a) == 1 and b.startswith(a)) or (len(b) == 1 and a.startswith(b)) for a, b in zip(fu, fc))
            if fits and 0.75 > best:
                best, why = 0.75, f'initials fit "{raw}"'
            elif 0.4 > best:
                best, why = 0.4, f'surname only matches "{raw}"'
    return best, why


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - fixed https host
        return json.loads(resp.read().decode("utf-8"))


def search_openalex_authors(name: str, ror: str) -> list[dict]:
    params = {
        "search": " ".join(name_tokens(name)),
        "filter": f"affiliations.institution.ror:{ror}",
        "per-page": "25",
        "select": "id,display_name,display_name_alternatives,orcid,works_count,last_known_institutions,affiliations",
    }
    mailto = getattr(settings, "OPENALEX_MAILTO", "")
    if mailto:
        params["mailto"] = mailto
    return list(_get(f"{AUTHORS_URL}?{urllib.parse.urlencode(params)}").get("results") or [])


def score_candidates(user, authors: list[dict], ror: str) -> list[dict]:
    saved = ResearcherOrcid.objects.filter(user=user).values_list("orcid", flat=True).first() or ""
    out = []
    for a in authors:
        orcid = (a.get("orcid") or "").rsplit("/", 1)[-1]
        names = [a.get("display_name") or ""] + list(a.get("display_name_alternatives") or [])
        score, why = name_score(getattr(user, "name", "") or "", names)
        reasons = [why]
        current = any((i or {}).get("ror") == ror for i in a.get("last_known_institutions") or [])
        if not current:
            score *= 0.8
            reasons.append("the institute is a past affiliation only")
        if (a.get("works_count") or 0) < 3:
            score *= 0.9
            reasons.append("fewer than 3 works")
        if saved and orcid:
            if orcid == saved:
                score, reasons = 1.0, [f"ORCID {orcid} equals the user's saved ORCID iD"]
            else:
                score *= 0.3
                reasons.append(f"ORCID {orcid} differs from the user's saved ORCID iD")
        out.append({
            "openalex_author_id": (a.get("id") or "").rsplit("/", 1)[-1], "display_name": a.get("display_name") or "",
            "orcid": orcid, "works_count": a.get("works_count") or 0,
            "institutions": [(i or {}).get("display_name") for i in a.get("last_known_institutions") or []][:5],
            "confidence": round(score, 3), "rationale": "; ".join(reasons),
        })
    return sorted(out, key=lambda c: -c["confidence"])


def decide(cands: list[dict]) -> list[dict]:
    kept = [c for c in cands if c["confidence"] >= KEEP_AT]
    for c in kept:
        c["status"] = AuthorLinkStatus.NEEDS_CONFIRMATION
    if kept and kept[0]["confidence"] >= AUTO_LINK_AT and (len(kept) == 1 or kept[1]["confidence"] < RUNNER_UP_BELOW):
        kept[0]["status"] = AuthorLinkStatus.AUTO_LINKED
    if kept and kept[0]["confidence"] == 1.0 and "equals the user's saved ORCID" in kept[0]["rationale"]:
        kept[0]["status"] = AuthorLinkStatus.AUTO_LINKED
    return kept


def link_user(user, *, apply: bool, ror: str | None = None, search=search_openalex_authors) -> list[dict]:
    """Candidates for ``user`` with the decision for each. With ``apply``, store them; an administrator's or the
    user's earlier decision (CONFIRMED / REJECTED) is never overwritten."""
    ror = ror or institute_ror()
    if not ror:
        raise AuthorLinkError("INSTITUTE_ROR_ID is not set.")
    cands = decide(score_candidates(user, search(getattr(user, "name", "") or "", ror), ror))
    if apply:
        with transaction.atomic():
            for c in cands:
                row = ResearcherAuthorCandidate.objects.filter(user=user, openalex_author_id=c["openalex_author_id"]).first()
                fields = {k: c[k] for k in ("display_name", "orcid", "works_count", "institutions", "rationale")}
                fields["confidence"] = Decimal(str(c["confidence"]))
                if row is None:
                    ResearcherAuthorCandidate.objects.create(user=user, openalex_author_id=c["openalex_author_id"],
                                                             status=c["status"], **fields)
                else:
                    if row.status not in (AuthorLinkStatus.CONFIRMED, AuthorLinkStatus.REJECTED):
                        fields["status"] = c["status"]
                    for k, v in fields.items():
                        setattr(row, k, v)
                    row.save()
    return cands


def decide_candidate(candidate: ResearcherAuthorCandidate, actor, *, confirm: bool) -> ResearcherAuthorCandidate:
    candidate.status = AuthorLinkStatus.CONFIRMED if confirm else AuthorLinkStatus.REJECTED
    candidate.decided_by, candidate.decided_at = actor, timezone.now()
    candidate.save(update_fields=["status", "decided_by", "decided_at", "updated_at"])
    return candidate


def works_for_author(author_id: str, since: date, max_pages: int = 5) -> list[dict]:
    from .service import PAPER_TYPES, WORK_SELECT

    params = {"filter": f"author.id:{author_id},from_publication_date:{since.isoformat()}", "per-page": "200",
              "select": WORK_SELECT}
    works, cursor = [], "*"
    for _ in range(max_pages):
        body = _get(f"{WORKS_URL}?{urllib.parse.urlencode({**params, 'cursor': cursor})}")
        page = list(body.get("results") or [])
        works.extend(page)
        cursor = (body.get("meta") or {}).get("next_cursor")
        if not page or not cursor:
            break
    return [w for w in works if not w.get("type") or w.get("type") in PAPER_TYPES]


def discover_linked(user, *, since: date | None = None) -> dict:
    """Suggest the works of every linked (automatic or confirmed) author record of ``user``."""
    from .service import LOOKBACK_YEARS, store_suggestions

    since = since or date(timezone.localdate().year - LOOKBACK_YEARS, 1, 1)
    added = found = 0
    for c in ResearcherAuthorCandidate.objects.filter(
            user=user, status__in=[AuthorLinkStatus.AUTO_LINKED, AuthorLinkStatus.CONFIRMED]):
        works = works_for_author(c.openalex_author_id, since)
        found += len(works)
        added += store_suggestions(user, works)
        c.last_checked_at = timezone.now()
        c.save(update_fields=["last_checked_at", "updated_at"])
    return {"found": found, "added": added}

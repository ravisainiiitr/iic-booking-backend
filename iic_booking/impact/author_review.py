"""Confirming which OpenAlex author record is which faculty member, when the name search found lookalikes.

Name + institute search links a faculty member automatically only when one record clearly fits (or its ORCID iD
equals the one they saved). Otherwise every plausible record waits as NEEDS_CONFIRMATION and that researcher's
papers are not scanned. Who may decide:

* the researcher themself (the "Is this you?" prompt);
* the Main Administrator (everyone);
* a Department Administrator (faculty of their own department).

A person may have several OpenAlex records (split profiles), so more than one can be confirmed. Confirming starts
discovery of that record's recent papers and, when publication mapping is on, the acknowledgement check, so
probable matches reach the review queue without waiting for the nightly scan. Earlier decisions are never
overwritten by later automatic searches (author_linking.link_user).
"""

from __future__ import annotations

import logging
import urllib.parse

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from .match_review import reviewer_department
from .models import AuthorLinkStatus, ResearcherAuthorCandidate, ResearcherOrcid

logger = logging.getLogger(__name__)

LINKED = (AuthorLinkStatus.AUTO_LINKED, AuthorLinkStatus.CONFIRMED)
WORKS_CACHE_SECONDS = 24 * 3600


class AuthorReviewError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "invalid"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


# --------------------------------------------------------------------------- access
def can_decide_for(actor, researcher) -> bool:
    if not (actor and actor.is_authenticated):
        return False
    if actor.pk == researcher.pk:
        return True
    scope = reviewer_department(actor)
    return scope is True or (bool(scope) and scope == getattr(researcher, "department_id", None))


def _scoped_users(actor):
    User = get_user_model()
    scope = reviewer_department(actor)
    if scope is None:
        return User.objects.none()
    qs = User.objects.filter(is_active=True)
    return qs if scope is True else qs.filter(department_id=scope)


def _with_flags(qs):
    pending = ResearcherAuthorCandidate.objects.filter(user=OuterRef("pk"), status=AuthorLinkStatus.NEEDS_CONFIRMATION)
    linked = ResearcherAuthorCandidate.objects.filter(user=OuterRef("pk"), status__in=LINKED)
    return qs.annotate(has_pending=Exists(pending), has_link=Exists(linked))


def waiting_users(actor):
    """Researchers in the actor's scope with lookalike records still to decide (not yet linked first)."""
    return (_with_flags(_scoped_users(actor)).filter(has_pending=True)
            .select_related("department").order_by("has_link", "name", "pk"))


def waiting_count(actor) -> int:
    """Researchers in scope who have no linked record and lookalikes waiting: their papers are not scanned yet."""
    return waiting_users(actor).filter(has_link=False).count()


# --------------------------------------------------------------------------- output
def candidate_out(c: ResearcherAuthorCandidate) -> dict:
    return {
        "id": c.pk, "openalex_author_id": c.openalex_author_id,
        "openalex_url": f"https://openalex.org/{c.openalex_author_id}", "display_name": c.display_name,
        "orcid": c.orcid, "works_count": c.works_count, "institutions": c.institutions or [],
        "confidence": float(c.confidence), "rationale": c.rationale, "status": c.status,
        "decided_at": c.decided_at.isoformat() if c.decided_at else None,
    }


def researcher_out(user, actor=None) -> dict:
    cands = list(ResearcherAuthorCandidate.objects.filter(user=user).order_by("-confidence", "-works_count", "pk"))
    dept = getattr(user, "department", None)
    return {
        "researcher": {
            "id": user.pk, "name": getattr(user, "name", "") or user.email,
            "department": getattr(dept, "name", "") or "", "department_code": getattr(dept, "code", "") or "",
            "orcid": ResearcherOrcid.objects.filter(user=user).values_list("orcid", flat=True).first() or "",
        },
        "waiting": [candidate_out(c) for c in cands if c.status == AuthorLinkStatus.NEEDS_CONFIRMATION],
        "linked": [candidate_out(c) for c in cands if c.status in LINKED],
        "rejected": [candidate_out(c) for c in cands if c.status == AuthorLinkStatus.REJECTED],
        "can_decide": can_decide_for(actor, user) if actor is not None else True,
    }


# --------------------------------------------------------------------------- recent papers (to tell lookalikes apart)
def recent_works(candidate: ResearcherAuthorCandidate, limit: int = 5) -> list[dict]:
    key = f"impact:author-works:{candidate.openalex_author_id}"
    hit = cache.get(key)
    if hit is not None:
        return hit
    from . import openalex
    from .author_linking import USER_AGENT, WORKS_URL

    params = {"filter": f"author.id:{candidate.openalex_author_id}", "sort": "publication_date:desc",
              "per-page": str(limit), "select": "id,doi,display_name,publication_year,primary_location"}
    body = openalex.get_json(f"{WORKS_URL}?{urllib.parse.urlencode(params)}", user_agent=USER_AGENT)
    out = []
    for w in body.get("results") or []:
        src = ((w.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
        out.append({"title": (w.get("display_name") or "")[:300], "year": w.get("publication_year"),
                    "journal": src[:200], "doi": (w.get("doi") or "").replace("https://doi.org/", "")})
    cache.set(key, out, WORKS_CACHE_SECONDS)
    return out


# --------------------------------------------------------------------------- decisions
def decide(actor, researcher, *, confirm_ids: list[int], reject_ids: list[int]) -> dict:
    """Confirm and/or reject the researcher's waiting records. Only waiting (or earlier-decided) records of this
    researcher can be changed; automatic links are left alone. Returns the researcher's updated state."""
    if not can_decide_for(actor, researcher):
        raise AuthorReviewError("You cannot confirm author records for this researcher.", status=403, code="forbidden")
    confirm_ids, reject_ids = set(map(int, confirm_ids or [])), set(map(int, reject_ids or []))
    if confirm_ids & reject_ids:
        raise AuthorReviewError("A record cannot be both confirmed and rejected.")
    if not (confirm_ids or reject_ids):
        raise AuthorReviewError("Choose at least one record.")
    newly_confirmed = 0
    with transaction.atomic():
        rows = {c.pk: c for c in ResearcherAuthorCandidate.objects.select_for_update()
                .filter(user=researcher, pk__in=confirm_ids | reject_ids)}
        missing = (confirm_ids | reject_ids) - set(rows)
        if missing:
            raise AuthorReviewError("Some records do not belong to this researcher.", status=404, code="not_found")
        now = timezone.now()
        for pk, c in rows.items():
            if c.status == AuthorLinkStatus.AUTO_LINKED:
                continue  # automatic links are not changed here
            new = AuthorLinkStatus.CONFIRMED if pk in confirm_ids else AuthorLinkStatus.REJECTED
            if c.status == new:
                continue
            newly_confirmed += new == AuthorLinkStatus.CONFIRMED
            c.status, c.decided_by, c.decided_at = new, actor, now
            c.save(update_fields=["status", "decided_by", "decided_at", "updated_at"])
        if newly_confirmed:
            transaction.on_commit(lambda: _start_follow_up(researcher.pk))
    out = researcher_out(researcher, actor)
    out["scan_started"] = bool(newly_confirmed)
    return out


def _start_follow_up(user_id: int) -> None:
    from .tasks import impact_author_confirmed_task

    try:
        impact_author_confirmed_task.delay(user_id)
    except Exception:  # broker down: the nightly scan picks the researcher up anyway
        logger.warning("could not queue follow-up for confirmed author user=%s", user_id, exc_info=True)


def follow_up(user_id: int) -> dict:
    """After a confirmation: suggest the linked records' recent papers, then (when publication mapping is on) check
    their acknowledgements and tell the reviewers about probable matches."""
    from django.conf import settings

    from . import author_linking, facility_mapping, match_review

    user = get_user_model().objects.filter(pk=user_id, is_active=True).first()
    if user is None:
        return {"skipped": "user not found"}
    found = author_linking.discover_linked(user)
    if not getattr(settings, "PUBLICATION_MAPPING_ENABLED", False):
        return {"discovery": found, "check": "PUBLICATION_MAPPING_ENABLED is off"}
    summary = facility_mapping.verify_user(user, apply=True, limit=25)
    notified = match_review.notify_reviewers(summary.get("queued_ids", []))
    return {"discovery": found, "queued": summary.get("queued", 0), "reviewers_notified": notified}

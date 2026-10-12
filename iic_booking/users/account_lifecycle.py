"""Programme / employment end date, suspension and automatic wallet link for IITR students and supervised users.

Who is in scope
    IITR Students (``student`` / ``individual_student`` without a post-doc / research associate alias, including
    Channel i accounts), IITR Post Doctoral Fellows and IITR Research Associates in Projects (student types with
    that alias) and IITR Startups (``startup_incubated_iitr``). Faculty, staff and external users are never in scope.

End date
    IITR Students: the Graduation Date when present, otherwise the Joining Date plus the programme duration of
    the degree (Ph.D. 5 years, M.Sc. 2, M.Tech. 2, Int. M.Tech 5, B.Tech. 4, anything else 5). Post-docs,
    research associates and startups: the end date they gave at registration (``User.program_end_date``).
    An approved extension (six months from the later of today and the current end date) wins when later.
    Without any date the user is never suspended; post-docs, research associates and startups are asked to
    give one after sign-in.

Suspension
    Only while ``settings.ACCOUNT_END_DATE_ENFORCEMENT`` is on, a user past the end date is suspended: they can
    sign in, view past bookings and download results, ask their supervisor for an extension, but every other
    write is refused with ``account_suspended`` (``AccountSuspensionMiddleware``). ``is_active`` is never
    changed. Future bookings are never cancelled; they are listed for the user, the supervisor and the OICs.

Automatic wallet link
    When an IITR faculty member approves the registration of a post-doc, research associate or startup, the user
    is linked to that faculty member's wallet at once (an approved ``WalletJoinRequest``); no second approval.

Every reader tolerates the table being absent until the migration runs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable, Optional

from django.conf import settings
from django.db import OperationalError, ProgrammingError, transaction
from django.db.models import Max, Q
from django.utils import timezone

from iic_booking.users.identity.dates import add_calendar_years
from iic_booking.users.models import (
    AccountLifecycle,
    AccountLifecycleEvent as LEvent,
    DegreeCode,
    EndDateSource,
    LifecycleCategory,
    User,
)
from iic_booking.users.models.user_type import UserType

logger = logging.getLogger(__name__)

SCHEMA_ERRORS = (ProgrammingError, OperationalError)
SUSPENDED_CODE = "account_suspended"
POSTDOC_ALIAS = "IITR Post Doctoral Fellows"
RA_ALIAS = "IITR Research Associates in Projects"
LEGACY_STARTUP_ALIASES = ("iitr startups", "iitr startup")
STUDENT_TYPES = (UserType.STUDENT, UserType.INDIVIDUAL_STUDENT)
SCOPE_USER_TYPES = (UserType.STUDENT, UserType.INDIVIDUAL_STUDENT, UserType.STARTUP_INCUBATED_IITR)
SUPERVISED_CATEGORIES = (
    LifecycleCategory.IITR_POSTDOC,
    LifecycleCategory.IITR_RESEARCH_ASSOCIATE,
    LifecycleCategory.IITR_STARTUP,
)
DEGREE_YEARS = {
    DegreeCode.PHD: 5,
    DegreeCode.MSC: 2,
    DegreeCode.MTECH: 2,
    DegreeCode.INT_MTECH: 5,
    DegreeCode.BTECH: 4,
    DegreeCode.OTHER: 5,
}
# Only these account types draw on a faculty wallet (``User.get_accessible_wallet``, ``WalletJoinRequest.clean``).
WALLET_LINK_TYPES = (UserType.STUDENT, UserType.OTHER)
REMINDER_DAYS = (30, 7, 1)
EXTENSION_REQUEST_WINDOW_DAYS = 30
SUPERVISOR_NEARING_DAYS = 60
QUIET_AFTER_DAYS = 30
MAX_USER_END_DATE_YEARS = 5

TPL_REMINDER = "account_end_date_reminder_email"
TPL_SUSPENDED = "account_suspended_email"
TPL_WALLET_LINKED = "wallet_auto_linked_email"

SOURCE_NOTES = {
    EndDateSource.GRADUATION_DATE: "Graduation Date from Channel i",
    EndDateSource.DERIVED_FROM_JOINING: "Joining Date + {years} years ({degree})",
    EndDateSource.USER_PROVIDED: "End date you gave at registration",
    EndDateSource.EXTENDED: "Extended by your supervisor",
    EndDateSource.MISSING: "Not available",
}


def enforcement_enabled() -> bool:
    return bool(getattr(settings, "ACCOUNT_END_DATE_ENFORCEMENT", False))


def _safe(fn, default):
    try:
        with transaction.atomic():
            return fn()
    except SCHEMA_ERRORS:
        logger.warning("account lifecycle tables unavailable", exc_info=True)
        return default


def schema_ready() -> bool:
    return _safe(lambda: AccountLifecycle.objects.exists() or True, False)


# ---------------------------------------------------------------------------
# Category and degree
# ---------------------------------------------------------------------------


def category_for(user) -> str:
    """Lifecycle category of a user, or ``""`` when the end-date rules do not apply."""
    ut = getattr(user, "user_type", None)
    alias = (getattr(user, "user_type_alias", None) or "").strip()
    if ut == UserType.STARTUP_INCUBATED_IITR:
        return LifecycleCategory.IITR_STARTUP
    if ut in STUDENT_TYPES:
        if alias == POSTDOC_ALIAS:
            return LifecycleCategory.IITR_POSTDOC
        if alias == RA_ALIAS:
            return LifecycleCategory.IITR_RESEARCH_ASSOCIATE
        if alias.lower() in LEGACY_STARTUP_ALIASES:
            return LifecycleCategory.IITR_STARTUP
        return LifecycleCategory.IITR_STUDENT
    return ""


def is_supervised_category(user) -> bool:
    return category_for(user) in SUPERVISED_CATEGORIES


def normalise_degree(name: Optional[str]) -> str:
    """Degree code for a Channel i / profile degree name; ``""`` when empty.

    ``Ph.D. - Doctor of Philosophy``, ``PhD`` → phd; ``Int. M.Tech``, ``Integrated M.Tech``, ``IMTech`` → int_mtech;
    ``M.Tech.`` → mtech; ``M.Sc.`` → msc; ``B.Tech.`` → btech. Other integrated / dual degrees (``Int. M.Sc.``,
    ``IDD``, ``B.Tech + M.Tech``) and every other programme → other (5 years, the same as Int. M.Tech).
    """
    raw = str(name or "").strip()
    if not raw:
        return ""
    upper = raw.upper()
    compact = re.sub(r"[^A-Z]", "", upper)
    words = re.findall(r"[A-Z]+", upper)
    if "PHD" in compact or "DOCTOROFPHILOSOPHY" in compact:
        return DegreeCode.PHD
    integrated = (
        "INTEGRATED" in words
        or compact.startswith(("INT", "IMTECH", "IMSC"))
        or "DUAL" in words
        or "IDD" in words
    )
    mtech = compact.startswith("MTECH") or "MASTEROFTECHNOLOGY" in compact
    if integrated and ("MTECH" in compact or "MASTEROFTECHNOLOGY" in compact) and "BTECH" not in compact and "DUAL" not in words and "IDD" not in words:
        return DegreeCode.INT_MTECH
    if integrated:
        return DegreeCode.OTHER
    if mtech:
        return DegreeCode.MTECH
    if compact.startswith("MSC") or "MASTEROFSCIENCE" in compact:
        return DegreeCode.MSC
    if compact.startswith("BTECH") or "BACHELOROFTECHNOLOGY" in compact:
        return DegreeCode.BTECH
    return DegreeCode.OTHER


def degree_label(code: str) -> str:
    return str(dict(DegreeCode.choices).get(code, "")) if code else ""


def category_label(category: str) -> str:
    return str(dict(LifecycleCategory.choices).get(category, "")) if category else ""


# ---------------------------------------------------------------------------
# End date
# ---------------------------------------------------------------------------


@dataclass
class EndDate:
    category: str
    degree_name: str
    degree_code: str
    joining_date: Optional[date]
    base_end_date: Optional[date]
    extended_until: Optional[date]
    programme_end_date: Optional[date]
    source: str


def _profile(user):
    from iic_booking.users.identity.service import UserIdentityService

    try:
        return UserIdentityService.get_profile(user)
    except Exception:
        return None


def _approved_extension_until(user) -> Optional[date]:
    from iic_booking.users.models import RegistrationExtensionRequest, RegistrationExtensionStatus

    return _safe(
        lambda: RegistrationExtensionRequest.objects.filter(
            user=user, status=RegistrationExtensionStatus.APPROVED, approved_until__isnull=False
        ).aggregate(m=Max("approved_until"))["m"],
        None,
    )


def compute(user, *, profile=None, extended_until: Optional[date] = None, use_db: bool = True) -> Optional[EndDate]:
    """End date of one user from the profile and approved extensions. ``None`` when out of scope."""
    category = category_for(user)
    if not category:
        return None
    if use_db and profile is None:
        profile = _profile(user)
    if use_db and extended_until is None:
        extended_until = _approved_extension_until(user)
    degree_name = (getattr(user, "degree_name", "") or "").strip() or (
        (getattr(profile, "student_degree_name", "") or "").strip() if profile else ""
    )
    joining = getattr(user, "joining_date", None) or (getattr(profile, "student_start_date", None) if profile else None)
    code = ""
    if category == LifecycleCategory.IITR_STUDENT:
        joining = joining or getattr(user, "program_start_date", None)
        graduation = getattr(user, "graduation_date", None) or (getattr(profile, "student_end_date", None) if profile else None)
        code = normalise_degree(degree_name) or (DegreeCode.OTHER if joining else "")
        if graduation:
            base, source = graduation, EndDateSource.GRADUATION_DATE
        elif joining:
            base, source = add_calendar_years(joining, DEGREE_YEARS[code]), EndDateSource.DERIVED_FROM_JOINING
        elif getattr(user, "program_end_date", None):
            base, source = user.program_end_date, EndDateSource.USER_PROVIDED
        else:
            base, source = None, EndDateSource.MISSING
    else:
        joining = getattr(user, "program_start_date", None) or joining
        base = getattr(user, "program_end_date", None)
        source = EndDateSource.USER_PROVIDED if base else EndDateSource.MISSING
    effective = base
    if extended_until and (base is None or extended_until >= base):
        effective, source = extended_until, EndDateSource.EXTENDED
    return EndDate(
        category=category,
        degree_name=degree_name[:255],
        degree_code=code,
        joining_date=joining,
        base_end_date=base,
        extended_until=extended_until,
        programme_end_date=effective,
        source=source,
    )


_FIELDS = ("category", "degree_name", "degree_code", "joining_date", "base_end_date", "extended_until", "programme_end_date")


def refresh(user, *, end: Optional[EndDate] = None) -> Optional[AccountLifecycle]:
    """Recompute and store the row; removes it when the user left the scope. Never raises on a missing table."""
    if not getattr(user, "pk", None):
        return None
    end = end if end is not None else compute(user)

    def work():
        row = AccountLifecycle.objects.filter(user_id=user.pk).first()
        if end is None:
            if row is not None:
                row.delete()
            return None
        values = {
            "category": end.category,
            "degree_name": end.degree_name,
            "degree_code": end.degree_code,
            "joining_date": end.joining_date,
            "base_end_date": end.base_end_date,
            "extended_until": end.extended_until,
            "programme_end_date": end.programme_end_date,
            "end_date_source": end.source,
        }
        if row is None:
            return AccountLifecycle.objects.create(user_id=user.pk, computed_at=timezone.now(), **values)
        changed = [k for k, v in values.items() if getattr(row, k) != v]
        if changed:
            for k in changed:
                setattr(row, k, values[k])
            row.computed_at = timezone.now()
            row.save(update_fields=changed + ["computed_at", "updated_at"])
        return row

    return _safe(work, None)


def lifecycle_for(user, *, create: bool = True) -> Optional[AccountLifecycle]:
    """Stored row (cached on the user object for the request); computed and saved on first use."""
    if not getattr(user, "pk", None) or not category_for(user):
        return None
    cached = getattr(user, "_account_lifecycle_cache", None)
    if cached is not None:
        return cached or None
    row = _safe(lambda: AccountLifecycle.objects.filter(user_id=user.pk).first(), None)
    if row is None and create:
        row = refresh(user)
    try:
        user._account_lifecycle_cache = row or False
    except Exception:
        pass
    return row


def forget_cached(user) -> None:
    if hasattr(user, "_account_lifecycle_cache"):
        try:
            del user._account_lifecycle_cache
        except Exception:
            pass


def current_end_date(user) -> Optional[date]:
    row = lifecycle_for(user)
    if row is not None:
        return row.programme_end_date
    return getattr(user, "program_end_date", None)


def source_note(row: Optional[AccountLifecycle]) -> str:
    if row is None:
        return SOURCE_NOTES[EndDateSource.MISSING]
    note = SOURCE_NOTES.get(row.end_date_source, "")
    if row.end_date_source == EndDateSource.DERIVED_FROM_JOINING:
        code = row.degree_code or DegreeCode.OTHER
        note = note.format(years=DEGREE_YEARS.get(code, 5), degree=degree_label(code) or "other programme")
    return note


# ---------------------------------------------------------------------------
# Suspension
# ---------------------------------------------------------------------------


def _past_end(row: Optional[AccountLifecycle], today: Optional[date] = None) -> bool:
    today = today or timezone.localdate()
    return bool(row is not None and row.programme_end_date and today > row.programme_end_date)


def is_suspended(user, today: Optional[date] = None) -> bool:
    """True when enforcement is on and the user's programme / employment end date has passed."""
    if not enforcement_enabled() or not getattr(user, "is_authenticated", False):
        return False
    if not category_for(user):
        return False
    return _past_end(lifecycle_for(user), today)


def suspension_message(user) -> str:
    end = current_end_date(user)
    when = end.strftime("%d %b %Y") if end else "the recorded date"
    return (
        f"Your programme / employment end date ({when}) has passed, so your account is suspended. You can view your "
        "past bookings and download results, but you cannot book, upload or make other changes until your "
        "supervisor (IITR Faculty) extends it. Use 'Request extension' to ask them."
    )


def suspension_payload(user) -> dict[str, Any]:
    end = current_end_date(user)
    return {
        "error": suspension_message(user),
        "detail": suspension_message(user),
        "code": SUSPENDED_CODE,
        "programme_end_date": end.isoformat() if end else None,
    }


def suspension_refusal(booking_user, *, on_behalf: bool = False) -> Optional[dict[str, Any]]:
    """Payload refusing a booking for a suspended user (also when staff book on their behalf)."""
    if not enforcement_enabled() or booking_user is None or not category_for(booking_user):
        return None
    if not _past_end(lifecycle_for(booking_user)):
        return None
    payload = suspension_payload(booking_user)
    if on_behalf:
        payload["error"] = payload["detail"] = (
            "This user's account is suspended: their programme / employment end date has passed. "
            "Their supervisor (IITR Faculty) can extend it."
        )
    return payload


def login_allowed_after_end(user) -> bool:
    """With enforcement on, users in scope sign in after their end date (suspended, read-only)."""
    return enforcement_enabled() and bool(category_for(user))


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


def _eligible_faculty(user) -> bool:
    from iic_booking.users.registration_approvals import is_eligible_faculty

    return user is not None and getattr(user, "is_active", False) and is_eligible_faculty(user)


def supervisor_for(user):
    """IITR faculty who decides extensions: the profile supervisor of post-docs / RAs / startups, else the owner
    of the wallet the user is linked to, else the faculty member who approved the registration."""
    from iic_booking.users.models import RegistrationApproval, RegistrationApprovalStatus
    from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus

    if getattr(user, "supervisor_id", None):
        sup = user.supervisor
        if _eligible_faculty(sup):
            return sup
    link = (
        WalletJoinRequest.objects.filter(student=user, status=WalletJoinRequestStatus.APPROVED, faculty__user_type=UserType.FACULTY)
        .select_related("faculty__department")
        .order_by("-updated_at", "-pk")
        .first()
    )
    if link is not None and _eligible_faculty(link.faculty):
        return link.faculty
    approval = _safe(
        lambda: RegistrationApproval.objects.filter(user=user, status=RegistrationApprovalStatus.APPROVED, faculty__isnull=False)
        .select_related("faculty__department")
        .first(),
        None,
    )
    if approval is not None and _eligible_faculty(approval.faculty):
        return approval.faculty
    return None


def supervised_user_ids(faculty) -> set[int]:
    from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus

    ids = set(User.objects.filter(supervisor=faculty).values_list("pk", flat=True))
    ids |= set(
        WalletJoinRequest.objects.filter(faculty=faculty, status=WalletJoinRequestStatus.APPROVED).values_list("student_id", flat=True)
    )
    return ids


# ---------------------------------------------------------------------------
# Extensions
# ---------------------------------------------------------------------------


def can_request_extension(user, today: Optional[date] = None) -> tuple[bool, str]:
    """(allowed, reason code). Post-docs / RAs / startups: any time once approved (unchanged rule).
    IITR Students: only with enforcement on, from 30 days before the end date and while suspended."""
    from iic_booking.users import registration_approvals as ra

    today = today or timezone.localdate()
    category = category_for(user)
    if not category:
        return False, "not_in_scope"
    end = current_end_date(user)
    if not end:
        return False, "no_end_date"
    if category in SUPERVISED_CATEGORIES:
        return (True, "ok") if ra.fully_approved(user) else (False, "not_approved")
    if not enforcement_enabled():
        return False, "enforcement_off"
    if (end - today).days > EXTENSION_REQUEST_WINDOW_DAYS:
        return False, "too_early"
    return True, "ok"


def extension_max_until(user, today: Optional[date] = None) -> date:
    from iic_booking.users.registration_approvals import EXTENSION_MAX_MONTHS
    from iic_booking.users.identity.dates import add_calendar_months

    today = today or timezone.localdate()
    end = current_end_date(user)
    base = end if end and end >= today else today
    return add_calendar_months(base, EXTENSION_MAX_MONTHS)


def on_extension_granted(user, until: date, *, actor=None, role: str = "", channel: str = "") -> bool:
    """Store the new end date and lift the suspension. Returns True when the user was suspended before."""
    forget_cached(user)
    before = _safe(lambda: AccountLifecycle.objects.filter(user_id=user.pk).first(), None)
    was_suspended = _past_end(before) and enforcement_enabled()
    row = refresh(user)
    if row is not None and (row.suspended_at or row.suspended_for_end_date):
        _safe(
            lambda: AccountLifecycle.objects.filter(pk=row.pk).update(suspended_at=None, suspended_for_end_date=None),
            None,
        )
    event(
        LEvent.Action.EXTENDED,
        user=user,
        actor=actor,
        role=role,
        channel=channel,
        until=until.isoformat() if until else None,
        previous_end_date=before.programme_end_date.isoformat() if before and before.programme_end_date else None,
    )
    if was_suspended:
        event(LEvent.Action.RESUMED, user=user, actor=actor, role=role, channel=channel)
    forget_cached(user)
    return was_suspended


def provide_end_date(user, value, *, request=None) -> AccountLifecycle:
    """Post-docs, RAs and startups without an end date give one (only while it is missing)."""
    from iic_booking.users.registration_approvals import ApprovalError, _parse_until

    if not is_supervised_category(user):
        raise ApprovalError("Only IITR post-docs, research associates and startups give their own end date.", "not_eligible")
    if getattr(user, "program_end_date", None):
        raise ApprovalError("Your end date is already recorded. Ask your supervisor for an extension instead.", "already_set")
    until = _parse_until(value)
    today = timezone.localdate()
    if until is None:
        raise ApprovalError("Enter a valid date.", "date_invalid")
    if until <= today:
        raise ApprovalError("The end date must be after today.", "date_past")
    if until > add_calendar_years(today, MAX_USER_END_DATE_YEARS):
        raise ApprovalError(f"The end date can be at most {MAX_USER_END_DATE_YEARS} years from today.", "date_too_far")
    user.program_end_date = until
    user.save(update_fields=["program_end_date"])
    forget_cached(user)
    row = refresh(user)
    event(LEvent.Action.END_DATE_PROVIDED, user=user, actor=user, role="user", channel="portal", end_date=until.isoformat())
    sup = supervisor_for(user)
    if sup is not None:
        _in_app(
            [sup],
            title="End date recorded",
            message=f"{_name(user)} recorded their programme / employment end date as {_fmt(until)}.",
            link="/registration-approvals",
            event_name="account_lifecycle.end_date_provided",
        )
    return row


# ---------------------------------------------------------------------------
# Audit, email, notices
# ---------------------------------------------------------------------------


def event(action: str, *, user=None, actor=None, role: str = "", channel: str = "", **details) -> None:
    def work():
        LEvent.objects.create(
            user=user if getattr(user, "pk", None) else None,
            action=action,
            actor=actor if getattr(actor, "pk", None) else None,
            actor_role=role or ("system" if actor is None else ""),
            channel=channel or ("system" if actor is None else "portal"),
            details={k: v for k, v in details.items() if v is not None},
        )

    try:
        _safe(work, None)
    except Exception:
        logger.exception("account lifecycle event failed action=%s", action)


def _name(user) -> str:
    from iic_booking.users.display import get_user_display_name

    return get_user_display_name(user) if user else ""


def _fmt(value: Optional[date]) -> str:
    return value.strftime("%d %b %Y") if value else ""


def _link(path: str) -> str:
    from iic_booking.communication.utils import get_frontend_absolute_url

    return get_frontend_absolute_url(path)


def _in_app(recipients, *, title: str, message: str, link: str, event_name: str, created_by=None) -> None:
    try:
        from iic_booking.communication.in_app import notify_in_app

        notify_in_app(recipients, title=title, message=message, link=link, event=event_name, created_by=created_by)
    except Exception:
        logger.exception("account lifecycle in-app notice failed event=%s", event_name)


def _send(code: str, recipient, context: dict[str, Any], *, cc: Iterable = (), created_by=None) -> bool:
    from iic_booking.communication.models import CommunicationLog
    from iic_booking.communication.service import CommunicationService
    from iic_booking.users.supervisor_invites import _ensure_template

    if recipient is None or not (getattr(recipient, "email", "") or "").strip():
        return False
    own = recipient.email.strip().lower()
    cc_emails = sorted(
        {
            (getattr(u, "email", "") or "").strip()
            for u in cc
            if u is not None and (getattr(u, "email", "") or "").strip() and (getattr(u, "email", "") or "").strip().lower() != own
        }
    )
    try:
        template = _ensure_template(code)
        if template is None:
            raise ValueError(f"template {code} is missing or inactive")
        log = CommunicationService.send_email(
            recipient=recipient,
            template=template,
            template_context=context,
            metadata={"module": "account_lifecycle", "template": code},
            created_by=created_by if getattr(created_by, "pk", None) else None,
            cc_emails=cc_emails or None,
        )
        return bool(log) and getattr(log, "status", "") != CommunicationLog.CommunicationStatus.FAILED
    except Exception:
        logger.exception("account lifecycle email failed template=%s", code)
        return False


def _context(user, row: Optional[AccountLifecycle], supervisor=None) -> dict[str, Any]:
    return {
        "user_name": _name(user),
        "user_email": user.email,
        "user_type": category_label(category_for(user)),
        "degree_name": (row.degree_name if row else "") or "",
        "programme_end_date": _fmt(row.programme_end_date if row else None),
        "end_date_source": source_note(row),
        "faculty_name": _name(supervisor) if supervisor else "",
        "link": _extension_link(user),
    }


def _extension_link(user) -> str:
    from iic_booking.users.registration_approvals import user_extension_link

    try:
        return user_extension_link(user)
    except Exception:
        return _link("/programme-extension")


# ---------------------------------------------------------------------------
# Automatic wallet link
# ---------------------------------------------------------------------------


def auto_link_wallet(user, faculty, *, actor=None, channel: str = "system", notify: bool = True) -> tuple[Optional[Any], str]:
    """Link a post-doc / RA / startup to the approving faculty member's wallet. Returns (link, outcome).

    Outcomes: created, approved_pending, already_linked, linked_elsewhere, own_wallet, not_in_scope, no_faculty.
    A user already linked to another wallet is left alone (that link decides who pays for student bookings).
    IITR Startup accounts (``startup_incubated_iitr``) book from their own wallet by design and are not linked.
    """
    from iic_booking.users.models import Wallet
    from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus

    if not is_supervised_category(user):
        return None, "not_in_scope"
    if faculty is None or not _eligible_faculty(faculty) or faculty.pk == user.pk:
        return None, "no_faculty"
    if user.user_type not in WALLET_LINK_TYPES:
        event(LEvent.Action.WALLET_AUTO_LINK_SKIPPED, user=user, actor=actor, channel=channel, reason="own_wallet", faculty_id=faculty.pk)
        return None, "own_wallet"
    now = timezone.now()
    message = f"Linked automatically when {_name(faculty)} approved the registration."
    with transaction.atomic():
        links = WalletJoinRequest.objects.select_for_update().filter(student=user)
        mine = links.filter(faculty=faculty, status=WalletJoinRequestStatus.APPROVED).first()
        if mine is not None:
            return mine, "already_linked"
        if links.filter(status=WalletJoinRequestStatus.APPROVED).exclude(faculty=faculty).exists():
            event(LEvent.Action.WALLET_AUTO_LINK_SKIPPED, user=user, actor=actor, channel=channel, reason="linked_elsewhere", faculty_id=faculty.pk)
            return None, "linked_elsewhere"
        wallet, _ = Wallet.objects.get_or_create(user=faculty)
        pending = links.filter(faculty=faculty, status=WalletJoinRequestStatus.PENDING).order_by("-pk").first()
        if pending is not None:
            pending.wallet = pending.wallet or wallet
            pending.status = WalletJoinRequestStatus.APPROVED
            pending.faculty_response = message
            pending.responded_at = now
            pending.save()
            link, outcome = pending, "approved_pending"
        else:
            link = WalletJoinRequest.objects.create(
                student=user,
                faculty=faculty,
                wallet=wallet,
                status=WalletJoinRequestStatus.APPROVED,
                message="Automatic link after registration approval.",
                faculty_response=message,
                responded_at=now,
            )
            outcome = "created"
        _activate_affiliation(user, faculty, link)
    event(
        LEvent.Action.WALLET_AUTO_LINKED,
        user=user,
        actor=actor,
        role="faculty" if getattr(actor, "pk", None) == faculty.pk else "",
        channel=channel,
        faculty_id=faculty.pk,
        join_request_id=link.pk,
        outcome=outcome,
    )
    if notify:
        _notify_linked(user, faculty, actor=actor)
    return link, outcome


def _activate_affiliation(user, faculty, link) -> None:
    try:
        from iic_booking.users.identity.service import UserIdentityService
        from iic_booking.users.models.channel_i_identity import AffiliationKind, UserAffiliation

        with transaction.atomic():
            updated = UserAffiliation.objects.filter(wallet_join_request=link).update(active=True)
            if not updated:
                view = UserIdentityService.view(user)
                UserAffiliation.objects.create(
                    user=user,
                    kind=AffiliationKind.FACULTY,
                    related_user=faculty,
                    department_id=view.internal_department_id,
                    wallet_join_request=link,
                    active=True,
                )
    except Exception:
        logger.debug("affiliation for automatic wallet link skipped user=%s", user.pk, exc_info=True)


def _notify_linked(user, faculty, *, actor=None) -> None:
    context = {
        "user_name": _name(user),
        "user_email": user.email,
        "user_type": category_label(category_for(user)),
        "faculty_name": _name(faculty),
        "faculty_email": faculty.email,
        "wallet_note": (
            f"Your bookings are charged to {_name(faculty)}'s wallet from now on. {_name(faculty)} can set a "
            "monthly spending limit or remove the link from their Wallet page."
        ),
        "link": _link("/wallet"),
    }
    _send(TPL_WALLET_LINKED, user, context, cc=[faculty], created_by=actor)
    _in_app(
        [user],
        title="Linked to your supervisor's wallet",
        message=f"You are now linked to {_name(faculty)}'s wallet. No separate wallet link request is needed.",
        link="/wallet",
        event_name="account_lifecycle.wallet_auto_linked",
        created_by=actor,
    )
    _in_app(
        [faculty],
        title="User linked to your wallet",
        message=f"{_name(user)} is now linked to your wallet because you approved their registration.",
        link="/wallet#wallet-join-requests",
        event_name="account_lifecycle.wallet_auto_linked",
        created_by=actor,
    )


def _faculty_approved(user) -> Optional[Any]:
    """The faculty member who approved this account, or None (admin-only approvals do not count)."""
    from iic_booking.users.models import RegistrationApproval, RegistrationApprovalStatus

    approval = _safe(lambda: RegistrationApproval.objects.filter(user=user).select_related("faculty__department").first(), None)
    if approval is not None and approval.status == RegistrationApprovalStatus.APPROVED and approval.decided_role == "faculty":
        return approval.faculty
    if getattr(user, "supervisor_approved", False) and user.supervisor_id:
        return user.supervisor
    return None


def backfill_links(*, apply: bool = False) -> dict[str, Any]:
    """Approved post-docs / RAs / startups whose faculty approved them but who have no wallet link yet."""
    from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus

    qs = (
        User.objects.filter(user_type__in=SCOPE_USER_TYPES, admin_approved=True, is_active=True)
        .select_related("supervisor__department")
        .order_by("pk")
    )
    counts: dict[str, dict[str, int]] = {}
    linked: list[dict[str, Any]] = []
    for user in qs:
        category = category_for(user)
        if category not in SUPERVISED_CATEGORIES:
            continue
        bucket = counts.setdefault(category, {})
        test = bool(getattr(user, "is_test_account", False))
        faculty = _faculty_approved(user)
        approved_links = WalletJoinRequest.objects.filter(student=user, status=WalletJoinRequestStatus.APPROVED)
        if faculty is None:
            outcome = "not_faculty_approved"
        elif not _eligible_faculty(faculty):
            outcome = "faculty_not_eligible"
        elif user.user_type not in WALLET_LINK_TYPES:
            outcome = "own_wallet"
        elif approved_links.filter(faculty=faculty).exists():
            outcome = "already_linked"
        elif approved_links.exists():
            outcome = "linked_elsewhere"
        elif test:
            outcome = "test_account_skipped"
        elif apply:
            _link_row, outcome = auto_link_wallet(user, faculty, channel="system", notify=True)
            if outcome in ("created", "approved_pending"):
                linked.append({"user_id": user.pk, "faculty_id": faculty.pk, "outcome": outcome, "category": category})
        else:
            pending = WalletJoinRequest.objects.filter(student=user, faculty=faculty, status=WalletJoinRequestStatus.PENDING).exists()
            outcome = "would_approve_pending" if pending else "would_create"
        bucket[outcome] = bucket.get(outcome, 0) + 1
    return {"applied": apply, "by_category": counts, "linked": linked, "linked_count": len(linked)}


# ---------------------------------------------------------------------------
# Daily run: recompute, reminders, suspension notices
# ---------------------------------------------------------------------------


def scope_queryset():
    return User.objects.filter(user_type__in=SCOPE_USER_TYPES).select_related("supervisor__department")


def _batch_extensions(user_ids: list[int]) -> dict[int, date]:
    from iic_booking.users.models import RegistrationExtensionRequest, RegistrationExtensionStatus

    return dict(
        _safe(
            lambda: list(
                RegistrationExtensionRequest.objects.filter(
                    user_id__in=user_ids, status=RegistrationExtensionStatus.APPROVED, approved_until__isnull=False
                )
                .values("user_id")
                .annotate(m=Max("approved_until"))
                .values_list("user_id", "m")
            ),
            [],
        )
    )


def _iter_computed(qs, chunk: int = 500):
    """Yield (user, EndDate) for every user in scope, batching profile and extension lookups."""
    from iic_booking.users.models.channel_i_identity import ChannelIIdentityProfile

    ids = list(qs.values_list("pk", flat=True))
    for start in range(0, len(ids), chunk):
        part = ids[start : start + chunk]
        users = list(scope_queryset().filter(pk__in=part))
        profiles = _safe(lambda: {p.user_id: p for p in ChannelIIdentityProfile.objects.filter(user_id__in=part)}, {})
        extensions = _batch_extensions(part)
        for user in users:
            end = compute(user, profile=profiles.get(user.pk), extended_until=extensions.get(user.pk), use_db=False)
            if end is not None:
                yield user, end


def recompute_all(*, apply: bool = True) -> dict[str, int]:
    created = updated = unchanged = 0
    existing = _safe(lambda: dict(AccountLifecycle.objects.values_list("user_id", "programme_end_date")), {})
    for user, end in _iter_computed(scope_queryset()):
        if not apply:
            if user.pk not in existing:
                created += 1
            elif existing[user.pk] != end.programme_end_date:
                updated += 1
            else:
                unchanged += 1
            continue
        had = user.pk in existing
        before = existing.get(user.pk)
        refresh(user, end=end)
        if not had:
            created += 1
        elif before != end.programme_end_date:
            updated += 1
        else:
            unchanged += 1
    return {"created": created, "updated": updated, "unchanged": unchanged, "applied": apply}


def _active_scope(qs):
    return qs.filter(is_active=True, admin_approved=True)


def _remind(user, row: AccountLifecycle, days: int) -> None:
    sup = supervisor_for(user)
    context = {**_context(user, row, sup), "days_left": str(days)}
    sent = _send(TPL_REMINDER, user, context, cc=[sup] if sup else [])
    marks = set((row.reminders_sent or {}).get(row.programme_end_date.isoformat(), []))
    marks.update(d for d in REMINDER_DAYS if d >= days)
    row.reminders_sent = {row.programme_end_date.isoformat(): sorted(marks, reverse=True)}
    row.save(update_fields=["reminders_sent", "updated_at"])
    event(LEvent.Action.REMINDER_SENT, user=user, days=days, end_date=row.programme_end_date.isoformat(), email_sent=sent)


def _suspend(user, row: AccountLifecycle, now, today: date) -> None:
    """Record the suspension and tell the user, the supervisor and the OICs of any future booking.

    Accounts whose end date passed more than ``QUIET_AFTER_DAYS`` before the run (e.g. long-graduated students on
    the day enforcement is switched on) get the in-portal notice only, no email."""
    from iic_booking.users.registration_approvals import future_bookings, serialize_booking

    sup = supervisor_for(user)
    bookings = list(future_bookings(user, now)[:50])
    rows = [serialize_booking(b) for b in bookings]
    row.suspended_at = now
    row.suspended_for_end_date = row.programme_end_date
    row.save(update_fields=["suspended_at", "suspended_for_end_date", "updated_at"])
    ids = ", ".join(r["display_id"] for r in rows)
    context = {
        **_context(user, row, sup),
        "future_bookings": ids,
        "future_bookings_note": (
            f"Your upcoming booking(s) {ids} have not been cancelled. The lab's Officer In Charge has been told; "
            "they may contact you." if ids else ""
        ),
    }
    quiet = (today - row.programme_end_date).days > QUIET_AFTER_DAYS
    sent = False if quiet else _send(TPL_SUSPENDED, user, context, cc=[sup] if sup else [])
    _in_app(
        [user],
        title="Account suspended: end date passed",
        message=suspension_message(user),
        link="/programme-extension",
        event_name="account_lifecycle.suspended",
    )
    if sup is not None:
        _in_app(
            [sup],
            title="Supervised user suspended",
            message=f"{_name(user)}'s programme / employment end date ({_fmt(row.programme_end_date)}) has passed. "
            "You can extend it by six months under Pending approvals once they ask.",
            link="/registration-approvals",
            event_name="account_lifecycle.suspended",
        )
    _flag_bookings_for_oics(user, bookings)
    event(
        LEvent.Action.SUSPENDED,
        user=user,
        end_date=row.programme_end_date.isoformat(),
        future_bookings_kept=[r["display_id"] for r in rows],
        supervisor_id=getattr(sup, "pk", None),
        email_sent=sent,
        quiet=quiet or None,
    )


def _flag_bookings_for_oics(user, bookings) -> None:
    from iic_booking.communication.in_app import equipment_oic_users
    from iic_booking.communication.utils import booking_display_id_for_email

    for b in bookings:
        try:
            oics = equipment_oic_users(b.equipment)
        except Exception:
            oics = []
        if not oics:
            continue
        _in_app(
            oics,
            title="Booking by a suspended user",
            message=(
                f"Booking {booking_display_id_for_email(b) or b.booking_id} on {getattr(b.equipment, 'name', '')} belongs to "
                f"{_name(user)}, whose account was suspended at the end of their programme / employment. The booking "
                "was not cancelled; please decide whether to keep it."
            ),
            link=f"/booking-management?expand={b.pk}",
            event_name="account_lifecycle.future_booking_flagged",
        )


def run_daily(today: Optional[date] = None) -> dict[str, Any]:
    """Idempotent: recompute every row; with enforcement on, send due reminders and record new suspensions."""
    if not schema_ready():
        return {"ran": False, "reason": "schema_pending"}
    today = today or timezone.localdate()
    recomputed = recompute_all(apply=True)
    if not enforcement_enabled():
        return {"ran": True, "enforcement": False, "recomputed": recomputed}
    now = timezone.now()
    reminded = suspended = 0
    rows = (
        AccountLifecycle.objects.filter(programme_end_date__isnull=False, user__is_active=True, user__admin_approved=True)
        .filter(programme_end_date__lte=today + timedelta(days=max(REMINDER_DAYS)))
        .select_related("user__supervisor__department")
    )
    for row in rows:
        user = row.user
        left = (row.programme_end_date - today).days
        try:
            with transaction.atomic():
                if left < 0:
                    if row.suspended_for_end_date != row.programme_end_date:
                        _suspend(user, row, now, today)
                        suspended += 1
                    continue
                due = [d for d in REMINDER_DAYS if left <= d]
                if not due:
                    continue
                sent = set((row.reminders_sent or {}).get(row.programme_end_date.isoformat(), []))
                if min(due) not in sent:
                    _remind(user, row, min(due))
                    reminded += 1
        except Exception:
            logger.exception("account lifecycle daily step failed user=%s", user.pk)
    return {"ran": True, "enforcement": True, "recomputed": recomputed, "reminded": reminded, "suspended": suspended}


# ---------------------------------------------------------------------------
# Read-only report (production dry run)
# ---------------------------------------------------------------------------


def report(today: Optional[date] = None, *, top_degrees: int = 60) -> dict[str, Any]:
    """Counts only (no names / emails): who would be suspended now, who is missing data. Writes nothing."""
    from iic_booking.users.registration_approvals import future_bookings

    today = today or timezone.localdate()
    now = timezone.now()
    soon = today + timedelta(days=EXTENSION_REQUEST_WINDOW_DAYS)
    by_category: dict[str, dict[str, int]] = {}
    by_degree: dict[str, dict[str, int]] = {}
    degree_names: dict[str, dict[str, Any]] = {}
    tests = {"in_scope": 0, "would_suspend_now": 0}
    expired_users: list = []
    for user, end in _iter_computed(_active_scope(scope_queryset())):
        if getattr(user, "is_test_account", False):
            tests["in_scope"] += 1
            if end.programme_end_date and today > end.programme_end_date:
                tests["would_suspend_now"] += 1
            continue
        c = by_category.setdefault(end.category, {"active_users": 0, "would_suspend_now": 0, "ending_within_30_days": 0, "missing_end_date": 0})
        c["active_users"] += 1
        c[f"source_{end.source}"] = c.get(f"source_{end.source}", 0) + 1
        expired = bool(end.programme_end_date and today > end.programme_end_date)
        if end.programme_end_date is None:
            c["missing_end_date"] += 1
        elif expired:
            c["would_suspend_now"] += 1
            if (today - end.programme_end_date).days > QUIET_AFTER_DAYS:
                c["would_suspend_now_ended_over_30_days_ago"] = c.get("would_suspend_now_ended_over_30_days_ago", 0) + 1
            expired_users.append(user)
        elif end.programme_end_date <= soon:
            c["ending_within_30_days"] += 1
        if end.category == LifecycleCategory.IITR_STUDENT:
            key = end.degree_code or "no_degree_no_joining_date"
            d = by_degree.setdefault(key, {"active_users": 0, "would_suspend_now": 0, "missing_end_date": 0, "from_graduation_date": 0, "derived_from_joining": 0})
            d["active_users"] += 1
            d["would_suspend_now"] += int(expired)
            d["missing_end_date"] += int(end.programme_end_date is None)
            d["from_graduation_date"] += int(end.base_end_date is not None and end.source == EndDateSource.GRADUATION_DATE)
            d["derived_from_joining"] += int(end.source == EndDateSource.DERIVED_FROM_JOINING)
            name = end.degree_name or "(empty)"
            entry = degree_names.setdefault(name, {"degree_name": name, "maps_to": end.degree_code or "", "users": 0})
            entry["users"] += 1
    with_future = 0
    no_supervisor = 0
    for user in expired_users:
        if future_bookings(user, now).exists():
            with_future += 1
        if supervisor_for(user) is None:
            no_supervisor += 1
    names = sorted(degree_names.values(), key=lambda e: -e["users"])
    return {
        "today": today.isoformat(),
        "enforcement_enabled": enforcement_enabled(),
        "schema_ready": schema_ready(),
        "by_category": by_category,
        "iitr_students_by_degree": by_degree,
        "would_suspend_now_total": sum(c["would_suspend_now"] for c in by_category.values()),
        "would_suspend_now_with_future_bookings": with_future,
        "would_suspend_now_without_resolvable_supervisor": no_supervisor,
        "missing_end_date_total": sum(c["missing_end_date"] for c in by_category.values()),
        "test_accounts": tests,
        "degree_names_top": names[:top_degrees],
        "degree_names_distinct": len(names),
    }


# ---------------------------------------------------------------------------
# API payloads
# ---------------------------------------------------------------------------


def _person(user) -> Optional[dict[str, Any]]:
    if user is None:
        return None
    dept = getattr(user, "department", None)
    return {"id": user.pk, "name": _name(user), "email": user.email, "department": getattr(dept, "name", "") if dept else ""}


def status_payload(user, *, full: bool = True) -> Optional[dict[str, Any]]:
    """What the profile, the banner and the extension page show. ``None`` for users out of scope."""
    category = category_for(user)
    if not category:
        return None
    row = lifecycle_for(user)
    today = timezone.localdate()
    end = row.programme_end_date if row else getattr(user, "program_end_date", None)
    suspended = enforcement_enabled() and _past_end(row, today)
    days_left = (end - today).days if end else None
    if suspended:
        state = "suspended"
    elif end is None:
        state = "end_date_missing"
    elif days_left is not None and days_left < 0:
        state = "ended"
    elif days_left is not None and days_left <= EXTENSION_REQUEST_WINDOW_DAYS:
        state = "ending_soon"
    else:
        state = "active"
    data: dict[str, Any] = {
        "category": category,
        "category_label": category_label(category),
        "programme_end_date": end.isoformat() if end else None,
        "end_date_source": row.end_date_source if row else EndDateSource.MISSING,
        "state": state,
        "suspended": suspended,
        "enforcement_enabled": enforcement_enabled(),
        "days_left": days_left,
        "end_date_required": end is None and category in SUPERVISED_CATEGORIES,
    }
    if not full:
        return data
    from iic_booking.users import registration_approvals as ra

    pending = ra.pending_extension(user)
    allowed, reason = can_request_extension(user, today)
    sup = supervisor_for(user)
    data.update(
        {
            "degree_name": (row.degree_name if row else "") or (getattr(user, "degree_name", "") or ""),
            "degree_code": row.degree_code if row else "",
            "degree_label": degree_label(row.degree_code) if row else "",
            "degree_years": DEGREE_YEARS.get(row.degree_code) if row and row.degree_code else None,
            "joining_date": row.joining_date.isoformat() if row and row.joining_date else None,
            "graduation_date": user.graduation_date.isoformat() if getattr(user, "graduation_date", None) else None,
            "base_end_date": row.base_end_date.isoformat() if row and row.base_end_date else None,
            "extended_until": row.extended_until.isoformat() if row and row.extended_until else None,
            "end_date_source_note": source_note(row),
            "supervisor": _person(sup),
            "can_request_extension": allowed and pending is None,
            "extension_block_reason": "" if allowed else reason,
            "extension_max_until": extension_max_until(user, today).isoformat() if end else None,
            "extension_max_months": ra.EXTENSION_MAX_MONTHS,
            "pending_extension": ra.serialize_extension(pending) if pending else None,
            "request_window_days": EXTENSION_REQUEST_WINDOW_DAYS,
        }
    )
    return data


def supervisor_overview(faculty, today: Optional[date] = None) -> dict[str, Any]:
    """Linked / supervised users who are suspended or within 60 days of their end date, for the faculty dashboard."""
    from iic_booking.users import registration_approvals as ra
    from iic_booking.users.models import RegistrationExtensionRequest, RegistrationExtensionStatus

    today = today or timezone.localdate()
    ids = supervised_user_ids(faculty)
    horizon = today + timedelta(days=SUPERVISOR_NEARING_DAYS)
    rows = _safe(
        lambda: list(
            AccountLifecycle.objects.filter(user_id__in=ids, programme_end_date__isnull=False, programme_end_date__lte=horizon)
            .filter(user__is_active=True)
            .select_related("user__department")
            .order_by("programme_end_date")[:300]
        ),
        [],
    )
    pending = _safe(
        lambda: list(
            RegistrationExtensionRequest.objects.filter(faculty=faculty, status=RegistrationExtensionStatus.PENDING)
            .select_related("user__department", "faculty")
            .order_by("created_at")
        ),
        [],
    )
    return {
        "enforcement_enabled": enforcement_enabled(),
        "pending_extensions": [ra.serialize_extension(e, for_faculty=True) for e in pending],
        "users": [
            {
                "user": _person(r.user),
                "category_label": category_label(r.category),
                "degree_name": r.degree_name,
                "programme_end_date": r.programme_end_date.isoformat(),
                "end_date_source": r.end_date_source,
                "end_date_source_note": source_note(r),
                "days_left": (r.programme_end_date - today).days,
                "suspended": enforcement_enabled() and _past_end(r, today),
            }
            for r in rows
        ],
        "nearing_days": SUPERVISOR_NEARING_DAYS,
    }

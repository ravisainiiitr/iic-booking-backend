"""Approval of self-registered (non Channel i) accounts by the IITR faculty member the user named.

Who is in scope
    Accounts created on the sign-up form: external user types, IITR Startups, and IITR Post Doctoral
    Fellows / Research Associates (IITR Student / Individual Student with a display alias). Channel i
    accounts are never listed.

IITR claim
    Post-docs, Research Associates and IITR Startups claim to be IITR users. When such a request names
    a faculty supervisor it goes to that faculty member: automatically when a new registration is
    verified, and for existing requests only when the Main Administrator forwards it. The faculty
    member's approval (with the disclaimer ticked) makes the account fully operational; no further
    approval is needed. External users keep the existing path (automatic for institutional email,
    otherwise Main Administrator approval).

Programme expiry
    ``User.program_end_date`` is the programme validity. The existing sign-in check already refuses
    IITR-type users after that date; the expiry automation (warnings at 30/7/1 days, then Force Inactive)
    is off until the Main Administrator switches it on. Extensions go to the same faculty member and are
    capped at six calendar months from the current validity (or from today when already expired).
    Future bookings of a disabled user are never cancelled; they are listed for the administrator.

Tables may be missing until migration 0128 runs: readers return defaults, writers raise ``SchemaPending``.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable, Iterable, Optional, TypeVar
from urllib.parse import quote

from django.core import signing
from django.db import OperationalError, ProgrammingError, transaction
from django.db.models import Min, Q
from django.utils import timezone

from iic_booking.users.display import get_user_display_name
from iic_booking.users.identity.dates import add_calendar_months
from iic_booking.users.models import (
    DepartmentType,
    RegistrationApproval,
    RegistrationApprovalChannel as Channel,
    RegistrationApprovalEvent as Event,
    RegistrationApprovalPolicy,
    RegistrationApprovalStatus as Status,
    RegistrationApprovalToken,
    RegistrationExtensionRequest,
    RegistrationExtensionStatus as ExtStatus,
    User,
)
from iic_booking.users.models.user_type import UserType

logger = logging.getLogger(__name__)

T = TypeVar("T")
SCHEMA_ERRORS = (ProgrammingError, OperationalError)

DISCLAIMER_VERSION = "2026-10-v1"
EXTENSION_MAX_MONTHS = 6
DEFAULT_WARNING_DAYS = (30, 7, 1)
DEFAULT_TOKEN_VALID_DAYS = 14
USER_EXTENSION_LINK_MAX_AGE = 60 * 60 * 24 * 60
USER_EXTENSION_SALT = "registration-approvals.extension-request"
MAX_REASON_LENGTH = 2000
VIEW_DEDUPE = timedelta(hours=1)

IITR_ALIASES = ("IITR Post Doctoral Fellows", "IITR Research Associates in Projects")
ALIAS_USER_TYPES = (UserType.STUDENT, UserType.INDIVIDUAL_STUDENT)
OPEN_BOOKING_STATUSES = ("PENDING", "PENDING_PAYMENT", "WAITLISTED", "BOOKED", "DISRUPTION_PENDING", "HOLD")

TPL_FACULTY_REQUEST = "registration_faculty_approval_request_email"
TPL_FACULTY_REMINDER = "registration_faculty_approval_reminder_email"
TPL_APPROVED = "registration_request_approved_email"
TPL_REJECTED = "registration_request_rejected_email"
TPL_EXPIRY_WARNING = "registration_expiry_warning_email"
TPL_EXTENSION_REQUEST = "registration_extension_request_email"
TPL_EXTENSION_GRANTED = "registration_extension_granted_email"
TPL_EXTENSION_DENIED = "registration_extension_denied_email"
TPL_DISABLED = "registration_access_disabled_email"
# Emails with Approve / Decline buttons and the decision deadline (new codes: existing DB rows are never re-synced).
TPL_FACULTY_DECISION = "registration_faculty_decision_request_email"
TPL_FACULTY_DECISION_REMINDER = "registration_faculty_decision_reminder_email"
TPL_USER_SENT_TO_FACULTY = "registration_sent_to_faculty_email"
TPL_USER_DECLINED = "registration_declined_reregister_email"
TPL_USER_TIMED_OUT = "registration_timed_out_email"

DEFAULT_DECISION_WINDOW_HOURS = 24
TIMED_OUT_MESSAGE = (
    "Request timed out. The 24-hour window to decide on this registration has passed, so it was treated as "
    "declined and the applicant has been told they can register again."
)

LIST_STATUSES = ("unverified", "pending_faculty", "pending_admin", "approved", "rejected", "expired", "disabled")


class SchemaPending(Exception):
    """The registration approval tables are not migrated yet."""


class ApprovalError(Exception):
    def __init__(self, message: str, code: str, status: int = 400, extra: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.extra = extra or {}


def _safe(fn: Callable[[], T], default: T) -> T:
    try:
        with transaction.atomic():
            return fn()
    except SCHEMA_ERRORS:
        logger.warning("registration approval tables unavailable", exc_info=True)
        return default


def schema_ready() -> bool:
    def probe() -> bool:
        RegistrationApproval.objects.exists()
        Event.objects.exists()
        RegistrationExtensionRequest.objects.exists()
        RegistrationApprovalToken.objects.exists()
        RegistrationApprovalPolicy.objects.exists()
        # Columns added by migration 0129.
        RegistrationApproval.objects.filter(decision_deadline__isnull=False).exists()
        RegistrationApprovalToken.objects.filter(outcome="x").exists()
        RegistrationApprovalPolicy.objects.filter(decision_window_hours=0).exists()
        return True

    return _safe(probe, False)


def require_schema() -> None:
    if not schema_ready():
        raise SchemaPending()


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def policy() -> RegistrationApprovalPolicy:
    row = _safe(lambda: RegistrationApprovalPolicy.objects.order_by("pk").first(), None)
    return row or RegistrationApprovalPolicy()


def automation_enabled() -> bool:
    return bool(policy().expiry_automation_enabled)


def warning_days(row: Optional[RegistrationApprovalPolicy] = None) -> tuple[int, ...]:
    raw = (row or policy()).warning_days or ""
    days: set[int] = set()
    for part in str(raw).split(","):
        try:
            value = int(part.strip())
        except ValueError:
            continue
        if 0 < value <= 365:
            days.add(value)
    return tuple(sorted(days, reverse=True)) or DEFAULT_WARNING_DAYS


def token_valid_days() -> int:
    try:
        return max(1, min(60, int(policy().token_valid_days or DEFAULT_TOKEN_VALID_DAYS)))
    except (TypeError, ValueError):
        return DEFAULT_TOKEN_VALID_DAYS


def decision_window_hours() -> int:
    try:
        value = int(getattr(policy(), "decision_window_hours", None) or DEFAULT_DECISION_WINDOW_HOURS)
    except (TypeError, ValueError):
        return DEFAULT_DECISION_WINDOW_HOURS
    return max(1, min(168, value))


def _fmt_datetime_ist(value) -> str:
    if not value:
        return ""
    return timezone.localtime(value).strftime("%d %b %Y, %I:%M %p")


def deadline_passed(approval: Optional[RegistrationApproval], now=None) -> bool:
    deadline = getattr(approval, "decision_deadline", None)
    return bool(
        approval is not None
        and approval.status == Status.PENDING_FACULTY
        and deadline is not None
        and deadline <= (now or timezone.now())
    )


# ---------------------------------------------------------------------------
# Scope and roles
# ---------------------------------------------------------------------------


def is_main_admin(user) -> bool:
    return bool(getattr(user, "is_authenticated", False)) and getattr(user, "user_type", None) == UserType.ADMIN


def _alias_q() -> Q:
    return Q(user_type__in=ALIAS_USER_TYPES) & ~Q(user_type_alias__isnull=True) & ~Q(user_type_alias="")


def registration_scope_q() -> Q:
    """Accounts created on the sign-up form (never Channel i)."""
    return (
        Q(user_type__in=UserType.get_external_user_codes())
        | Q(user_type=UserType.STARTUP_INCUBATED_IITR)
        | _alias_q()
    )


def claims_iitr_q() -> Q:
    return Q(user_type=UserType.STARTUP_INCUBATED_IITR) | _alias_q()


def claims_iitr(user) -> bool:
    if getattr(user, "user_type", None) == UserType.STARTUP_INCUBATED_IITR:
        return True
    return getattr(user, "user_type", None) in ALIAS_USER_TYPES and bool((getattr(user, "user_type_alias", "") or "").strip())


def scoped_users():
    return User.objects.filter(registration_scope_q()).exclude(is_test_account=True)


def is_eligible_faculty(user) -> bool:
    dept = getattr(user, "department", None)
    return (
        getattr(user, "user_type", None) == UserType.FACULTY
        and dept is not None
        and getattr(dept, "department_type", None) == DepartmentType.INTERNAL
    )


def actor_role(actor, subject=None, approval_or_ext=None) -> str:
    if actor is None or not getattr(actor, "pk", None):
        return "system"
    if is_main_admin(actor):
        return "main_admin"
    faculty_id = getattr(approval_or_ext, "faculty_id", None)
    if faculty_id and faculty_id == actor.pk:
        return "faculty"
    if subject is not None and getattr(subject, "pk", None) == actor.pk:
        return "user"
    return getattr(actor, "user_type", "") or "user"


def _client_ip(request) -> Optional[str]:
    from iic_booking.users.mobile_sessions import client_ip

    try:
        return client_ip(request)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def _fmt_date(value: Optional[date]) -> str:
    return value.strftime("%d %b %Y") if value else ""


def _iso(value) -> Optional[str]:
    return value.isoformat() if value else None


def _name(user) -> str:
    return get_user_display_name(user) if user else ""


def _department_name(user) -> str:
    dept = getattr(user, "department", None)
    if dept:
        return dept.name or ""
    org = getattr(user, "organization_request", None)
    return getattr(org, "name", "") or ""


def user_type_label(user) -> str:
    return user.get_user_type_display_label() or (user.user_type or "")


def registration_disclaimer(user) -> str:
    validity = _fmt_date(user.program_end_date) or "the date shown above"
    return (
        f"I confirm that {_name(user)} is working under my supervision at IIT Roorkee and that, to the best "
        f"of my knowledge, the details shown above, including the programme validity up to {validity}, are correct."
    )


def extension_disclaimer(user, until: Optional[date]) -> str:
    return (
        f"I confirm that {_name(user)} continues to work under my supervision at IIT Roorkee and request that "
        f"their access be extended up to {_fmt_date(until) or 'the date selected'}. I understand that an extension "
        f"is valid for at most six months at a time."
    )


def extension_max_until(user, today: Optional[date] = None) -> date:
    today = today or timezone.localdate()
    end = getattr(user, "program_end_date", None)
    base = end if end and end >= today else today
    return add_calendar_months(base, EXTENSION_MAX_MONTHS)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def fully_approved(user) -> bool:
    if not user.admin_approved:
        return False
    if user.needs_supervisor_approval() and not user.supervisor_approved:
        return False
    return True


def compute_status(user, approval: Optional[RegistrationApproval], today: Optional[date] = None) -> str:
    today = today or timezone.localdate()
    if not user.email_verified and not user.admin_approved:
        return "unverified"
    if fully_approved(user):
        if user.force_inactive or not user.is_active:
            return "disabled"
        if claims_iitr(user) and user.program_end_date and today > user.program_end_date:
            return "expired"
        return "approved"
    if approval is not None and approval.status == Status.REJECTED:
        return "rejected"
    if approval is not None and approval.status == Status.PENDING_FACULTY:
        return "pending_faculty"
    return "pending_admin"


def _approval_for(user) -> Optional[RegistrationApproval]:
    try:
        return user.registration_approval
    except RegistrationApproval.DoesNotExist:
        return None
    except SCHEMA_ERRORS:
        return None


def get_or_create_approval(user) -> RegistrationApproval:
    approval, created = RegistrationApproval.objects.get_or_create(
        user=user, defaults={"faculty": user.supervisor if user.supervisor_id else None}
    )
    if created and fully_approved(user):
        approval.status = Status.APPROVED
        approval.save(update_fields=["status", "updated_at"])
    return approval


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def audit(
    action: str,
    *,
    user=None,
    approval: Optional[RegistrationApproval] = None,
    extension: Optional[RegistrationExtensionRequest] = None,
    actor=None,
    role: str = "",
    channel: str = "",
    request=None,
    **details,
) -> Optional[Event]:
    subject = user or (approval.user if approval else None) or (extension.user if extension else None)
    try:
        with transaction.atomic():
            return Event.objects.create(
                user=subject,
                subject_email=(getattr(subject, "email", "") or "")[:254],
                subject_name=(_name(subject) if subject else "")[:255],
                approval=approval,
                extension=extension,
                action=action,
                actor=actor if getattr(actor, "pk", None) else None,
                actor_email=(getattr(actor, "email", "") or "")[:254] if getattr(actor, "pk", None) else "",
                actor_role=role or actor_role(actor, subject, approval or extension),
                channel=channel or (Channel.SYSTEM if actor is None else Channel.PORTAL),
                ip_address=_client_ip(request),
                details={k: v for k, v in details.items() if v is not None},
            )
    except Exception:
        logger.exception("registration approval audit failed action=%s", action)
        return None


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


def hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _issue_token(*, faculty, approval=None, extension=None, expires_at=None) -> tuple[str, RegistrationApprovalToken]:
    while True:
        raw = secrets.token_urlsafe(32)
        digest = hash_token(raw)
        if not RegistrationApprovalToken.objects.filter(token_hash=digest).exists():
            break
    subject = getattr(approval, "user", None) or getattr(extension, "user", None)
    row = RegistrationApprovalToken.objects.create(
        token_hash=digest,
        purpose=RegistrationApprovalToken.Purpose.EXTENSION if extension else RegistrationApprovalToken.Purpose.REGISTRATION,
        approval=approval,
        extension=extension,
        faculty=faculty,
        expires_at=expires_at or timezone.now() + timedelta(days=token_valid_days()),
        subject_name=(_name(subject) if subject else "")[:255],
    )
    return raw, row


def _close_tokens(approval: RegistrationApproval, outcome: str, now=None) -> None:
    """Retire every link for this request and remember why, so a late click can explain it."""
    now = now or timezone.now()
    RegistrationApprovalToken.objects.filter(approval=approval, used_at__isnull=True).update(used_at=now)
    RegistrationApprovalToken.objects.filter(approval=approval, outcome="").update(outcome=outcome)


def _retire_tokens(*, approval=None, extension=None) -> None:
    qs = RegistrationApprovalToken.objects.filter(used_at__isnull=True)
    if approval is not None:
        qs = qs.filter(approval=approval)
    elif extension is not None:
        qs = qs.filter(extension=extension)
    else:
        return
    qs.update(used_at=timezone.now())


def review_link(raw_token: str) -> str:
    from iic_booking.communication.utils import get_frontend_absolute_url

    target = f"/registration-approvals?token={quote(raw_token, safe='')}"
    return get_frontend_absolute_url(f"/login?next={quote(target, safe='')}")


def decision_link(raw_token: str, action: str) -> str:
    """One-click page from the faculty email; no sign-in, the single-use token is the credential."""
    from iic_booking.communication.utils import get_frontend_absolute_url

    return get_frontend_absolute_url(f"/registration-decision?token={quote(raw_token, safe='')}&action={action}")


def register_link() -> str:
    from iic_booking.communication.utils import get_frontend_absolute_url

    return get_frontend_absolute_url("/auth?mode=register")


def _refuse_closed_token(row: RegistrationApprovalToken, now=None) -> None:
    """Explain why a registration link no longer works: timed out, already decided or removed."""
    if row.purpose != RegistrationApprovalToken.Purpose.REGISTRATION:
        return
    approval = row.approval if row.approval_id else None
    if row.outcome == RegistrationApprovalToken.Outcome.TIMED_OUT or deadline_passed(approval, now):
        raise ApprovalError(TIMED_OUT_MESSAGE, "timed_out", 410)
    if row.outcome in (RegistrationApprovalToken.Outcome.APPROVED, RegistrationApprovalToken.Outcome.DECLINED):
        raise ApprovalError(
            "This request has already been "
            + ("approved." if row.outcome == RegistrationApprovalToken.Outcome.APPROVED else "declined."),
            "already_decided",
            410,
            {"outcome": row.outcome},
        )
    if approval is None:
        raise ApprovalError("This registration request is no longer open.", "request_closed", 410)


def resolve_token(raw_token: str, faculty, *, request=None, lock: bool = False) -> RegistrationApprovalToken:
    """Return the token row when it is valid for ``faculty``; refuse used, expired or someone else's link."""
    digest = hash_token((raw_token or "").strip())
    qs = RegistrationApprovalToken.objects.select_related("approval__user", "extension__user")
    if lock:
        qs = qs.select_for_update(of=("self",))
    row = qs.filter(token_hash=digest).first() if raw_token else None
    if row is None:
        raise ApprovalError("This approval link is not valid.", "token_invalid", 404)
    _refuse_closed_token(row)
    target_user = row.approval.user if row.approval_id else (row.extension.user if row.extension_id else None)
    if row.faculty_id != getattr(faculty, "pk", None):
        audit(
            Event.Action.TOKEN_REFUSED,
            user=target_user,
            approval=row.approval,
            extension=row.extension,
            actor=faculty,
            channel=Channel.EMAIL_LINK,
            request=request,
            reason="wrong_faculty",
        )
        raise ApprovalError(
            "This approval link was sent to a different faculty member. Sign in with the account it was sent to.",
            "wrong_faculty",
            403,
        )
    if row.used_at is not None:
        raise ApprovalError("This approval link has already been used.", "token_used", 410)
    if row.expires_at <= timezone.now():
        raise ApprovalError(
            "This approval link has expired. Open Pending approvals on your dashboard instead.", "token_expired", 410
        )
    return row


def resolve_email_decision_token(raw_token: str, *, viewer=None, request=None) -> RegistrationApprovalToken:
    """Token from the Approve / Decline buttons in the faculty email. No sign-in needed; if someone is signed in
    it must be the faculty member the email went to. An overdue request is timed out here and refused."""
    digest = hash_token((raw_token or "").strip())
    row = (
        RegistrationApprovalToken.objects.select_related("approval__user__department", "faculty")
        .filter(token_hash=digest, purpose=RegistrationApprovalToken.Purpose.REGISTRATION)
        .first()
        if raw_token
        else None
    )
    if row is None:
        raise ApprovalError("This approval link is not valid.", "token_invalid", 404)
    if getattr(viewer, "is_authenticated", False) and viewer.pk != row.faculty_id:
        audit(
            Event.Action.TOKEN_REFUSED,
            user=row.approval.user if row.approval_id else None,
            approval=row.approval,
            actor=viewer,
            channel=Channel.EMAIL_LINK,
            request=request,
            reason="wrong_faculty",
        )
        raise ApprovalError(
            "You are signed in as someone else. This link was sent to a different faculty member; sign out or open "
            "it in a private window.",
            "wrong_faculty",
            403,
        )
    approval = row.approval if row.approval_id else None
    if deadline_passed(approval):
        time_out_approval(approval.pk)
        raise ApprovalError(TIMED_OUT_MESSAGE, "timed_out", 410)
    _refuse_closed_token(row)
    if row.used_at is not None:
        raise ApprovalError("This approval link has already been used.", "token_used", 410)
    if approval.status != Status.PENDING_FACULTY:
        raise ApprovalError("This request has already been decided.", "already_decided", 410)
    if row.expires_at <= timezone.now():
        raise ApprovalError(
            "This approval link has expired. Open Pending approvals on your dashboard instead.", "token_expired", 410
        )
    return row


def serialize_email_decision(row: RegistrationApprovalToken) -> dict[str, Any]:
    approval = row.approval
    data = serialize_request_for_faculty(approval)
    data.update(
        {
            "faculty_name": _name(row.faculty),
            "department": _department_name(approval.user),
            "decision_deadline": _iso(approval.decision_deadline),
            "decision_deadline_display": _fmt_datetime_ist(approval.decision_deadline),
            "window_hours": decision_window_hours(),
        }
    )
    return data


# ---------------------------------------------------------------------------
# Signed link for users (extension request from a warning email / sign-in page)
# ---------------------------------------------------------------------------


def make_user_extension_token(user) -> str:
    return signing.dumps({"u": user.pk}, salt=USER_EXTENSION_SALT, compress=True)


def read_user_extension_token(token: str) -> Optional[User]:
    try:
        data = signing.loads(token or "", salt=USER_EXTENSION_SALT, max_age=USER_EXTENSION_LINK_MAX_AGE)
    except signing.BadSignature:
        return None
    return User.objects.filter(pk=(data or {}).get("u")).first()


def user_extension_link(user) -> str:
    from iic_booking.communication.utils import get_frontend_absolute_url

    return get_frontend_absolute_url(f"/programme-extension?token={quote(make_user_extension_token(user), safe='')}")


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


def _ensure_template(code: str):
    from iic_booking.users.supervisor_invites import _ensure_template as ensure

    return ensure(code)


def _send(
    code: str,
    recipient,
    context: dict[str, Any],
    *,
    cc: Iterable = (),
    created_by=None,
    approval=None,
    extension=None,
) -> bool:
    from iic_booking.communication.service import CommunicationService

    if recipient is None or not (getattr(recipient, "email", "") or "").strip():
        return False
    cc_emails = sorted(
        {
            (getattr(u, "email", "") or "").strip()
            for u in cc
            if u is not None and (getattr(u, "email", "") or "").strip()
            and (getattr(u, "email", "") or "").strip().lower() != recipient.email.strip().lower()
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
            metadata={
                "module": "registration_approval",
                "registration_approval_id": getattr(approval, "pk", None),
                "registration_extension_id": getattr(extension, "pk", None),
            },
            created_by=created_by if getattr(created_by, "pk", None) else None,
            cc_emails=cc_emails or None,
        )
        from iic_booking.communication.models import CommunicationLog

        return bool(log) and getattr(log, "status", "") != CommunicationLog.CommunicationStatus.FAILED
    except Exception as exc:
        logger.exception("registration approval email failed template=%s", code)
        audit(
            Event.Action.EMAIL_FAILED,
            user=getattr(approval, "user", None) or getattr(extension, "user", None),
            approval=approval,
            extension=extension,
            template=code,
            error=str(exc)[:300],
        )
        return False


def _login_link() -> str:
    from iic_booking.communication.utils import get_frontend_absolute_url

    return get_frontend_absolute_url("/login")


def _user_context(user) -> dict[str, Any]:
    return {
        "user_name": _name(user),
        "user_email": user.email,
        "user_type": user_type_label(user),
        "department": _department_name(user),
        "employee_id": user.emp_id or "",
        "phone": user.phone_number or "",
        "programme_start": _fmt_date(user.program_start_date),
        "programme_validity": _fmt_date(user.program_end_date),
    }


def _validity_note(user) -> str:
    if not user.program_end_date:
        return ""
    return (
        f"Your access is valid until {_fmt_date(user.program_end_date)}. Before then you can ask your supervisor "
        f"for an extension; each extension is valid for up to six months."
    )


# ---------------------------------------------------------------------------
# Registration: submit, forward, remind, change faculty
# ---------------------------------------------------------------------------


def on_registration_verified(user, *, request=None) -> Optional[RegistrationApproval]:
    """Called when a new registration is confirmed from the email link. Never raises."""
    if not user.user_type or not User.objects.filter(pk=user.pk).filter(registration_scope_q()).exists():
        return None

    def work() -> RegistrationApproval:
        approval = get_or_create_approval(user)
        audit(
            Event.Action.SUBMITTED,
            approval=approval,
            actor=user,
            role="user",
            channel=Channel.PORTAL,
            request=request,
            claims_iitr=claims_iitr(user),
            faculty_id=user.supervisor_id,
            programme_validity=_iso(user.program_end_date),
        )
        if claims_iitr(user) and user.supervisor_id and not fully_approved(user):
            try:
                forward(user, actor=None, request=request, automatic=True)
            except ApprovalError as err:
                logger.info("registration approval not forwarded user=%s code=%s", user.pk, err.code)
            approval.refresh_from_db()
        return approval

    try:
        return _safe(work, None)
    except Exception:
        logger.exception("registration approval submit failed user=%s", user.pk)
        return None


def _forward_checks(user, approval: RegistrationApproval) -> None:
    if not claims_iitr(user):
        raise ApprovalError("Only requests from users claiming to be IITR users go to a faculty member.", "not_iitr")
    if not user.supervisor_id or not is_eligible_faculty(user.supervisor):
        raise ApprovalError(
            "This request has no IITR faculty supervisor. Set the faculty first.", "faculty_missing"
        )
    if not user.email_verified:
        raise ApprovalError("The user has not verified their email yet.", "email_not_verified")
    if fully_approved(user):
        raise ApprovalError("This account is already approved.", "already_approved")
    if approval.status == Status.REJECTED:
        raise ApprovalError("This request was rejected. Approve it as Main Administrator instead.", "rejected")


@transaction.atomic
def forward(user, *, actor=None, request=None, automatic: bool = False, reminder: bool = False) -> RegistrationApproval:
    approval = get_or_create_approval(user)
    approval = RegistrationApproval.objects.select_for_update().get(pk=approval.pk)
    _forward_checks(user, approval)
    if reminder and approval.status != Status.PENDING_FACULTY:
        raise ApprovalError("A reminder can only be sent for a request that is with the faculty.", "not_with_faculty")
    if reminder and deadline_passed(approval):
        raise ApprovalError(TIMED_OUT_MESSAGE, "timed_out", 410)
    faculty = user.supervisor
    now = timezone.now()
    if reminder and approval.decision_deadline:
        deadline = approval.decision_deadline
    else:
        # Every (re-)forward starts a fresh window; reminders keep the current one.
        deadline = now + timedelta(hours=decision_window_hours())
    _retire_tokens(approval=approval)
    raw, token = _issue_token(faculty=faculty, approval=approval, expires_at=deadline)
    approval.faculty = faculty
    approval.status = Status.PENDING_FACULTY
    approval.decision_deadline = deadline
    fields = ["faculty", "status", "decision_deadline", "updated_at"]
    if reminder:
        approval.last_reminder_at = now
        approval.reminder_count += 1
        fields += ["last_reminder_at", "reminder_count"]
    else:
        approval.forwarded_at = now
        approval.forwarded_by = actor if getattr(actor, "pk", None) else None
        approval.forward_count += 1
        fields += ["forwarded_at", "forwarded_by", "forward_count"]
    approval.save(update_fields=fields)
    hours = decision_window_hours()
    context = {
        **_user_context(user),
        "recipient_name": _name(faculty),
        "faculty_name": _name(faculty),
        "disclaimer_text": registration_disclaimer(user),
        "expires_on": _fmt_date(timezone.localtime(token.expires_at).date()),
        "deadline": _fmt_datetime_ist(deadline),
        "window_hours": str(hours),
        "approve_link": decision_link(raw, "approve"),
        "decline_link": decision_link(raw, "decline"),
        "link": review_link(raw),
    }
    sent = _send(
        TPL_FACULTY_DECISION_REMINDER if reminder else TPL_FACULTY_DECISION,
        faculty,
        context,
        created_by=actor,
        approval=approval,
    )
    audit(
        Event.Action.REMINDER_SENT if reminder else Event.Action.FORWARDED,
        approval=approval,
        actor=actor,
        role="system" if actor is None else "",
        channel=Channel.SYSTEM if actor is None else Channel.PORTAL,
        request=request,
        faculty_id=faculty.pk,
        faculty_email=faculty.email,
        automatic=automatic or None,
        email_sent=sent,
        link_expires_at=_iso(token.expires_at),
        decision_deadline=_iso(deadline),
    )
    if not reminder:
        user_context = {
            **_user_context(user),
            "faculty_name": _name(faculty),
            "deadline": _fmt_datetime_ist(deadline),
            "window_hours": str(hours),
            "link": _login_link(),
        }
        user_sent = _send(TPL_USER_SENT_TO_FACULTY, user, user_context, created_by=actor, approval=approval)
        audit(
            Event.Action.USER_NOTIFIED,
            approval=approval,
            role="system",
            channel=Channel.SYSTEM,
            decision_deadline=_iso(deadline),
            email_sent=user_sent,
        )
    try:
        from iic_booking.communication.in_app import notify_in_app

        notify_in_app(
            [faculty],
            title="Registration awaiting your approval",
            message=f"{_name(user)} named you as their supervisor. Please confirm or decline the registration.",
            link="/registration-approvals",
            event="registration_approval.requested",
            created_by=actor,
        )
    except Exception:
        logger.exception("registration approval in-app notice failed user=%s", user.pk)
    return approval


def remind(user, *, actor, request=None) -> RegistrationApproval:
    return forward(user, actor=actor, request=request, reminder=True)


def _status_for(user, ready: bool, today: date) -> str:
    return compute_status(user, _approval_for(user) if ready else None, today)


def bulk_forward_candidates(ready: Optional[bool] = None):
    """Existing requests that claim IITR, name an eligible faculty member and wait for the administrator."""
    ready = schema_ready() if ready is None else ready
    today = timezone.localdate()
    rows = []
    qs = (
        scoped_users()
        .filter(claims_iitr_q(), email_verified=True, supervisor__isnull=False)
        .select_related("supervisor__department")
    )
    if ready:
        qs = qs.select_related("registration_approval")
    for user in qs:
        if not is_eligible_faculty(user.supervisor):
            continue
        if _status_for(user, ready, today) == "pending_admin":
            rows.append(user)
    return rows


def bulk_forward(*, actor, request=None, expected_count: Optional[int] = None) -> dict[str, Any]:
    candidates = bulk_forward_candidates()
    if expected_count is not None and int(expected_count) != len(candidates):
        raise ApprovalError(
            f"The number of requests changed to {len(candidates)}. Review the count and confirm again.",
            "count_changed",
            409,
            {"count": len(candidates)},
        )
    forwarded, failed = [], []
    for user in candidates:
        try:
            with transaction.atomic():
                forward(user, actor=actor, request=request)
            forwarded.append(user.pk)
        except ApprovalError as err:
            failed.append({"user_id": user.pk, "error": err.message})
    return {"forwarded": len(forwarded), "failed": failed, "user_ids": forwarded}


@transaction.atomic
def change_faculty(user, *, faculty, reason: str, actor, request=None, then_forward: bool = False) -> RegistrationApproval:
    reason = (reason or "").strip()[:MAX_REASON_LENGTH]
    if not reason:
        raise ApprovalError("Give a reason for changing the faculty.", "reason_required")
    if faculty is None or not is_eligible_faculty(faculty):
        raise ApprovalError("Choose an IITR faculty member from an internal department.", "faculty_invalid")
    if faculty.pk == user.pk:
        raise ApprovalError("A user cannot be their own supervisor.", "faculty_invalid")
    approval = get_or_create_approval(user)
    approval = RegistrationApproval.objects.select_for_update().get(pk=approval.pk)
    previous = user.supervisor
    user.supervisor = faculty
    user.save(update_fields=["supervisor"])
    _retire_tokens(approval=approval)
    approval.faculty = faculty
    if approval.status == Status.PENDING_FACULTY:
        approval.status = Status.PENDING_ADMIN
    approval.decision_deadline = None
    approval.save(update_fields=["faculty", "status", "decision_deadline", "updated_at"])
    RegistrationExtensionRequest.objects.filter(user=user, status=ExtStatus.PENDING).update(faculty=faculty)
    audit(
        Event.Action.FACULTY_CHANGED,
        approval=approval,
        actor=actor,
        request=request,
        reason=reason,
        previous_faculty_id=getattr(previous, "pk", None),
        previous_faculty_email=getattr(previous, "email", None),
        faculty_id=faculty.pk,
        faculty_email=faculty.email,
    )
    if then_forward:
        approval = forward(user, actor=actor, request=request)
    return approval


# ---------------------------------------------------------------------------
# Registration: decisions
# ---------------------------------------------------------------------------


def _make_operational(user) -> None:
    user.email_verified = True
    user.admin_approved = True
    fields = ["email_verified", "admin_approved"]
    if user.needs_supervisor_approval():
        user.supervisor_approved = True
        fields.append("supervisor_approved")
    if user.access_on_hold:
        user.access_on_hold = False
        fields.append("access_on_hold")
    user.save(update_fields=fields)


def _notify_decision(user, approval, *, approved: bool, decided_by, reason: str, cc_faculty) -> None:
    context = {
        **_user_context(user),
        "decided_by": _name(decided_by) if decided_by else "IIC Administrator",
        "reason": reason,
        "validity_note": _validity_note(user) if approved and claims_iitr(user) else "",
        "link": _login_link(),
    }
    _send(
        TPL_APPROVED if approved else TPL_REJECTED,
        user,
        context,
        cc=[cc_faculty] if cc_faculty else [],
        created_by=decided_by,
        approval=approval,
    )


def faculty_decide(
    approval: RegistrationApproval,
    *,
    faculty,
    decision: str,
    reason: str = "",
    disclaimer_accepted: bool = False,
    disclaimer_version: str = "",
    raw_token: str = "",
    request=None,
) -> RegistrationApproval:
    if deadline_passed(approval):
        # Commit the timeout (own savepoint) before refusing, so the applicant is told straight away.
        time_out_approval(approval.pk)
        raise ApprovalError(TIMED_OUT_MESSAGE, "timed_out", 410)
    if raw_token:
        # Outside the atomic block so a refused link stays in the audit log.
        resolve_token(raw_token, faculty, request=request)
    with transaction.atomic():
        return _faculty_decide(
            approval,
            faculty=faculty,
            decision=decision,
            reason=reason,
            disclaimer_accepted=disclaimer_accepted,
            disclaimer_version=disclaimer_version,
            raw_token=raw_token,
            request=request,
        )


def _faculty_decide(
    approval: RegistrationApproval,
    *,
    faculty,
    decision: str,
    reason: str,
    disclaimer_accepted: bool,
    disclaimer_version: str,
    raw_token: str,
    request,
) -> RegistrationApproval:
    channel = Channel.EMAIL_LINK if raw_token else Channel.PORTAL
    token = None
    if raw_token:
        token = resolve_token(raw_token, faculty, request=request, lock=True)
        if token.approval_id != approval.pk:
            raise ApprovalError("This approval link is for a different request.", "token_mismatch", 400)
    approval = RegistrationApproval.objects.select_for_update().select_related("user").get(pk=approval.pk)
    if approval.faculty_id != getattr(faculty, "pk", None):
        raise ApprovalError("This request is not addressed to you.", "not_your_request", 403)
    if approval.status != Status.PENDING_FACULTY:
        raise ApprovalError("This request has already been decided.", "already_decided", 409)
    now = timezone.now()
    if deadline_passed(approval, now):
        raise ApprovalError(TIMED_OUT_MESSAGE, "timed_out", 410)
    decision = (decision or "").strip().lower()
    if decision == "decline":
        decision = "disapprove"
    reason = (reason or "").strip()[:MAX_REASON_LENGTH]
    user = approval.user
    if decision == "approve":
        if not disclaimer_accepted:
            raise ApprovalError("Tick the confirmation before approving.", "disclaimer_required")
        if (disclaimer_version or "") != DISCLAIMER_VERSION:
            raise ApprovalError("The confirmation text has changed. Reload the page and try again.", "disclaimer_outdated", 409)
    elif decision == "disapprove":
        if not reason:
            raise ApprovalError("Give a reason for declining. It is emailed to the applicant.", "reason_required")
    else:
        raise ApprovalError("Choose approve or decline.", "decision_invalid")

    approved = decision == "approve"
    disclaimer = registration_disclaimer(user) if approved else ""
    approval.status = Status.APPROVED if approved else Status.REJECTED
    approval.decided_at = now
    approval.decided_by = faculty
    approval.decided_role = "faculty"
    approval.decision_reason = reason
    approval.decision_channel = channel
    approval.disclaimer_text = disclaimer
    approval.disclaimer_version = DISCLAIMER_VERSION if approved else ""
    approval.save()
    if token is not None:
        token.used_at = now
        token.save(update_fields=["used_at"])
    _close_tokens(
        approval,
        RegistrationApprovalToken.Outcome.APPROVED if approved else RegistrationApprovalToken.Outcome.DECLINED,
        now,
    )
    if approved:
        _make_operational(user)
    audit(
        Event.Action.APPROVED if approved else Event.Action.DISAPPROVED,
        approval=approval,
        actor=faculty,
        role="faculty",
        channel=channel,
        request=request,
        reason=reason or None,
        disclaimer_text=disclaimer or None,
        disclaimer_version=DISCLAIMER_VERSION if approved else None,
        programme_validity=_iso(user.program_end_date),
        snapshot=None if approved else request_snapshot(user, approval),
    )
    if approved:
        _notify_decision(user, approval, approved=True, decided_by=faculty, reason=reason, cc_faculty=faculty)
        return approval
    context = {
        **_user_context(user),
        "decided_by": _name(faculty),
        "faculty_name": _name(faculty),
        "reason": reason,
        "link": register_link(),
    }
    _send(TPL_USER_DECLINED, user, context, cc=[faculty], created_by=faculty, approval=approval)
    remove_pending_account(user, approval, outcome="declined", actor=faculty, request=request, channel=channel)
    return approval


# ---------------------------------------------------------------------------
# Registration: removal after a decline or a timeout, and the 24-hour timeout
# ---------------------------------------------------------------------------


def request_snapshot(user, approval: Optional[RegistrationApproval] = None) -> dict[str, Any]:
    """Key details kept in the audit log after the pending account is removed."""
    faculty = getattr(approval, "faculty", None) if approval is not None else None
    faculty = faculty or (user.supervisor if user.supervisor_id else None)
    return {
        "user_id": user.pk,
        "name": _name(user),
        "email": user.email,
        "user_type": user.user_type,
        "user_type_label": user_type_label(user),
        "department": _department_name(user),
        "employee_id": user.emp_id or "",
        "phone": user.phone_number or "",
        "programme_start": _iso(user.program_start_date),
        "programme_validity": _iso(user.program_end_date),
        "registered_at": _iso(user.date_joined),
        "faculty_id": getattr(faculty, "pk", None),
        "faculty_name": _name(faculty) if faculty else "",
        "faculty_email": getattr(faculty, "email", "") if faculty else "",
        "forwarded_at": _iso(getattr(approval, "forwarded_at", None)),
        "decision_deadline": _iso(getattr(approval, "decision_deadline", None)),
    }


def _removal_blocker(user) -> str:
    from iic_booking.equipment.models import Booking

    if fully_approved(user):
        return "account_approved"
    if user.last_login is not None:
        return "has_signed_in"
    if Booking.objects.filter(user=user).exists():
        return "has_bookings"
    return ""


def remove_pending_account(
    user, approval: RegistrationApproval, *, outcome: str, actor=None, request=None, channel: str = ""
) -> bool:
    """Delete a never-approved pending account so the email can register again; the audit log keeps a snapshot.

    Anything that ever became a real account (approved, signed in, has bookings) is kept as Rejected instead.
    """
    from django.db.models import ProtectedError
    from django.db.utils import IntegrityError

    channel = channel or (Channel.SYSTEM if actor is None else Channel.PORTAL)
    blocker = _removal_blocker(user)
    snapshot = request_snapshot(user, approval)
    if not blocker:
        try:
            with transaction.atomic():
                user.delete()
        except (ProtectedError, IntegrityError) as exc:
            blocker = f"delete_refused:{type(exc).__name__}"
    if blocker:
        audit(
            Event.Action.ACCOUNT_REMOVED,
            user=user,
            approval=approval,
            actor=actor,
            role="system" if actor is None else "",
            channel=channel,
            request=request,
            outcome=outcome,
            removed=False,
            kept_reason=blocker,
        )
        return False
    approval._account_removed = True
    try:
        with transaction.atomic():
            Event.objects.create(
                user=None,
                subject_email=(snapshot["email"] or "")[:254],
                subject_name=(snapshot["name"] or "")[:255],
                approval=None,
                action=Event.Action.ACCOUNT_REMOVED,
                actor=actor if getattr(actor, "pk", None) else None,
                actor_email=(getattr(actor, "email", "") or "")[:254] if getattr(actor, "pk", None) else "",
                actor_role="faculty" if getattr(actor, "pk", None) else "system",
                channel=channel,
                ip_address=_client_ip(request),
                details={"outcome": outcome, "removed": True, "snapshot": snapshot},
            )
    except Exception:
        logger.exception("registration approval removal audit failed")
    return True


def time_out_approval(approval_id: int, now=None) -> bool:
    """Treat one overdue request as declined: tell the applicant, then remove the pending account."""
    now = now or timezone.now()
    with transaction.atomic():
        approval = (
            RegistrationApproval.objects.select_for_update(of=("self",))
            .select_related("user", "faculty")
            .filter(pk=approval_id)
            .first()
        )
        if approval is None or not deadline_passed(approval, now):
            return False
        user = approval.user
        if fully_approved(user):
            return False
        hours = decision_window_hours()
        reason = f"No decision was made within {hours} hours of sending the request to the faculty member."
        approval.status = Status.REJECTED
        approval.decided_at = now
        approval.decided_by = None
        approval.decided_role = "system"
        approval.decision_reason = reason
        approval.decision_channel = Channel.SYSTEM
        approval.save()
        _close_tokens(approval, RegistrationApprovalToken.Outcome.TIMED_OUT, now)
        audit(
            Event.Action.TIMED_OUT,
            approval=approval,
            role="system",
            channel=Channel.SYSTEM,
            reason=reason,
            decision_deadline=_iso(approval.decision_deadline),
            snapshot=request_snapshot(user, approval),
        )
        context = {
            **_user_context(user),
            "faculty_name": _name(approval.faculty) if approval.faculty_id else "the faculty member",
            "deadline": _fmt_datetime_ist(approval.decision_deadline),
            "window_hours": str(hours),
            "reason": reason,
            "link": register_link(),
        }
        _send(TPL_USER_TIMED_OUT, user, context, approval=approval)
        remove_pending_account(user, approval, outcome="timed_out")
    return True


def overdue_approvals_q(now=None) -> Q:
    """Only requests actually sent with a deadline; never-forwarded or pre-deadline requests have none."""
    return Q(status=Status.PENDING_FACULTY, decision_deadline__isnull=False, decision_deadline__lte=now or timezone.now())


def process_decision_timeouts(now=None, limit: int = 200) -> dict[str, int]:
    if not schema_ready():
        return {"timed_out": 0, "skipped": 0, "schema_pending": 1}
    now = now or timezone.now()
    ids = list(
        RegistrationApproval.objects.filter(overdue_approvals_q(now)).order_by("decision_deadline").values_list("pk", flat=True)[:limit]
    )
    done = skipped = 0
    for pk in ids:
        try:
            if time_out_approval(pk, now):
                done += 1
            else:
                skipped += 1
        except Exception:
            skipped += 1
            logger.exception("registration decision timeout failed approval=%s", pk)
    return {"timed_out": done, "skipped": skipped}


@transaction.atomic
def admin_decide(user, *, actor, approve: bool, reason: str = "", request=None) -> RegistrationApproval:
    reason = (reason or "").strip()[:MAX_REASON_LENGTH]
    if not approve and not reason:
        raise ApprovalError("A reason is required to reject. It is emailed to the user.", "reason_required")
    approval = get_or_create_approval(user)
    approval = RegistrationApproval.objects.select_for_update().get(pk=approval.pk)
    previous_status = compute_status(user, approval)
    override = previous_status in ("pending_faculty", "rejected") or (
        approval.decided_role == "faculty" and approval.status in (Status.APPROVED, Status.REJECTED)
    )
    now = timezone.now()
    approval.status = Status.APPROVED if approve else Status.REJECTED
    approval.decided_at = now
    approval.decided_by = actor
    approval.decided_role = "main_admin"
    approval.decision_reason = reason
    approval.decision_channel = Channel.PORTAL
    approval.disclaimer_text = ""
    approval.disclaimer_version = ""
    approval.save()
    _close_tokens(
        approval,
        RegistrationApprovalToken.Outcome.APPROVED if approve else RegistrationApprovalToken.Outcome.DECLINED,
        now,
    )
    if approve:
        _make_operational(user)
    elif user.admin_approved:
        user.admin_approved = False
        user.save(update_fields=["admin_approved"])
    audit(
        Event.Action.ADMIN_OVERRIDE if override else (Event.Action.APPROVED if approve else Event.Action.DISAPPROVED),
        approval=approval,
        actor=actor,
        role="main_admin",
        request=request,
        decision="approve" if approve else "reject",
        reason=reason or None,
        previous_status=previous_status,
    )
    cc = approval.faculty if approval.faculty_id and previous_status == "pending_faculty" else None
    _notify_decision(user, approval, approved=approve, decided_by=actor, reason=reason, cc_faculty=cc)
    return approval


def record_view(*, faculty, approval=None, extension=None, channel: str = Channel.PORTAL, request=None) -> None:
    def work():
        since = timezone.now() - VIEW_DEDUPE
        recent = Event.objects.filter(action=Event.Action.VIEWED, actor=faculty, created_at__gte=since)
        recent = recent.filter(approval=approval) if approval is not None else recent.filter(extension=extension)
        if recent.exists():
            return
        if approval is not None and approval.first_viewed_at is None:
            approval.first_viewed_at = timezone.now()
            approval.save(update_fields=["first_viewed_at", "updated_at"])
        audit(
            Event.Action.VIEWED,
            approval=approval,
            extension=extension,
            actor=faculty,
            role="faculty",
            channel=channel,
            request=request,
        )

    _safe(work, None)


# ---------------------------------------------------------------------------
# Extensions
# ---------------------------------------------------------------------------


def pending_extension(user) -> Optional[RegistrationExtensionRequest]:
    return _safe(
        lambda: RegistrationExtensionRequest.objects.filter(user=user, status=ExtStatus.PENDING).order_by("-created_at").first(),
        None,
    )


def can_request_extension(user) -> bool:
    return claims_iitr(user) and bool(user.program_end_date) and fully_approved(user)


@transaction.atomic
def request_extension(user, *, reason: str = "", channel: str = Channel.PORTAL, request=None) -> RegistrationExtensionRequest:
    require_schema()
    if not can_request_extension(user):
        raise ApprovalError(
            "An extension can be requested only by an approved IITR account that has a programme validity date.",
            "not_eligible",
        )
    existing = pending_extension(user)
    if existing is not None:
        return existing
    faculty = user.supervisor if user.supervisor_id and is_eligible_faculty(user.supervisor) else None
    approval = get_or_create_approval(user)
    ext = RegistrationExtensionRequest.objects.create(
        user=user,
        faculty=faculty,
        previous_end_date=user.program_end_date,
        max_until=extension_max_until(user),
        user_reason=(reason or "").strip()[:MAX_REASON_LENGTH],
        requested_channel=channel,
    )
    audit(
        Event.Action.EXTENSION_REQUESTED,
        approval=approval,
        extension=ext,
        actor=user,
        role="user",
        channel=channel,
        request=request,
        faculty_id=getattr(faculty, "pk", None),
        previous_end_date=_iso(ext.previous_end_date),
        max_until=_iso(ext.max_until),
    )
    if faculty is not None:
        _send_extension_request(ext, actor=user)
    return ext


def _send_extension_request(ext: RegistrationExtensionRequest, *, actor=None, reminder: bool = False) -> bool:
    _retire_tokens(extension=ext)
    raw, token = _issue_token(faculty=ext.faculty, extension=ext)
    user = ext.user
    context = {
        **_user_context(user),
        "recipient_name": _name(ext.faculty),
        "current_validity": _fmt_date(ext.previous_end_date),
        "max_until": _fmt_date(ext.max_until),
        "user_reason": ext.user_reason,
        "disclaimer_text": extension_disclaimer(user, ext.max_until),
        "expires_on": _fmt_date(timezone.localtime(token.expires_at).date()),
        "link": review_link(raw),
    }
    return _send(TPL_EXTENSION_REQUEST, ext.faculty, context, created_by=actor, extension=ext)


def remind_extension(ext: RegistrationExtensionRequest, *, actor, request=None) -> bool:
    if ext.status != ExtStatus.PENDING or ext.faculty is None:
        raise ApprovalError("Only a pending extension with a faculty member can be reminded.", "not_with_faculty")
    sent = _send_extension_request(ext, actor=actor, reminder=True)
    audit(Event.Action.REMINDER_SENT, extension=ext, actor=actor, request=request, kind="extension", email_sent=sent)
    return sent


def _parse_until(value) -> Optional[date]:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        return None


def _apply_extension(user, until: date) -> bool:
    """Set the new validity; undo a disable done by the expiry automation. Returns True when re-enabled."""
    user.program_end_date = until
    user.save(update_fields=["program_end_date"])
    approval = _approval_for(user)
    re_enabled = False
    if approval is not None and approval.expiry_set_force_inactive:
        user.force_inactive = False
        user.save(update_fields=["force_inactive"])
        approval.expiry_set_force_inactive = False
        approval.expiry_disabled_at = None
        approval.save(update_fields=["expiry_set_force_inactive", "expiry_disabled_at", "updated_at"])
        re_enabled = True
    return re_enabled


def _notify_extension(ext: RegistrationExtensionRequest, *, granted: bool, decided_by) -> None:
    user = ext.user
    context = {
        **_user_context(user),
        "previous_validity": _fmt_date(ext.previous_end_date),
        "new_validity": _fmt_date(ext.approved_until),
        "decided_by": _name(decided_by) if decided_by else "IIC Administrator",
        "reason": ext.decision_reason,
        "link": _login_link(),
    }
    _send(
        TPL_EXTENSION_GRANTED if granted else TPL_EXTENSION_DENIED,
        user,
        context,
        cc=[ext.faculty] if ext.faculty_id else [],
        created_by=decided_by,
        extension=ext,
    )


def decide_extension(
    ext: RegistrationExtensionRequest,
    *,
    actor,
    decision: str,
    until=None,
    reason: str = "",
    disclaimer_accepted: bool = False,
    disclaimer_version: str = "",
    raw_token: str = "",
    request=None,
) -> RegistrationExtensionRequest:
    if raw_token:
        resolve_token(raw_token, actor, request=request)
    with transaction.atomic():
        return _decide_extension(
            ext,
            actor=actor,
            decision=decision,
            until=until,
            reason=reason,
            disclaimer_accepted=disclaimer_accepted,
            disclaimer_version=disclaimer_version,
            raw_token=raw_token,
            request=request,
        )


def _decide_extension(
    ext: RegistrationExtensionRequest,
    *,
    actor,
    decision: str,
    until,
    reason: str,
    disclaimer_accepted: bool,
    disclaimer_version: str,
    raw_token: str,
    request,
) -> RegistrationExtensionRequest:
    channel = Channel.EMAIL_LINK if raw_token else Channel.PORTAL
    token = None
    if raw_token:
        token = resolve_token(raw_token, actor, request=request, lock=True)
        if token.extension_id != ext.pk:
            raise ApprovalError("This approval link is for a different request.", "token_mismatch", 400)
    ext = RegistrationExtensionRequest.objects.select_for_update().select_related("user").get(pk=ext.pk)
    as_admin = is_main_admin(actor) and not raw_token
    if not as_admin and ext.faculty_id != getattr(actor, "pk", None):
        raise ApprovalError("This request is not addressed to you.", "not_your_request", 403)
    if ext.status != ExtStatus.PENDING:
        raise ApprovalError("This request has already been decided.", "already_decided", 409)
    decision = (decision or "").strip().lower()
    reason = (reason or "").strip()[:MAX_REASON_LENGTH]
    user = ext.user
    granted = decision == "approve"
    new_until = None
    if granted:
        new_until = _parse_until(until) or ext.max_until
        cap = ext.max_until
        if new_until > cap:
            raise ApprovalError(f"An extension can be at most six months, up to {_fmt_date(cap)}.", "extension_too_long")
        floor = max(ext.previous_end_date or timezone.localdate(), timezone.localdate())
        if new_until <= floor:
            raise ApprovalError("Choose a date after the current validity.", "extension_too_short")
        if not as_admin:
            if not disclaimer_accepted:
                raise ApprovalError("Tick the confirmation before approving.", "disclaimer_required")
            if (disclaimer_version or "") != DISCLAIMER_VERSION:
                raise ApprovalError("The confirmation text has changed. Reload the page and try again.", "disclaimer_outdated", 409)
    elif decision == "disapprove":
        if not reason:
            raise ApprovalError("Give a reason for declining the extension.", "reason_required")
    else:
        raise ApprovalError("Choose approve or disapprove.", "decision_invalid")

    now = timezone.now()
    role = "main_admin" if as_admin else "faculty"
    disclaimer = extension_disclaimer(user, new_until) if granted and not as_admin else ""
    ext.status = ExtStatus.APPROVED if granted else ExtStatus.DENIED
    ext.approved_until = new_until
    ext.decided_at = now
    ext.decided_by = actor
    ext.decided_role = role
    ext.decision_reason = reason
    ext.decision_channel = channel
    ext.disclaimer_text = disclaimer
    ext.disclaimer_version = DISCLAIMER_VERSION if disclaimer else ""
    ext.save()
    if token is not None:
        token.used_at = now
        token.save(update_fields=["used_at"])
    _retire_tokens(extension=ext)
    approval = _approval_for(user)
    re_enabled = _apply_extension(user, new_until) if granted else False
    audit(
        Event.Action.EXTENSION_GRANTED if granted else Event.Action.EXTENSION_DENIED,
        approval=approval,
        extension=ext,
        actor=actor,
        role=role,
        channel=channel,
        request=request,
        previous_end_date=_iso(ext.previous_end_date),
        approved_until=_iso(new_until),
        max_until=_iso(ext.max_until),
        reason=reason or None,
        disclaimer_text=disclaimer or None,
        disclaimer_version=DISCLAIMER_VERSION if disclaimer else None,
    )
    if re_enabled:
        audit(Event.Action.RE_ENABLED, approval=approval, extension=ext, actor=actor, role=role, channel=channel, request=request)
    _notify_extension(ext, granted=granted, decided_by=actor)
    return ext


@transaction.atomic
def admin_grant_extension(user, *, actor, until, reason: str, request=None) -> RegistrationExtensionRequest:
    """Main Administrator extends access directly (same six-month cap); closes any pending request."""
    require_schema()
    reason = (reason or "").strip()[:MAX_REASON_LENGTH]
    if not reason:
        raise ApprovalError("Give a reason for the extension.", "reason_required")
    if not user.program_end_date:
        raise ApprovalError("This user has no programme validity date.", "no_validity")
    ext = pending_extension(user)
    if ext is None:
        ext = RegistrationExtensionRequest.objects.create(
            user=user,
            faculty=user.supervisor if user.supervisor_id else None,
            previous_end_date=user.program_end_date,
            max_until=extension_max_until(user),
            requested_channel=Channel.PORTAL,
            user_reason="",
        )
    return decide_extension(ext, actor=actor, decision="approve", until=until, reason=reason, request=request)


# ---------------------------------------------------------------------------
# Expiry automation
# ---------------------------------------------------------------------------


def expiry_scope(today: Optional[date] = None):
    """Approved, IITR-claiming self-registered accounts with a programme validity date."""
    return (
        scoped_users()
        .filter(claims_iitr_q(), program_end_date__isnull=False, admin_approved=True)
        .select_related("supervisor", "department")
    )


def future_bookings(user, now=None):
    from iic_booking.equipment.models import Booking

    now = now or timezone.now()
    return (
        Booking.objects.filter(user=user, status__in=OPEN_BOOKING_STATUSES)
        .annotate(first_slot_start=Min("daily_slots__start_datetime"))
        .filter(first_slot_start__gte=now)
        .select_related("equipment")
        .order_by("first_slot_start")
    )


def serialize_booking(b) -> dict[str, Any]:
    from iic_booking.communication.utils import booking_display_id_for_email

    return {
        "booking_id": b.booking_id,
        "display_id": booking_display_id_for_email(b) or str(b.booking_id),
        "equipment": getattr(b.equipment, "name", ""),
        "status": b.status,
        "starts_at": _iso(getattr(b, "first_slot_start", None)),
    }


@dataclass
class _Plan:
    warn: list[tuple[User, int]]
    disable: list[User]


def _plan(today: date) -> _Plan:
    days = warning_days()
    warn: list[tuple[User, int]] = []
    disable: list[User] = []
    for user in expiry_scope(today).select_related("registration_approval"):
        if not fully_approved(user):
            continue
        end = user.program_end_date
        left = (end - today).days
        if left < 0:
            if not user.force_inactive and user.is_active:
                disable.append(user)
            continue
        due = [d for d in days if left <= d]
        if not due:
            continue
        approval = _approval_for(user)
        sent = set((approval.expiry_warnings_sent or {}).get(end.isoformat(), [])) if approval else set()
        smallest = min(due)
        if smallest not in sent:
            warn.append((user, smallest))
    return _Plan(warn=warn, disable=disable)


def dry_run(today: Optional[date] = None) -> dict[str, Any]:
    today = today or timezone.localdate()
    plan = _plan(today)
    now = timezone.now()

    def row(user, extra=None):
        data = {
            "user_id": user.pk,
            "name": _name(user),
            "email": user.email,
            "programme_validity": _iso(user.program_end_date),
            "faculty": _name(user.supervisor) if user.supervisor_id else "",
            "future_bookings": future_bookings(user, now).count(),
        }
        data.update(extra or {})
        return data

    return {
        "automation_enabled": automation_enabled(),
        "today": today.isoformat(),
        "warning_days": list(warning_days()),
        "would_warn": [row(u, {"days": d}) for u, d in plan.warn],
        "would_disable": [row(u) for u in plan.disable],
        "counts": {"would_warn": len(plan.warn), "would_disable": len(plan.disable)},
    }


def _warn(user, days: int, *, today: date) -> None:
    approval = get_or_create_approval(user)
    end = user.program_end_date
    context = {
        **_user_context(user),
        "days_left": str(days),
        "faculty_name": _name(user.supervisor) if user.supervisor_id else "",
        "link": user_extension_link(user),
    }
    sent = _send(TPL_EXPIRY_WARNING, user, context, cc=[user.supervisor] if user.supervisor_id else [], approval=approval)
    record = dict(approval.expiry_warnings_sent or {})
    marks = set(record.get(end.isoformat(), []))
    marks.update(d for d in warning_days() if d >= days)
    record = {end.isoformat(): sorted(marks, reverse=True)}
    approval.expiry_warnings_sent = record
    approval.save(update_fields=["expiry_warnings_sent", "updated_at"])
    audit(Event.Action.EXPIRY_WARNING, approval=approval, days=days, programme_validity=end.isoformat(), email_sent=sent)


@transaction.atomic
def _disable(user, *, now) -> None:
    user = User.objects.select_for_update().get(pk=user.pk)
    if user.force_inactive:
        return
    approval = get_or_create_approval(user)
    user.force_inactive = True
    user.save(update_fields=["force_inactive"])
    approval.expiry_set_force_inactive = True
    approval.expiry_disabled_at = now
    approval.save(update_fields=["expiry_set_force_inactive", "expiry_disabled_at", "updated_at"])
    bookings = [serialize_booking(b) for b in future_bookings(user, now)[:50]]
    context = {
        **_user_context(user),
        "faculty_name": _name(user.supervisor) if user.supervisor_id else "",
        "link": user_extension_link(user),
    }
    sent = _send(TPL_DISABLED, user, context, cc=[user.supervisor] if user.supervisor_id else [], approval=approval)
    audit(
        Event.Action.DISABLED,
        approval=approval,
        programme_validity=_iso(user.program_end_date),
        future_bookings_kept=[b["display_id"] for b in bookings],
        email_sent=sent,
    )


def run_expiry(today: Optional[date] = None, *, force: bool = False) -> dict[str, Any]:
    """Send due warnings and disable expired accounts. No-op unless the automation is switched on."""
    if not schema_ready():
        return {"ran": False, "reason": "schema_pending"}
    if not force and not automation_enabled():
        return {"ran": False, "reason": "disabled"}
    today = today or timezone.localdate()
    now = timezone.now()
    plan = _plan(today)
    warned = disabled = 0
    for user, days in plan.warn:
        try:
            with transaction.atomic():
                _warn(user, days, today=today)
            warned += 1
        except Exception:
            logger.exception("registration expiry warning failed user=%s", user.pk)
    for user in plan.disable:
        try:
            _disable(user, now=now)
            disabled += 1
        except Exception:
            logger.exception("registration expiry disable failed user=%s", user.pk)
    return {"ran": True, "warned": warned, "disabled": disabled}


def set_automation(enabled: bool, *, actor=None, request=None) -> RegistrationApprovalPolicy:
    require_schema()
    row = RegistrationApprovalPolicy.objects.order_by("pk").first() or RegistrationApprovalPolicy()
    previous = bool(row.expiry_automation_enabled)
    row.expiry_automation_enabled = bool(enabled)
    if not enabled:
        row.enabled_at = None
    elif not previous:
        row.enabled_at = timezone.now()
    row.updated_by = actor if getattr(actor, "pk", None) else None
    row.save()
    if previous != bool(enabled):
        audit(
            Event.Action.AUTOMATION_CHANGED,
            actor=actor,
            role="main_admin" if actor is not None else "system",
            channel=Channel.PORTAL if actor is not None else Channel.SYSTEM,
            request=request,
            enabled=bool(enabled),
        )
    return row


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _person(user) -> Optional[dict[str, Any]]:
    if user is None:
        return None
    dept = getattr(user, "department", None)
    return {
        "id": user.pk,
        "name": _name(user),
        "email": user.email,
        "department": getattr(dept, "name", "") if dept else "",
    }


def serialize_row(user, today: Optional[date] = None) -> dict[str, Any]:
    today = today or timezone.localdate()
    approval = _approval_for(user)
    return {
        "user_id": user.pk,
        "name": _name(user),
        "email": user.email,
        "user_type": user.user_type,
        "user_type_label": user_type_label(user),
        "department": _department_name(user),
        "department_id": user.department_id,
        "claims_iitr": claims_iitr(user),
        "faculty": _person(user.supervisor) if user.supervisor_id else None,
        "faculty_missing": claims_iitr(user) and not user.supervisor_id,
        "status": compute_status(user, approval, today),
        "registered_at": _iso(user.date_joined),
        "programme_validity": _iso(user.program_end_date),
        "email_verified": user.email_verified,
        "forwarded_at": _iso(approval.forwarded_at) if approval else None,
        "decision_deadline": _iso(approval.decision_deadline)
        if approval is not None and approval.status == Status.PENDING_FACULTY
        else None,
        "reminder_count": approval.reminder_count if approval else 0,
        "decided_at": _iso(approval.decided_at) if approval else None,
        "decided_role": approval.decided_role if approval else "",
    }


def serialize_event(e: Event) -> dict[str, Any]:
    return {
        "id": e.pk,
        "at": _iso(e.created_at),
        "action": e.action,
        "action_label": e.get_action_display(),
        "user_id": e.user_id,
        "user_email": e.subject_email,
        "user_name": e.subject_name,
        "actor_email": e.actor_email,
        "actor_name": _name(e.actor) if e.actor_id else "",
        "actor_role": e.actor_role,
        "channel": e.channel,
        "ip_address": e.ip_address,
        "details": e.details or {},
        "extension_id": e.extension_id,
    }


def serialize_extension(ext: RegistrationExtensionRequest, *, for_faculty: bool = False) -> dict[str, Any]:
    user = ext.user
    data = {
        "id": ext.pk,
        "kind": "extension",
        "user": _person(user),
        "user_type_label": user_type_label(user),
        "status": ext.status,
        "faculty": _person(ext.faculty) if ext.faculty_id else None,
        "previous_end_date": _iso(ext.previous_end_date),
        "max_until": _iso(ext.max_until),
        "approved_until": _iso(ext.approved_until),
        "user_reason": ext.user_reason,
        "requested_channel": ext.requested_channel,
        "created_at": _iso(ext.created_at),
        "decided_at": _iso(ext.decided_at),
        "decided_role": ext.decided_role,
        "decided_by": _name(ext.decided_by) if ext.decided_by_id else "",
        "decision_reason": ext.decision_reason,
        "disclaimer_text": ext.disclaimer_text,
        "max_months": EXTENSION_MAX_MONTHS,
    }
    if for_faculty and ext.status == ExtStatus.PENDING:
        data["disclaimer_template"] = extension_disclaimer(user, ext.max_until)
        data["disclaimer_version"] = DISCLAIMER_VERSION
    return data


def _documents(user, request=None) -> list[dict[str, Any]]:
    try:
        from iic_booking.users.models import UserDocument

        rows = []
        for doc in UserDocument.objects.filter(user=user).order_by("-pk")[:20]:
            url = ""
            try:
                url = request.build_absolute_uri(doc.file.url) if request else doc.file.url
            except Exception:
                url = ""
            rows.append(
                {
                    "id": doc.pk,
                    "document_type": getattr(doc, "document_type", "") or "",
                    "description": getattr(doc, "description", "") or "",
                    "url": url,
                    "uploaded_at": _iso(getattr(doc, "uploaded_at", None) or getattr(doc, "created_at", None)),
                }
            )
        return rows
    except Exception:
        logger.exception("registration approval documents failed user=%s", user.pk)
        return []


def serialize_request_for_faculty(approval: RegistrationApproval) -> dict[str, Any]:
    user = approval.user
    data = {
        "id": approval.pk,
        "kind": "registration",
        "status": approval.status,
        "user": _person(user),
        "user_type_label": user_type_label(user),
        "employee_id": user.emp_id or "",
        "phone": user.phone_number or "",
        "programme_start": _iso(user.program_start_date),
        "programme_validity": _iso(user.program_end_date),
        "registered_at": _iso(user.date_joined),
        "forwarded_at": _iso(approval.forwarded_at),
        "decision_deadline": _iso(approval.decision_deadline) if approval.status == Status.PENDING_FACULTY else None,
        "decided_at": _iso(approval.decided_at),
        "decision_reason": approval.decision_reason,
        "disclaimer_text": approval.disclaimer_text,
        "account_removed": bool(getattr(approval, "_account_removed", False)),
    }
    if approval.status == Status.PENDING_FACULTY:
        data["disclaimer_template"] = registration_disclaimer(user)
        data["disclaimer_version"] = DISCLAIMER_VERSION
    return data


def serialize_detail(user, *, request=None) -> dict[str, Any]:
    today = timezone.localdate()
    approval = _approval_for(user)
    row = serialize_row(user, today)
    events = list(Event.objects.filter(user=user).select_related("actor").order_by("created_at", "id")[:500])
    extensions = list(
        RegistrationExtensionRequest.objects.filter(user=user).select_related("faculty", "decided_by", "user").order_by("-created_at")
    )
    now = timezone.now()
    row.update(
        {
            "phone": user.phone_number or "",
            "employee_id": user.emp_id or "",
            "gender": user.gender or "",
            "programme_start": _iso(user.program_start_date),
            "access_on_hold": user.access_on_hold,
            "admin_approved": user.admin_approved,
            "supervisor_approved": user.supervisor_approved,
            "needs_supervisor_approval": user.needs_supervisor_approval(),
            "is_active": user.is_active,
            "force_inactive": user.force_inactive,
            "documents": _documents(user, request),
            "approval": None
            if approval is None
            else {
                "status": approval.status,
                "faculty": _person(approval.faculty) if approval.faculty_id else None,
                "forward_count": approval.forward_count,
                "forwarded_at": _iso(approval.forwarded_at),
                "decision_deadline": _iso(approval.decision_deadline),
                "last_reminder_at": _iso(approval.last_reminder_at),
                "first_viewed_at": _iso(approval.first_viewed_at),
                "decided_at": _iso(approval.decided_at),
                "decided_by": _name(approval.decided_by) if approval.decided_by_id else "",
                "decided_role": approval.decided_role,
                "decision_reason": approval.decision_reason,
                "decision_channel": approval.decision_channel,
                "disclaimer_text": approval.disclaimer_text,
                "disclaimer_version": approval.disclaimer_version,
                "expiry_disabled_at": _iso(approval.expiry_disabled_at),
                "expiry_set_force_inactive": approval.expiry_set_force_inactive,
            },
            "extensions": [serialize_extension(e) for e in extensions],
            "extension_max_until": _iso(extension_max_until(user, today)) if user.program_end_date else None,
            "future_bookings": [serialize_booking(b) for b in future_bookings(user, now)[:50]],
            "timeline": [serialize_event(e) for e in events],
        }
    )
    return row


# ---------------------------------------------------------------------------
# Listing and reports
# ---------------------------------------------------------------------------


def list_rows(params: dict[str, Any]) -> list[dict[str, Any]]:
    today = timezone.localdate()
    qs = scoped_users().select_related("department", "organization_request", "supervisor__department")
    qs = qs.select_related("registration_approval")
    claims = (params.get("claims_iitr") or "").strip().lower()
    if claims in ("1", "true", "yes"):
        qs = qs.filter(claims_iitr_q())
    elif claims in ("0", "false", "no"):
        qs = qs.exclude(claims_iitr_q())
    if params.get("faculty"):
        try:
            qs = qs.filter(supervisor_id=int(params["faculty"]))
        except (TypeError, ValueError):
            pass
    if params.get("department"):
        try:
            qs = qs.filter(department_id=int(params["department"]))
        except (TypeError, ValueError):
            pass
    if (params.get("faculty_missing") or "").lower() in ("1", "true", "yes"):
        qs = qs.filter(claims_iitr_q(), supervisor__isnull=True)
    start = _parse_until(params.get("date_from"))
    end = _parse_until(params.get("date_to"))
    if start:
        qs = qs.filter(date_joined__date__gte=start)
    if end:
        qs = qs.filter(date_joined__date__lte=end)
    q = (params.get("q") or "").strip()
    if q:
        qs = qs.filter(
            Q(name__icontains=q) | Q(email__icontains=q) | Q(emp_id__icontains=q) | Q(phone_number__icontains=q)
            | Q(supervisor__name__icontains=q) | Q(supervisor__email__icontains=q)
            | Q(department__name__icontains=q) | Q(organization_request__name__icontains=q)
        )
    wanted = {s for s in str(params.get("status") or "").split(",") if s in LIST_STATUSES}
    rows = []
    for user in qs.order_by("-date_joined")[:5000]:
        row = serialize_row(user, today)
        if wanted and row["status"] not in wanted:
            continue
        rows.append(row)
    return rows


def pending_admin_count() -> int:
    today = timezone.localdate()
    qs = (
        scoped_users()
        .filter(email_verified=True)
        .filter(Q(admin_approved=False) | Q(supervisor_approved=False))
        .select_related("registration_approval")
    )
    return sum(1 for u in qs if compute_status(u, _approval_for(u), today) == "pending_admin")


def summary_counts() -> dict[str, Any]:
    today = timezone.localdate()
    counts = {s: 0 for s in LIST_STATUSES}
    iitr_pending = missing_faculty = 0
    qs = scoped_users().select_related("registration_approval", "supervisor__department")
    for user in qs:
        status = compute_status(user, _approval_for(user), today)
        counts[status] += 1
        if claims_iitr(user) and status in ("pending_admin", "pending_faculty"):
            iitr_pending += 1
            if not user.supervisor_id:
                missing_faculty += 1
    pending_ext = _safe(lambda: RegistrationExtensionRequest.objects.filter(status=ExtStatus.PENDING).count(), 0)
    return {
        "by_status": counts,
        "iitr_pending": iitr_pending,
        "iitr_pending_missing_faculty": missing_faculty,
        "bulk_forward_candidates": len(bulk_forward_candidates(True)),
        "pending_extensions": pending_ext,
        "automation_enabled": automation_enabled(),
        "decision_window_hours": decision_window_hours(),
    }


def production_report(today: Optional[date] = None) -> dict[str, Any]:
    """Read-only counts for the deploy report; no names or addresses."""
    today = today or timezone.localdate()
    ready = schema_ready()
    soon = today + timedelta(days=30)
    scope = scoped_users()
    iitr = scope.filter(claims_iitr_q())
    pending_iitr = [
        u for u in iitr.select_related("supervisor__department")
        if _status_for(u, ready, today) in ("pending_admin", "pending_faculty")
    ]
    approved_iitr = iitr.filter(admin_approved=True, program_end_date__isnull=False)
    expired = approved_iitr.filter(program_end_date__lt=today)
    within_30 = approved_iitr.filter(program_end_date__gte=today, program_end_date__lte=soon)
    expired_users = list(expired)
    now = timezone.now()
    return {
        "today": today.isoformat(),
        "registered_accounts_in_scope": scope.count(),
        "claiming_iitr": iitr.count(),
        "pending_claiming_iitr": len(pending_iitr),
        "pending_claiming_iitr_missing_faculty": sum(1 for u in pending_iitr if not u.supervisor_id),
        "pending_claiming_iitr_faculty_not_eligible": sum(
            1 for u in pending_iitr if u.supervisor_id and not is_eligible_faculty(u.supervisor)
        ),
        "bulk_forward_candidates": len(bulk_forward_candidates(ready)),
        "pending_not_iitr": sum(
            1 for u in scope.exclude(claims_iitr_q()) if _status_for(u, ready, today) in ("pending_admin", "pending_faculty")
        ),
        "approved_iitr_programme_expired": len(expired_users),
        "approved_iitr_programme_expired_still_active": sum(1 for u in expired_users if u.is_active and not u.force_inactive),
        "approved_iitr_programme_expiring_30_days": within_30.count(),
        "expired_with_future_bookings": sum(1 for u in expired_users if future_bookings(u, now).exists()),
        "all_scope_programme_expired": scope.filter(admin_approved=True, program_end_date__lt=today).count(),
        "all_scope_programme_expiring_30_days": scope.filter(
            admin_approved=True, program_end_date__gte=today, program_end_date__lte=soon
        ).count(),
        "portal_wide_programme_expired_active": User.objects.filter(
            is_active=True, program_end_date__lt=today
        ).exclude(is_test_account=True).count(),
        "portal_wide_programme_expiring_30_days": User.objects.filter(
            is_active=True, program_end_date__gte=today, program_end_date__lte=soon
        ).exclude(is_test_account=True).count(),
        "automation_enabled": automation_enabled(),
        "schema_ready": ready,
        **_decision_timer_counts(ready, now),
    }


def _decision_timer_counts(ready: bool, now) -> dict[str, Any]:
    if not ready:
        return {}

    def counts() -> dict[str, Any]:
        with_faculty = RegistrationApproval.objects.filter(status=Status.PENDING_FACULTY)
        return {
            "decision_window_hours": decision_window_hours(),
            "with_faculty_timer_running": with_faculty.filter(decision_deadline__gt=now).count(),
            "with_faculty_overdue": with_faculty.filter(overdue_approvals_q(now)).count(),
            "with_faculty_no_timer": with_faculty.filter(decision_deadline__isnull=True).count(),
            "pending_admin_never_forwarded": RegistrationApproval.objects.filter(
                status=Status.PENDING_ADMIN, forwarded_at__isnull=True
            ).count(),
        }

    return _safe(counts, {})

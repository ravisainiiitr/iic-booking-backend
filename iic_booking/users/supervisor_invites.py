"""Invite a supervisor who is not on the portal yet to review a student's wallet link request.

The email only links to the normal portal sign-in. The token in the link is used after sign-in to
route the faculty member to the request; it never authenticates anyone. Pending invites become
ordinary pending ``WalletJoinRequest`` rows when a faculty member with the invited email signs in.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta
from html import escape
from typing import Any, Optional
from urllib.parse import quote

from django.conf import settings
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import (
    Department,
    DepartmentType,
    SupervisorInvite,
    SupervisorInviteEvent,
    SupervisorInviteStatus,
    User,
    WalletJoinRequest,
    WalletJoinRequestStatus,
)
from .models.user_type import UserType

logger = logging.getLogger(__name__)

INVITE_EMAIL_TEMPLATE = "supervisor_invite_email"
ACCEPTED_EMAIL_TEMPLATE = "supervisor_invite_accepted_student_email"

INVITE_VALID_DAYS = 30
MAX_ACTIVE_INVITES_PER_STUDENT = 3
RESEND_COOLDOWN = timedelta(hours=24)
DEFAULT_EMAIL_DAILY_CAP = 5
MAX_MESSAGE_LENGTH = 1000
MAX_NAME_LENGTH = 255

INVITER_USER_TYPES = {UserType.STUDENT, UserType.OTHER}


class InviteError(Exception):
    def __init__(self, message: str, code: str, status: int = 400, extra: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.extra = extra or {}


def allowed_email_domains() -> list[str]:
    raw = getattr(settings, "SUPERVISOR_INVITE_EMAIL_DOMAINS", None) or ["iitr.ac.in"]
    if isinstance(raw, str):
        raw = raw.split(",")
    return [d.strip().lower().lstrip("@") for d in raw if d and d.strip()]


def email_daily_cap() -> int:
    try:
        return max(1, int(getattr(settings, "SUPERVISOR_INVITE_EMAIL_DAILY_CAP", DEFAULT_EMAIL_DAILY_CAP)))
    except (TypeError, ValueError):
        return DEFAULT_EMAIL_DAILY_CAP


def normalize_email(value: Any) -> str:
    return str(value or "").strip().lower()


def is_allowed_domain(email: str) -> bool:
    domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    return any(domain == d or domain.endswith("." + d) for d in allowed_email_domains())


def hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _new_token() -> tuple[str, str]:
    while True:
        token = secrets.token_urlsafe(32)
        digest = hash_token(token)
        if not SupervisorInvite.objects.filter(token_hash=digest).exists():
            return token, digest


def review_link(token: str) -> str:
    from iic_booking.communication.utils import get_frontend_absolute_url

    target = f"/wallet?supervisor_invite={token}#wallet-join-requests"
    return get_frontend_absolute_url(f"/login?next={quote(target, safe='')}")


def audit(action: str, *, invite: Optional[SupervisorInvite] = None, actor=None, email: str = "", **details) -> None:
    try:
        SupervisorInviteEvent.objects.create(
            invite=invite,
            action=action,
            actor=actor if getattr(actor, "pk", None) else None,
            email=email or (invite.email if invite else ""),
            details=details,
        )
    except Exception:
        logger.exception("supervisor invite audit failed action=%s", action)


def expire_stale(qs=None) -> int:
    now = timezone.now()
    qs = qs if qs is not None else SupervisorInvite.objects.all()
    stale = list(qs.filter(status=SupervisorInviteStatus.PENDING, expires_at__lte=now))
    for invite in stale:
        invite.status = SupervisorInviteStatus.EXPIRED
        invite.save(update_fields=["status"])
        audit(SupervisorInviteEvent.Action.EXPIRED, invite=invite)
    return len(stale)


def _student_programme(student) -> str:
    return (getattr(student, "degree_name", "") or "").strip()


def _student_department(student) -> str:
    dept = getattr(student, "department", None)
    name = (getattr(dept, "name", "") or "").strip() if dept else ""
    return name or (getattr(student, "branch_name", "") or "").strip()


def _ensure_template(code: str):
    """Create the catalog template when missing; never touch an existing (possibly admin-edited) row."""
    from iic_booking.communication.models import CommunicationTemplate
    from iic_booking.communication.utils import get_current_user, set_current_user, clear_current_user

    existing = CommunicationTemplate.objects.filter(
        code=code, communication_type=CommunicationTemplate.CommunicationType.EMAIL
    ).first()
    if existing is not None:
        return existing if existing.is_active else None
    from iic_booking.communication.default_email_templates import get_default_email_template

    spec = get_default_email_template(code)
    if spec is None:
        return None
    fields = {k: spec[k] for k in ("name", "subject", "body_text", "body_html", "description", "variable_help")}
    # Template saves are restricted to staff when a request user is set; this is a system default.
    previous = get_current_user()
    clear_current_user()
    try:
        template, _ = CommunicationTemplate.objects.get_or_create(
            code=code,
            communication_type=CommunicationTemplate.CommunicationType.EMAIL,
            defaults={**fields, "is_active": True},
        )
    finally:
        if previous is not None:
            set_current_user(previous)
    return template


def _render(template, context: dict[str, str], html_fields: tuple[str, ...]) -> dict[str, str]:
    """Plain text uses raw values; HTML gets user-supplied values escaped."""
    from iic_booking.communication.service import CommunicationService

    text = CommunicationService.render_template(template, context)
    html_ctx = {k: (escape(v) if k in html_fields and isinstance(v, str) else v) for k, v in context.items()}
    html = CommunicationService.render_template(template, html_ctx)
    return {"subject": text.get("subject", ""), "text": text.get("message", ""), "html": html.get("html_message", "")}


def _deliver(*, to_email: str, rendered: dict[str, str], template, metadata: dict, created_by=None, recipient=None) -> bool:
    from iic_booking.communication.models import CommunicationLog
    from iic_booking.users.test_accounts import email_redirects, is_test_user, redirect_email_address

    subject = rendered["subject"]
    inviter = created_by
    if inviter is not None and is_test_user(inviter):
        delivery = email_redirects() or []
    else:
        delivery, subject = redirect_email_address(to_email, subject=subject)
    if not delivery:
        logger.warning("supervisor invite email skipped: no delivery address (template=%s)", template.code)
        return False
    meta = dict(metadata)
    if [d.lower() for d in delivery] != [to_email.lower()]:
        meta["test_account_email_redirect"] = ", ".join(delivery)
        meta["original_recipient_email"] = to_email
    log = CommunicationLog.objects.create(
        communication_type=CommunicationLog.CommunicationType.EMAIL,
        recipient=recipient,
        recipient_email=", ".join(delivery)[:255],
        template=template,
        subject=subject,
        message=rendered["text"],
        status=CommunicationLog.CommunicationStatus.PENDING,
        metadata=meta,
        created_by=created_by if getattr(created_by, "pk", None) else None,
    )
    try:
        send_mail(
            subject=subject,
            message=rendered["text"],
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=delivery,
            html_message=rendered["html"] or None,
            fail_silently=False,
        )
    except Exception as exc:
        log.status = CommunicationLog.CommunicationStatus.FAILED
        log.error_message = str(exc)[:2000]
        log.save(update_fields=["status", "error_message"])
        logger.exception("supervisor invite email failed template=%s", template.code)
        return False
    log.status = CommunicationLog.CommunicationStatus.SENT
    log.sent_at = timezone.now()
    log.save(update_fields=["status", "sent_at"])
    return True


def send_invite_email(invite: SupervisorInvite, token: str) -> bool:
    template = _ensure_template(INVITE_EMAIL_TEMPLATE)
    if template is None:
        audit(SupervisorInviteEvent.Action.EMAIL_FAILED, invite=invite, reason="template_missing")
        return False
    student = invite.student
    name = (invite.supervisor_name or "").strip()
    context = {
        "recipient_name": name or "Professor",
        "student_name": (student.name or "").strip() or student.email,
        "student_email": student.email,
        "student_programme": _student_programme(student),
        "student_department": _student_department(student),
        "message": invite.message or "",
        "expires_on": timezone.localtime(invite.expires_at).strftime("%d %b %Y"),
        "link": review_link(token),
    }
    rendered = _render(
        template,
        context,
        ("recipient_name", "student_name", "student_email", "student_programme", "student_department", "message"),
    )
    ok = _deliver(
        to_email=invite.email,
        rendered=rendered,
        template=template,
        metadata={"module": "supervisor_invite", "supervisor_invite_id": invite.pk},
        created_by=student,
    )
    if not ok:
        audit(SupervisorInviteEvent.Action.EMAIL_FAILED, invite=invite, actor=student)
    return ok


def _emails_sent_today(email: str) -> int:
    since = timezone.now() - timedelta(hours=24)
    return SupervisorInviteEvent.objects.filter(
        email=email,
        action__in=[SupervisorInviteEvent.Action.CREATED, SupervisorInviteEvent.Action.RESENT],
        created_at__gte=since,
    ).count()


def _check_email_cap(email: str) -> None:
    if _emails_sent_today(email) >= email_daily_cap():
        raise InviteError(
            "This supervisor has already received several invitations today. Please try again tomorrow.",
            "email_daily_cap",
            status=429,
        )


@dataclass
class InviteInput:
    email: str
    supervisor_name: str = ""
    department_id: Optional[int] = None
    message: str = ""


def validate_invite(student, data: InviteInput) -> tuple[str, Optional[Department]]:
    if getattr(student, "user_type", None) not in INVITER_USER_TYPES:
        raise InviteError("Only students can invite a supervisor.", "not_allowed", status=403)

    from iic_booking.users.identity.flags import student_lifecycle_enabled

    if student_lifecycle_enabled():
        from iic_booking.users.identity.service import UserEligibilityService

        ok, code = UserEligibilityService.can_create_affiliation(student)
        if not ok:
            raise InviteError("Disabled students cannot invite a supervisor.", code or "student_disabled", status=403)

    email = normalize_email(data.email)
    if not email:
        raise InviteError("Enter your supervisor's email address.", "email_required")
    try:
        validate_email(email)
    except ValidationError:
        raise InviteError("Enter a valid email address.", "email_invalid")
    if not is_allowed_domain(email):
        domains = ", ".join("@" + d for d in allowed_email_domains())
        raise InviteError(
            f"Use your supervisor's institute email address (ending in {domains}).",
            "email_domain",
        )
    if email == normalize_email(student.email):
        raise InviteError("You cannot invite yourself. Enter your supervisor's email address.", "self_invite")

    existing = User.objects.filter(email__iexact=email).select_related("department").first()
    if existing is not None:
        if existing.user_type != UserType.FACULTY:
            raise InviteError(
                "This email belongs to a portal account that is not a faculty member. "
                "Only faculty supervisors can be invited.",
                "not_faculty",
            )
        if existing.is_active:
            raise InviteError(
                f"{existing.name or existing.email} is already on the portal. "
                "Select them in the search above and send a link request instead.",
                "faculty_on_portal",
                status=409,
            )

    if len(data.message or "") > MAX_MESSAGE_LENGTH:
        raise InviteError(f"Keep the message under {MAX_MESSAGE_LENGTH} characters.", "message_too_long")
    if len(data.supervisor_name or "") > MAX_NAME_LENGTH:
        raise InviteError("The name is too long.", "name_too_long")

    department = None
    if data.department_id not in (None, ""):
        try:
            department = Department.objects.get(pk=int(data.department_id), department_type=DepartmentType.INTERNAL)
        except (Department.DoesNotExist, TypeError, ValueError):
            raise InviteError("Choose a department from the list.", "department_invalid")
    return email, department


def create_invite(student, data: InviteInput) -> SupervisorInvite:
    try:
        email, department = validate_invite(student, data)
    except InviteError as err:
        audit(SupervisorInviteEvent.Action.REFUSED, actor=student, email=normalize_email(data.email)[:254], code=err.code)
        raise

    mine = SupervisorInvite.objects.filter(student=student)
    expire_stale(mine)
    if mine.filter(status=SupervisorInviteStatus.PENDING, email=email).exists():
        raise InviteError(
            "You have already invited this email. Use Resend on your pending invitation instead.",
            "duplicate_invite",
        )
    if mine.filter(status=SupervisorInviteStatus.PENDING).count() >= MAX_ACTIVE_INVITES_PER_STUDENT:
        raise InviteError(
            f"You can have at most {MAX_ACTIVE_INVITES_PER_STUDENT} pending invitations. "
            "Cancel one before sending another.",
            "too_many_active",
            status=429,
        )
    _check_email_cap(email)

    token, digest = _new_token()
    now = timezone.now()
    with transaction.atomic():
        invite = SupervisorInvite.objects.create(
            student=student,
            email=email,
            supervisor_name=(data.supervisor_name or "").strip(),
            department=department,
            message=(data.message or "").strip(),
            token_hash=digest,
            expires_at=now + timedelta(days=INVITE_VALID_DAYS),
            last_sent_at=now,
            send_count=1,
        )
        audit(SupervisorInviteEvent.Action.CREATED, invite=invite, actor=student)
    send_invite_email(invite, token)
    return invite


def _owned_pending(student, invite_id: int) -> SupervisorInvite:
    invite = SupervisorInvite.objects.filter(pk=invite_id, student=student).first()
    if invite is None:
        raise InviteError("Invitation not found.", "not_found", status=404)
    expire_stale(SupervisorInvite.objects.filter(pk=invite.pk))
    invite.refresh_from_db()
    if invite.status != SupervisorInviteStatus.PENDING:
        raise InviteError(f"This invitation is {invite.get_status_display().lower()}.", "not_pending")
    return invite


def resend_invite(student, invite_id: int) -> SupervisorInvite:
    invite = _owned_pending(student, invite_id)
    now = timezone.now()
    if invite.last_sent_at and now - invite.last_sent_at < RESEND_COOLDOWN:
        next_at = timezone.localtime(invite.last_sent_at + RESEND_COOLDOWN).strftime("%d %b %Y, %H:%M")
        raise InviteError(
            f"You can resend this invitation once every 24 hours. Try again after {next_at}.",
            "resend_cooldown",
            status=429,
        )
    _check_email_cap(invite.email)
    token, digest = _new_token()
    invite.token_hash = digest
    invite.last_sent_at = now
    invite.send_count = (invite.send_count or 0) + 1
    invite.expires_at = now + timedelta(days=INVITE_VALID_DAYS)
    invite.save(update_fields=["token_hash", "last_sent_at", "send_count", "expires_at"])
    audit(SupervisorInviteEvent.Action.RESENT, invite=invite, actor=student)
    send_invite_email(invite, token)
    return invite


def cancel_invite(student, invite_id: int) -> SupervisorInvite:
    invite = _owned_pending(student, invite_id)
    invite.status = SupervisorInviteStatus.CANCELLED
    invite.cancelled_at = timezone.now()
    invite.save(update_fields=["status", "cancelled_at"])
    audit(SupervisorInviteEvent.Action.CANCELLED, invite=invite, actor=student)
    return invite


def can_resend_at(invite: SupervisorInvite):
    if invite.status != SupervisorInviteStatus.PENDING or not invite.last_sent_at:
        return None
    return invite.last_sent_at + RESEND_COOLDOWN


def serialize_invite(invite: SupervisorInvite) -> dict:
    now = timezone.now()
    resend_at = can_resend_at(invite)
    return {
        "id": invite.id,
        "email": invite.email,
        "supervisor_name": invite.supervisor_name,
        "department_id": invite.department_id,
        "department_name": invite.department.name if invite.department_id else "",
        "message": invite.message,
        "status": invite.status,
        "status_display": invite.get_status_display(),
        "created_at": invite.created_at.isoformat() if invite.created_at else None,
        "expires_at": invite.expires_at.isoformat() if invite.expires_at else None,
        "last_sent_at": invite.last_sent_at.isoformat() if invite.last_sent_at else None,
        "accepted_at": invite.accepted_at.isoformat() if invite.accepted_at else None,
        "join_request_id": invite.join_request_id,
        "can_resend": bool(resend_at and resend_at <= now),
        "can_resend_at": resend_at.isoformat() if resend_at else None,
        "can_cancel": invite.status == SupervisorInviteStatus.PENDING,
    }


def _notify_student_accepted(invite: SupervisorInvite, faculty) -> None:
    from iic_booking.communication.utils import get_frontend_absolute_url

    student = invite.student
    faculty_label = faculty.get_display_name() if hasattr(faculty, "get_display_name") else (faculty.name or faculty.email)
    try:
        from iic_booking.communication.in_app import notify_in_app

        notify_in_app(
            [student],
            title="Your request is with your supervisor",
            message=f"{faculty_label} has signed in. Your wallet link request is waiting for their approval.",
            link="/wallet",
            event="supervisor_invite.accepted",
            created_by=faculty,
        )
    except Exception:
        logger.exception("supervisor invite in-app notice failed invite=%s", invite.pk)
    try:
        template = _ensure_template(ACCEPTED_EMAIL_TEMPLATE)
        if template is None:
            return
        context = {
            "student_name": (student.name or "").strip() or student.email,
            "faculty_name": faculty_label,
            "faculty_email": faculty.email,
            "link": get_frontend_absolute_url("/wallet"),
        }
        rendered = _render(template, context, ("student_name", "faculty_name", "faculty_email"))
        _deliver(
            to_email=student.email,
            rendered=rendered,
            template=template,
            metadata={"module": "supervisor_invite", "supervisor_invite_id": invite.pk, "event": "accepted"},
            created_by=faculty,
            recipient=student,
        )
    except Exception:
        logger.exception("supervisor invite accepted email failed invite=%s", invite.pk)


def _create_join_request(student, faculty, message: str) -> WalletJoinRequest:
    from .models import Wallet

    wallet, _ = Wallet.objects.get_or_create(user=faculty)
    join_request = WalletJoinRequest.objects.create(
        student=student,
        faculty=faculty,
        wallet=wallet,
        message=message,
        status=WalletJoinRequestStatus.PENDING,
    )
    try:
        from iic_booking.users.identity.service import UserIdentityService
        from iic_booking.users.models.channel_i_identity import AffiliationKind, UserAffiliation

        view = UserIdentityService.view(student)
        UserAffiliation.objects.create(
            user=student,
            kind=AffiliationKind.FACULTY,
            related_user=faculty,
            department_id=view.internal_department_id,
            wallet_join_request=join_request,
            active=False,
        )
    except Exception:
        pass
    return join_request


def convert_invites_for_faculty(faculty) -> list[SupervisorInvite]:
    """Turn pending invites for this faculty member's email into pending wallet link requests (never approved)."""
    if faculty is None or getattr(faculty, "user_type", None) != UserType.FACULTY or not faculty.is_active:
        return []
    email = normalize_email(faculty.email)
    if not email:
        return []
    pending = SupervisorInvite.objects.filter(email=email, status=SupervisorInviteStatus.PENDING)
    if not pending.exists():
        return []
    expire_stale(pending)

    converted: list[SupervisorInvite] = []
    for invite_id in list(
        SupervisorInvite.objects.filter(email=email, status=SupervisorInviteStatus.PENDING).values_list("pk", flat=True)
    ):
        with transaction.atomic():
            invite = (
                SupervisorInvite.objects.select_for_update()
                .select_related("student", "student__department")
                .filter(pk=invite_id, status=SupervisorInviteStatus.PENDING)
                .first()
            )
            if invite is None:
                continue
            student = invite.student
            if student.pk == faculty.pk:
                continue
            existing = (
                WalletJoinRequest.objects.filter(
                    student=student,
                    faculty=faculty,
                    status__in=[WalletJoinRequestStatus.PENDING, WalletJoinRequestStatus.APPROVED],
                )
                .order_by("-pk")
                .first()
            )
            join_request = existing or _create_join_request(student, faculty, invite.message)
            invite.status = SupervisorInviteStatus.ACCEPTED
            invite.accepted_at = timezone.now()
            invite.accepted_by = faculty
            invite.join_request = join_request
            invite.save(update_fields=["status", "accepted_at", "accepted_by", "join_request"])
            audit(
                SupervisorInviteEvent.Action.ACCEPTED,
                invite=invite,
                actor=faculty,
                join_request_id=join_request.pk,
                reused_existing_request=existing is not None,
            )
        if existing is None:
            _notify_student_accepted(invite, faculty)
        converted.append(invite)
    return converted


def convert_invites_on_login(user) -> None:
    try:
        convert_invites_for_faculty(user)
    except Exception:
        logger.exception("supervisor invite conversion failed user_id=%s (login continues)", getattr(user, "id", None))


def resolve_token_for_faculty(faculty, token: str) -> dict:
    """After sign-in: point the faculty member at the request created from this invite (no auth by token)."""
    convert_invites_on_login(faculty)
    invite = (
        SupervisorInvite.objects.select_related("student", "join_request")
        .filter(token_hash=hash_token(token or ""))
        .first()
    )
    if invite is None or normalize_email(faculty.email) != invite.email:
        return {"matched": False}
    student = invite.student
    return {
        "matched": True,
        "status": invite.status,
        "join_request_id": invite.join_request_id,
        "join_request_status": invite.join_request.status if invite.join_request_id else None,
        "student_name": (student.name or "").strip() or student.email,
    }

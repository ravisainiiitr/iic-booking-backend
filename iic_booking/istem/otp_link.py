"""Linking a user who is already registered on I-STEM, proved by an OTP sent to the I-STEM email.

Create User answers "already registered" with the existing user (``service._await_otp`` then sets the link to
``AWAITING_OTP``). The user asks for an OTP on the portal; it goes to the email registered on I-STEM, shown to them
only masked, and entering it links the account at once. No staff step and no I-STEM call.

Rules that protect the record:
- Proof by email: the account is linked only after the code sent to the I-STEM email is entered.
- Limited OTP: a code expires after ``OTP_TTL`` and allows ``MAX_ATTEMPTS`` tries; sends are spaced and capped per
  hour; ``PAUSE_AFTER_EXHAUSTED`` codes used up within ``PAUSE_WINDOW`` pause linking for ``PAUSE``.
- One to one: an I-STEM user already linked to another portal account is refused (also enforced by the database).
- Audit trail: every send, wrong code and link is an ``IstemLinkOtp`` row plus an ``IstemSyncEvent``.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from . import conf
from .choices import AttemptOutcome, ErrorCategory, EvidenceType, LinkOtpState, Operation, UserLinkState
from .errors import IstemConfigError, IstemError
from .models import IstemLinkOtp, IstemUserLink
from .outbox import record_event
from .redaction import mask
from .service import _link_user, linked_elsewhere

logger = logging.getLogger("iic_booking.istem")

OTP_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5
RESEND_INTERVAL = timedelta(seconds=60)
MAX_SENDS_PER_HOUR = 5
PAUSE_AFTER_EXHAUSTED = 3
PAUSE_WINDOW = timedelta(hours=24)
PAUSE = timedelta(hours=24)
_HASH_SALT = "iic_booking.istem.otp_link"


class OtpLinkError(Exception):
    """A refused OTP request; ``code`` is stable for the frontend, ``message`` is shown to the user."""

    def __init__(self, code: str, message: str, *, http_status: int = 400, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.extra = extra


def _hash(link: IstemUserLink, public_user_id: str, code: str) -> str:
    return salted_hmac(_HASH_SALT, f"{link.pk}:{public_user_id}:{code}").hexdigest()


def _pending_otp(link: IstemUserLink) -> IstemLinkOtp | None:
    return link.otps.filter(state=LinkOtpState.PENDING).order_by("-sent_at").first()


def link_status(user) -> dict:
    """What the portal shows the user about their I-STEM link."""
    link = IstemUserLink.objects.filter(user=user).first()
    if link is None:
        return {"state": None, "otp_required": False}
    out = {"state": link.state, "otp_required": False}
    if link.state == UserLinkState.LINKED:
        out["public_user_id"] = link.public_user_id
        out["istem_profile"] = link.istem_profile or {}  # the user's own, verified record (for prefill)
    if link.state == UserLinkState.AWAITING_OTP and conf.otp_linking_enabled():
        out.update(otp_required=True, email_masked=mask(link.candidate_email), paused_until=None)
        if link.otp_paused_until and link.otp_paused_until > timezone.now():
            out["paused_until"] = link.otp_paused_until
        otp = _pending_otp(link)
        if otp is not None and otp.expires_at > timezone.now():
            out["otp"] = {"expires_at": otp.expires_at, "attempts_left": MAX_ATTEMPTS - otp.attempts}
    return out


MAX_LOOKUPS_PER_HOUR = 10


def find_existing_account(user, *, email: str = "", mobile: str = "", client_factory=None) -> dict:
    """An existing I-STEM user registers on the portal by naming the email or mobile number they registered with
    I-STEM: Public User Lookup finds the account, and the OTP flow above proves it. The profile I-STEM returns is
    kept on the link, shown to the user only once the OTP is verified, and used to fill their portal profile.

    [STAGING] Public User Lookup is not deployed on staging (404) and its rows carry no email yet; both were requested
    from I-STEM. Until then this answers ``lookup_unavailable`` / ``istem_email_missing``.
    """
    from .service import _finish_attempt, _outcome_for, _start_attempt, default_client

    email = (email or "").strip().lower()
    mobile = "".join(ch for ch in (mobile or "") if ch.isdigit())[-10:]
    if not conf.otp_linking_enabled():
        raise OtpLinkError("disabled", "Linking an existing I-STEM account by OTP is not available.", http_status=403)
    if not email and len(mobile) != 10:
        raise OtpLinkError("invalid_lookup", "Enter the email or the 10-digit mobile number registered with I-STEM.")
    with transaction.atomic():
        link, _ = IstemUserLink.objects.select_for_update().get_or_create(user=user)
        if link.state == UserLinkState.LINKED:
            raise OtpLinkError("already_linked", "Your account is already linked to I-STEM.", http_status=409)
        if link.attempts.filter(operation=Operation.LOOKUP_USER, started_at__gte=timezone.now() - timedelta(hours=1)).count() >= MAX_LOOKUPS_PER_HOUR:
            raise OtpLinkError("rate_limited", "Too many look-ups. Please try again in an hour.", http_status=429)
        query = {"user_email": email} if email else {"user_contactno": mobile}
        attempt = _start_attempt(None, Operation.LOOKUP_USER, user_link=link, payload=query, endpoint="public-user-lookup")
    try:
        rows = (client_factory or default_client)().lookup_public_user(**query)
    except IstemError as exc:
        with transaction.atomic():
            _finish_attempt(attempt, _outcome_for(exc), error=exc)
        if exc.category == ErrorCategory.NOT_FOUND:
            raise OtpLinkError("lookup_unavailable", "Looking up I-STEM accounts is not available yet.", http_status=503)
        raise OtpLinkError("lookup_failed", "I-STEM could not be reached. Please try again later.", http_status=502)
    except IstemConfigError:
        raise OtpLinkError("lookup_unavailable", "Looking up I-STEM accounts is not available yet.", http_status=503)
    ids = {row["user_id"] for row in rows}
    istem_email = rows[0]["profile"].get("email") or "" if len(ids) == 1 else ""
    with transaction.atomic():
        _finish_attempt(attempt, AttemptOutcome.SUCCESS, response={"user_ids": sorted(ids)})
    if not ids:
        raise OtpLinkError("not_found", "No I-STEM account was found for these details.", http_status=404)
    if len(ids) > 1:
        raise OtpLinkError("ambiguous", "More than one I-STEM account matches; please use your email instead.", http_status=409)
    if not istem_email:
        raise OtpLinkError("istem_email_missing", "I-STEM did not return the account's email, so no OTP can be sent.",
                           http_status=503)
    row = rows[0]
    with transaction.atomic():
        link = IstemUserLink.objects.select_for_update().get(pk=link.pk)
        if link.state == UserLinkState.LINKED:
            raise OtpLinkError("already_linked", "Your account is already linked to I-STEM.", http_status=409)
        previous = link.state
        if linked_elsewhere(link, row["user_id"]):
            link.candidate_public_user_id = row["user_id"]
            _refuse_linked_elsewhere(link)
            refused = True
        else:
            refused = False
            link.state = UserLinkState.AWAITING_OTP
            link.candidate_public_user_id = row["user_id"]
            link.candidate_email = istem_email
            link.candidate_profile = row["profile"]
            link.review_reason = f"Existing I-STEM account found by look-up; waiting for the OTP sent to {mask(istem_email)}."
            link.save()
            record_event(user_link=link, from_state=previous, to_state=link.state, attempt=attempt,
                         reason="existing I-STEM user found by look-up; waiting for the user's email OTP")
    if refused:
        raise OtpLinkError("linked_elsewhere", "This I-STEM account is already linked to another portal account. "
                           "The IIC office will review it.", http_status=409)
    return {"email_masked": mask(istem_email)}


# Local billing-profile field <- I-STEM profile key; filled only where the portal profile is still blank.
PROFILE_PREFILL = {
    "billing_name": "billing_name",
    "gstin": "gstin",
    "billing_address_line1": "address",
    "billing_city": "city",
    "billing_state": "state",
    "billing_pincode": "pin",
    "billing_country": "country",
}


def apply_istem_profile(user, profile: dict) -> list[str]:
    """Fill the blank fields of the user's billing profile from the verified I-STEM profile; returns the fields set."""
    from iic_booking.users.models.billing import ExternalBillingProfile

    billing, _ = ExternalBillingProfile.objects.get_or_create(user=user)
    filled = []
    for field, key in PROFILE_PREFILL.items():
        value = (profile.get(key) or "").strip()
        current = (getattr(billing, field, "") or "").strip()
        if value and not current:
            setattr(billing, field, value[: billing._meta.get_field(field).max_length])
            filled.append(field)
    if filled:
        billing.save(update_fields=filled)
    return filled


def _locked_awaiting_link(user) -> IstemUserLink:
    if not conf.otp_linking_enabled():
        raise OtpLinkError("disabled", "Linking an existing I-STEM account by OTP is not available.", http_status=403)
    link = IstemUserLink.objects.select_for_update().filter(user=user).first()
    if link is None or link.state != UserLinkState.AWAITING_OTP or not link.candidate_public_user_id or not link.candidate_email:
        raise OtpLinkError("not_awaiting_otp", "Your account is not waiting for an I-STEM OTP.", http_status=409)
    if link.otp_paused_until and link.otp_paused_until > timezone.now():
        raise OtpLinkError("paused", "Too many wrong codes. OTP linking is paused; please try again later.",
                           http_status=429, paused_until=link.otp_paused_until.isoformat())
    return link


def _refuse_linked_elsewhere(link: IstemUserLink, otp: IstemLinkOtp | None = None) -> None:
    """One to one: the candidate I-STEM user is already linked to another account; hand it to an administrator."""
    if otp is not None:
        otp.state = LinkOtpState.REFUSED
        otp.finished_at = timezone.now()
        otp.save(update_fields=["state", "finished_at"])
    previous = link.state
    link.state = UserLinkState.NEEDS_REVIEW
    link.review_reason = (f"I-STEM user {link.candidate_public_user_id} is already linked to another portal account; "
                          "OTP linking refused.")
    link.save(update_fields=["state", "review_reason", "updated_at"])
    record_event(user_link=link, from_state=previous, to_state=link.state, reason="OTP linking refused: I-STEM user linked to another account")


def _send_email(user, email: str, code: str) -> None:
    from iic_booking.users.test_accounts import redirect_email_for_user

    subject = "OTP to link your I-STEM account - IIC Booking Portal, IIT Roorkee"
    body = (f"Your one-time password to link your I-STEM account with the IIC Booking Portal is: {code}\n\n"
            f"It expires in {int(OTP_TTL.total_seconds() // 60)} minutes. Do not share it with anyone.\n\n"
            "If you did not ask to link your I-STEM account, ignore this email.")
    recipients, delivery_subject = redirect_email_for_user(user, original_email=email, subject=subject)
    send_mail(subject=delivery_subject or subject, message=body, from_email=settings.DEFAULT_FROM_EMAIL,
              recipient_list=recipients or [email], fail_silently=False)


def send_otp(user) -> dict:
    """Send a new OTP to the candidate's I-STEM email (any earlier unused code stops working)."""
    refused = False
    with transaction.atomic():
        link = _locked_awaiting_link(user)
        now = timezone.now()
        if linked_elsewhere(link, link.candidate_public_user_id):
            _refuse_linked_elsewhere(link)
            refused = True
        else:
            last = link.otps.order_by("-sent_at").first()
            if last is not None and now - last.sent_at < RESEND_INTERVAL:
                wait = int((RESEND_INTERVAL - (now - last.sent_at)).total_seconds()) + 1
                raise OtpLinkError("too_soon", f"Please wait {wait} seconds before asking for another OTP.",
                                   http_status=429, retry_after=wait)
            if link.otps.filter(sent_at__gte=now - timedelta(hours=1)).count() >= MAX_SENDS_PER_HOUR:
                raise OtpLinkError("rate_limited", "Too many OTPs requested. Please try again in an hour.", http_status=429)
            link.otps.filter(state=LinkOtpState.PENDING).update(state=LinkOtpState.SUPERSEDED, finished_at=now)
            code = f"{secrets.randbelow(10 ** 6):06d}"
            otp = IstemLinkOtp.objects.create(
                user_link=link, public_user_id=link.candidate_public_user_id, email_masked=mask(link.candidate_email),
                code_hash=_hash(link, link.candidate_public_user_id, code), sent_at=now, expires_at=now + OTP_TTL,
            )
            record_event(user_link=link, from_state=link.state, to_state=link.state,
                         reason=f"email OTP {otp.pk} sent to {otp.email_masked}")
            try:
                _send_email(user, link.candidate_email, code)
            except Exception:
                logger.exception("istem otp email failed user_link=%s", link.pk)
                # Rolls the OTP back: nothing was sent, so nothing is recorded as sent.
                raise OtpLinkError("send_failed", "The OTP email could not be sent. Please try again later.", http_status=502)
    if refused:
        raise OtpLinkError("linked_elsewhere", "This I-STEM account is already linked to another portal account. "
                           "The IIC office will review it.", http_status=409)
    return {"email_masked": otp.email_masked, "expires_at": otp.expires_at, "attempts_left": MAX_ATTEMPTS}


def verify_otp(user, code: str) -> IstemUserLink:
    """Check the code; a correct one links the account to the existing I-STEM user at once."""
    code = "".join((code or "").split())
    if not (code.isdigit() and len(code) == 6):
        raise OtpLinkError("invalid_code", "Enter the 6-digit OTP from the email.")
    error = None
    with transaction.atomic():
        link = _locked_awaiting_link(user)
        now = timezone.now()
        otp = link.otps.select_for_update().filter(state=LinkOtpState.PENDING).order_by("-sent_at").first()
        if otp is None:
            raise OtpLinkError("no_otp", "No OTP is waiting. Please ask for a new one.")
        if otp.expires_at <= now:
            otp.state = LinkOtpState.EXPIRED
            otp.finished_at = now
            otp.save(update_fields=["state", "finished_at"])
            error = OtpLinkError("expired", "This OTP has expired. Please ask for a new one.")
        elif not constant_time_compare(_hash(link, otp.public_user_id, code), otp.code_hash):
            otp.attempts += 1
            left = MAX_ATTEMPTS - otp.attempts
            reason = f"wrong email OTP {otp.pk} (attempt {otp.attempts} of {MAX_ATTEMPTS})"
            if left <= 0:
                otp.state = LinkOtpState.EXHAUSTED
                otp.finished_at = now
                exhausted = link.otps.filter(state=LinkOtpState.EXHAUSTED, finished_at__gte=now - PAUSE_WINDOW).exclude(pk=otp.pk).count() + 1
                if exhausted >= PAUSE_AFTER_EXHAUSTED:
                    link.otp_paused_until = now + PAUSE
                    link.save(update_fields=["otp_paused_until", "updated_at"])
                    reason += f"; OTP linking paused until {link.otp_paused_until:%Y-%m-%d %H:%M} UTC"
                error = OtpLinkError("attempts_exceeded", "Too many incorrect codes. Please ask for a new OTP.")
            else:
                error = OtpLinkError("wrong_code", f"Incorrect OTP. {left} attempt(s) left.", attempts_left=left)
            otp.save(update_fields=["attempts", "state", "finished_at"])
            record_event(user_link=link, from_state=link.state, to_state=link.state, reason=reason)
        elif linked_elsewhere(link, otp.public_user_id):
            _refuse_linked_elsewhere(link, otp)
            error = OtpLinkError("linked_elsewhere", "This I-STEM account is already linked to another portal account. "
                                 "The IIC office will review it.", http_status=409)
        else:
            otp.state = LinkOtpState.VERIFIED
            otp.attempts += 1
            otp.finished_at = now
            otp.save(update_fields=["state", "attempts", "finished_at"])
            profile = dict(link.candidate_profile or {})
            if profile:
                link.istem_profile, link.candidate_profile = profile, {}
            _link_user(link, otp.public_user_id, reason=f"existing I-STEM user linked by email OTP {otp.pk}",
                       evidence_type=EvidenceType.EMAIL_OTP, evidence_reference=f"OTP {otp.pk} to {otp.email_masked}")
            if profile and link.state == UserLinkState.LINKED:
                apply_istem_profile(user, profile)
    if error is not None:
        raise error
    return link

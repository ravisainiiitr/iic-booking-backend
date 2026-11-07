"""One-time codes on WhatsApp, always next to the email that carries them today, never instead of it.

Three uses:
- proving a WhatsApp number from the profile (``start_verification`` / ``confirm_verification``);
- sign-in codes, only to a proved number of a user who is opted in (``send_sign_in_code``);
- I-STEM codes, only when the person ticked the box for this request (``send_on_request``).

The code is never stored: the message row keeps no parameters, and it is sent at once with no retry, because a code that
arrives late is useless and the email has it anyway. Nothing here raises into the caller's sign-in or registration.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from . import conf
from .client import WhatsAppError, send_code_template
from .models import WhatsAppMessage, WhatsAppOptIn
from .service import OptInError, _record_consent, normalize_phone
from .template_catalogue import BY_EVENT
from .templates import is_approved

logger = logging.getLogger(__name__)

EVENT = "otp_code"
SENDS_PER_NUMBER = 5
SEND_WINDOW_SECONDS = 15 * 60
VERIFY_TTL = timedelta(minutes=10)
VERIFY_MAX_ATTEMPTS = 5
_SALT = "iic_booking.whatsapp.otp"
_VERIFY_KEY = "whatsapp-verify:{}"


class VerificationError(ValueError):
    def __init__(self, code: str, message: str, *, http_status: int = 400):
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status


def new_code() -> str:
    return f"{secrets.randbelow(10 ** 6):06d}"


def mask_number(phone: str) -> str:
    return f"number ending {phone[-2:]}" if phone else ""


def available() -> bool:
    """On, configured and the template approved at Meta. Off for everybody otherwise."""
    entry = BY_EVENT[EVENT]
    return conf.enabled() and is_approved(conf.template_name(EVENT), entry.language)


def _within_send_limit(phone: str) -> bool:
    """Protects the person holding the number, and the bill: at most SENDS_PER_NUMBER codes in the window."""
    key = "whatsapp-otp-sends:" + hashlib.sha256(phone.encode()).hexdigest()
    if cache.add(key, 1, timeout=SEND_WINDOW_SECONDS):
        return True
    try:
        return cache.incr(key) <= SENDS_PER_NUMBER
    except ValueError:
        cache.set(key, 1, timeout=SEND_WINDOW_SECONDS)
        return True


def _send(phone: str, code: str, *, purpose: str, user=None, session=None) -> bool:
    """One attempt. True only when Meta accepted it. Passes every gate of a normal message except the opt-in, which each
    caller settles for itself."""
    try:
        if not available() or not conf.may_send_to(phone) or not _within_send_limit(phone):
            return False
        entry = BY_EVENT[EVENT]
        template = conf.template_name(EVENT)
        status, wamid, error = WhatsAppMessage.Status.SENT, "", ""
        try:
            wamid = send_code_template(phone, template, code, language=entry.language, session=session)
        except WhatsAppError as exc:
            status, error = WhatsAppMessage.Status.FAILED, str(exc).replace(code, "[code]")[:2000]
            logger.warning("whatsapp %s code not sent: %s", purpose, error)
        WhatsAppMessage.objects.create(user=user, to_e164=phone, event=EVENT, template=template, params=[],
                                       event_key=f"otp:{purpose}:{secrets.token_hex(16)}"[:120], status=status,
                                       wamid=wamid, error=error)
        return status == WhatsAppMessage.Status.SENT
    except Exception as exc:  # noqa: BLE001 - the email has gone; this must never fail the sign-in or the registration
        logger.warning("whatsapp %s code failed: %s", purpose, type(exc).__name__)
        return False


def sign_in_number(user) -> str:
    """The number a sign-in code may go to: proved by this user, still the current number, and still opted in."""
    row = WhatsAppOptIn.objects.filter(user=user, opted_in=True).first()
    if row is None or not row.phone_e164 or row.phone_verified_e164 != row.phone_e164:
        return ""
    return row.phone_e164


def send_sign_in_code(user, code: str, *, session=None) -> str:
    """Returns the masked number when the code went out, else ""."""
    phone = sign_in_number(user)
    if phone and _send(phone, code, purpose="login", user=user, session=session):
        return mask_number(phone)
    return ""


def send_on_request(raw_phone: str, code: str, *, purpose: str, user=None, session=None) -> str:
    """The person asked for this code on WhatsApp just now, on a number another service holds for them. That request is
    the consent, kept in the consent history. Someone who replied STOP is not sent anything."""
    try:
        phone = normalize_phone(raw_phone)
    except OptInError:
        return ""
    if WhatsAppOptIn.objects.filter(phone_e164=phone, opted_in=False, opted_out_at__isnull=False).exists():
        return ""
    if not _send(phone, code, purpose=purpose, user=user, session=session):
        return ""
    _record_consent(user_id=getattr(user, "pk", None), phone=phone, action="opt_in", source="otp_request")
    return mask_number(phone)


def _hash(user_id: int, phone: str, code: str) -> str:
    return salted_hmac(_SALT, f"{user_id}:{phone}:{code}").hexdigest()


def start_verification(user, *, session=None) -> dict:
    """Send a code to the user's opted-in number, to prove that it is theirs."""
    row = WhatsAppOptIn.objects.filter(user=user, opted_in=True).first()
    if row is None or not row.phone_e164:
        raise VerificationError("not_opted_in", "Turn on WhatsApp notifications first.")
    if not available():
        raise VerificationError("unavailable", "WhatsApp codes are not available yet.", http_status=503)
    code = new_code()
    if not _send(row.phone_e164, code, purpose="verify", user=user, session=session):
        raise VerificationError("send_failed", "The code could not be sent on WhatsApp. Check the number, or try again later.",
                                http_status=502)
    cache.set(_VERIFY_KEY.format(user.pk), {"phone": row.phone_e164, "code_hash": _hash(user.pk, row.phone_e164, code), "attempts": 0},
              timeout=int(VERIFY_TTL.total_seconds()))
    return {"sent_to": mask_number(row.phone_e164), "expires_in": int(VERIFY_TTL.total_seconds())}


def confirm_verification(user, code: str) -> None:
    key = _VERIFY_KEY.format(user.pk)
    pending = cache.get(key)
    if not pending:
        raise VerificationError("expired", "This code has expired. Please ask for a new one.")
    row = WhatsAppOptIn.objects.filter(user=user, opted_in=True, phone_e164=pending["phone"]).first()
    if row is None:
        cache.delete(key)
        raise VerificationError("number_changed", "Your WhatsApp number has changed. Please ask for a new code.")
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if not constant_time_compare(pending["code_hash"], _hash(user.pk, pending["phone"], code)):
        pending["attempts"] += 1
        if pending["attempts"] >= VERIFY_MAX_ATTEMPTS:
            cache.delete(key)
            raise VerificationError("attempts_exceeded", "Too many incorrect codes. Please ask for a new one.", http_status=429)
        cache.set(key, pending, timeout=int(VERIFY_TTL.total_seconds()))
        raise VerificationError("wrong_code", f"Incorrect code. {VERIFY_MAX_ATTEMPTS - pending['attempts']} attempt(s) left.")
    cache.delete(key)
    row.phone_verified_e164, row.phone_verified_at = row.phone_e164, timezone.now()
    row.save(update_fields=["phone_verified_e164", "phone_verified_at", "updated_at"])

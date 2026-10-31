"""Registration of a user who already has an I-STEM account: before the portal account exists, the user gives the email
or the mobile number registered on I-STEM (31 Oct 2026 committee: either is enough; when both are given, one account
must carry both); Public User Lookup must find exactly one account, an OTP goes to the email registered on I-STEM,
and once it is entered the registration form is filled from the I-STEM profile. The signed link token returned
then links the new portal account to that I-STEM user when the registration is submitted (``link_new_user``).

There is no portal user yet, so the pending lookup lives in the cache (code hash, attempts, profile) under a random
token; nothing about the I-STEM account is shown before the OTP is verified. Available only while the I-STEM API and
OTP linking are switched on (``available``).
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.core.mail import send_mail
from django.utils.crypto import constant_time_compare, salted_hmac

from . import conf
from .errors import IstemConfigError, IstemError
from .redaction import mask

logger = logging.getLogger(__name__)

OTP_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5
LOOKUPS_PER_HOUR_PER_IP = 10
LOOKUPS_PER_HOUR_PER_EMAIL = 5
LINK_TOKEN_MAX_AGE = 60 * 60  # seconds the verified result may be used to submit the registration
_SALT = "iic_booking.istem.registration_lookup"
_KEY = "istem-reg:{}"


class RegistrationLookupError(Exception):
    def __init__(self, code: str, message: str, *, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def available() -> bool:
    return bool(conf.istem_enabled() and conf.otp_linking_enabled())


def _digits(mobile: str) -> str:
    return "".join(ch for ch in (mobile or "") if ch.isdigit())[-10:]


def _hash(token: str, code: str) -> str:
    return salted_hmac(_SALT, f"{token}:{code}").hexdigest()


def _count(key: str, limit: int) -> bool:
    """Count one use of ``key`` this hour; False when the limit is already reached."""
    added = cache.add(key, 1, timeout=3600)
    if added:
        return True
    try:
        n = cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=3600)
        return True
    return n <= limit


def _send_otp(email: str, code: str) -> None:
    subject = "OTP to register with your I-STEM account - IIC Booking Portal, IIT Roorkee"
    body = (f"Your one-time password to register on the IIC Booking Portal with your I-STEM account is: {code}\n\n"
            f"It expires in {int(OTP_TTL.total_seconds() // 60)} minutes. Do not share it with anyone.\n\n"
            "If you did not start a registration on the IIC Booking Portal, ignore this email.")
    send_mail(subject=subject, message=body, from_email=settings.DEFAULT_FROM_EMAIL, recipient_list=[email],
              fail_silently=False)


def start(email: str, mobile: str, *, client_ip: str = "", client_factory=None) -> dict:
    """Find the I-STEM account registered with ``email`` or ``mobile`` (both must match when both are given) and send
    an OTP to the email registered on I-STEM."""
    from .models import IstemUserLink
    from .service import default_client

    if not available():
        raise RegistrationLookupError("unavailable", "Registering with an I-STEM account is not available yet.", http_status=403)
    email = (email or "").strip().lower()
    mobile = _digits(mobile)
    if email and "@" not in email:
        raise RegistrationLookupError("invalid", "Enter a valid email address.")
    if mobile and len(mobile) != 10:
        raise RegistrationLookupError("invalid", "Enter the 10-digit mobile number registered on I-STEM.")
    if not email and not mobile:
        raise RegistrationLookupError("invalid", "Enter the email or the 10-digit mobile number registered on I-STEM.")
    if not _count(f"istem-reg-ip:{client_ip or 'unknown'}", LOOKUPS_PER_HOUR_PER_IP) or not _count(
        f"istem-reg-id:{email or mobile}", LOOKUPS_PER_HOUR_PER_EMAIL
    ):
        raise RegistrationLookupError("rate_limited", "Too many look-ups. Please try again in an hour.", http_status=429)
    try:
        client = (client_factory or default_client)()
        rows = client.lookup_public_user(user_email=email) if email else client.lookup_public_user(user_contactno=mobile)
    except (IstemError, IstemConfigError):
        logger.warning("I-STEM registration look-up failed", exc_info=True)
        raise RegistrationLookupError("lookup_failed", "I-STEM could not be reached. Please try again later, or "
                                      "register without it.", http_status=502)
    matches = [
        r for r in rows
        if (not email or (r["profile"].get("email") or "").lower() == email)
        and (not mobile or _digits(r["profile"].get("mobile", "")) == mobile)
        and "@" in (r["profile"].get("email") or "")
    ]
    if len(matches) != 1:
        given = " and ".join(x for x in ("email" if email else "", "mobile number" if mobile else "") if x)
        raise RegistrationLookupError("not_found", f"No I-STEM account was found with this {given}. "
                                      "Check it, or register without I-STEM.", http_status=404)
    row = matches[0]
    # The OTP always goes to the email registered on I-STEM (also when the user gave only the mobile number).
    email = (row["profile"].get("email") or "").strip().lower()
    if IstemUserLink.objects.filter(public_user_id=row["user_id"]).exclude(public_user_id="").exists():
        raise RegistrationLookupError("already_registered", "This I-STEM account is already linked to a portal "
                                      "account. Sign in instead, or contact the IIC office.", http_status=409)
    token = secrets.token_urlsafe(24)
    code = f"{secrets.randbelow(10 ** 6):06d}"
    cache.set(_KEY.format(token), {
        "public_user_id": row["user_id"], "email": email, "code_hash": _hash(token, code), "attempts": 0,
        "profile": row["profile"],
    }, timeout=int(OTP_TTL.total_seconds()))
    try:
        _send_otp(email, code)
    except Exception:
        cache.delete(_KEY.format(token))
        logger.exception("I-STEM registration OTP email failed")
        raise RegistrationLookupError("send_failed", "The OTP email could not be sent. Please try again later.",
                                      http_status=502)
    return {"token": token, "email_masked": mask(email), "expires_in": int(OTP_TTL.total_seconds())}


def verify(token: str, code: str) -> dict:
    """Check the OTP; returns the profile for the registration form and a signed link token."""
    key = _KEY.format(token or "")
    pending = cache.get(key) if token else None
    if not pending:
        raise RegistrationLookupError("expired", "This OTP has expired. Please look up your I-STEM account again.")
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != 6:
        raise RegistrationLookupError("invalid_code", "Enter the 6-digit OTP from the email.")
    if not constant_time_compare(pending["code_hash"], _hash(token, code)):
        pending["attempts"] += 1
        if pending["attempts"] >= MAX_ATTEMPTS:
            cache.delete(key)
            raise RegistrationLookupError("attempts_exceeded", "Too many incorrect codes. Please look up your I-STEM "
                                          "account again.", http_status=429)
        cache.set(key, pending, timeout=int(OTP_TTL.total_seconds()))
        raise RegistrationLookupError("wrong_code", f"Incorrect OTP. {MAX_ATTEMPTS - pending['attempts']} attempt(s) left.")
    cache.delete(key)
    profile = pending["profile"]
    link_token = signing.dumps(
        {"public_user_id": pending["public_user_id"], "email": pending["email"], "profile": profile}, salt=_SALT
    )
    return {"profile": profile, "link_token": link_token}


def link_new_user(user, link_token: str) -> bool:
    """After registration: link ``user`` to the verified I-STEM account and fill blank billing details.
    The token must be recent and issued for the email the account registered with; otherwise nothing happens."""
    from .choices import EvidenceType
    from .models import IstemUserLink
    from .otp_link import apply_istem_profile
    from .service import _link_user

    if not link_token:
        return False
    try:
        data = signing.loads(link_token, salt=_SALT, max_age=LINK_TOKEN_MAX_AGE)
    except signing.BadSignature:
        logger.warning("I-STEM registration link token rejected for user %s", getattr(user, "pk", None))
        return False
    if (data.get("email") or "").lower() != (user.email or "").strip().lower():
        return False
    link, _ = IstemUserLink.objects.get_or_create(user=user)
    link.istem_profile = data.get("profile") or {}
    link.save(update_fields=["istem_profile", "updated_at"])
    _link_user(link, str(data["public_user_id"]), reason="existing I-STEM user registered with email OTP",
               evidence_type=EvidenceType.EMAIL_OTP, evidence_reference=f"registration OTP to {mask(user.email)}")
    apply_istem_profile(user, link.istem_profile)
    return True

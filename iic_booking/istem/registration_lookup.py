"""Registration of a user who already has an I-STEM account: before the portal account exists, the user gives the email
or the mobile number registered on I-STEM (31 Oct 2026 committee: either is enough; when both are given, one account
must carry both); Public User Lookup must find exactly one account, an OTP goes to the email registered on I-STEM,
and once it is entered the registration form is filled from the I-STEM profile. The signed link token returned
then links the new portal account to that I-STEM user when the registration is submitted (``link_new_user``).

There is no portal user yet, so the pending lookup lives in the cache (code hash, attempts, profile) under a random
token; nothing about the I-STEM account is shown before the OTP is verified. Available only while the I-STEM API and
OTP linking are switched on (``available``), and while ``ISTEM_REGISTRATION_TEST_KEY`` is set only with that key;
accounts registered that way are test accounts.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.utils.crypto import constant_time_compare, salted_hmac

from . import conf
from .errors import IstemConfigError, IstemError
from .redaction import mask

logger = logging.getLogger(__name__)

OTP_TTL = timedelta(minutes=5)
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


def _test_key() -> str:
    return str(getattr(settings, "ISTEM_REGISTRATION_TEST_KEY", "") or "").strip()


def available(test_key: str = "") -> bool:
    if not (conf.istem_enabled() and conf.otp_linking_enabled()):
        return False
    key = _test_key()
    return not key or constant_time_compare(key, (test_key or "").strip())


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


TEST_EMAIL_DOMAIN = "@example.com"


def test_otp_inbox(email: str) -> str:
    """Where the OTP of a made-up I-STEM *staging* user goes (``ISTEM_TEST_OTP_INBOX``), or "" to send it as usual.
    Only for @example.com addresses (a domain that receives no mail) and only while connected to I-STEM staging."""
    inbox = str(getattr(settings, "ISTEM_TEST_OTP_INBOX", "") or "").strip()
    if not inbox or "@" not in inbox or not (email or "").lower().endswith(TEST_EMAIL_DOMAIN):
        return ""
    return inbox if "staging" in conf.base_url().lower() else ""


def _send_otp(email: str, code: str, *, name: str = "") -> None:
    from iic_booking.communication.utils import get_frontend_absolute_url

    from .otp_email import PORTAL_NAME, send_otp_email

    minutes = int(OTP_TTL.total_seconds() // 60)
    recipient, wrap = email, None
    redirect = test_otp_inbox(email)
    if redirect:
        recipient = redirect
        wrap = lambda subject: f"[I-STEM staging test] {subject} (for {email})"  # noqa: E731
        logger.info("I-STEM registration OTP for a test address sent to the test inbox")
    send_otp_email(
        "istem_registration_otp_email", recipients=[recipient], subject_wrap=wrap,
        context={"name": name or "there", "otp": code, "minutes": minutes, "masked_email": mask(email), "link": get_frontend_absolute_url("/auth")},
        fallback_subject="Your OTP to register with your I-STEM account",
        fallback_body=(f"Your one-time password to register on the {PORTAL_NAME} with your I-STEM account is: {code}\n\n"
                       f"It expires in {minutes} minutes. Do not share it with anyone.\n\n"
                       f"If you did not start a registration on the {PORTAL_NAME}, ignore this email."),
    )


def start(email: str, mobile: str, *, client_ip: str = "", test_key: str = "", client_factory=None) -> dict:
    """Find the I-STEM account registered with ``email`` or ``mobile`` (both must match when both are given) and send
    an OTP to the email registered on I-STEM, and the same OTP on WhatsApp to the mobile on I-STEM when there is one."""
    from .models import IstemUserLink
    from .service import default_client

    if not available(test_key):
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
    pending = {
        "public_user_id": row["user_id"], "email": email, "code_hash": _hash(token, code), "attempts": 0,
        "profile": row["profile"], "test_account": bool(_test_key()),
    }
    cache.set(_KEY.format(token), pending, timeout=int(OTP_TTL.total_seconds()))
    try:
        profile = row["profile"]
        _send_otp(email, code, name=" ".join(str(profile.get(k) or "").strip() for k in ("first_name", "last_name")).strip() or str(profile.get("name") or "").strip())
    except Exception:
        cache.delete(_KEY.format(token))
        logger.exception("I-STEM registration OTP email failed")
        raise RegistrationLookupError("send_failed", "The OTP email could not be sent. Please try again later.",
                                      http_status=502)
    on_whatsapp = ""
    mobile_on_istem = str(row["profile"].get("mobile") or "")
    if mobile_on_istem and conf.whatsapp_otp_enabled():
        from iic_booking.whatsapp.otp import send_on_request

        on_whatsapp = send_on_request(mobile_on_istem, code, purpose="istem_register")
        if on_whatsapp:
            pending["whatsapp_sent"] = True
            cache.set(_KEY.format(token), pending, timeout=int(OTP_TTL.total_seconds()))
    return {"token": token, "email_masked": mask(email), "whatsapp_masked": on_whatsapp, "expires_in": int(OTP_TTL.total_seconds())}


def verify(token: str, code: str) -> dict:
    """Check the OTP; returns the profile for the registration form and a signed link token."""
    key = _KEY.format(token or "")
    pending = cache.get(key) if token else None
    if not pending:
        raise RegistrationLookupError("expired", "This OTP has expired. Please look up your I-STEM account again.")
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != 6:
        raise RegistrationLookupError("invalid_code", "Enter the 6-digit OTP.")
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
        {"public_user_id": pending["public_user_id"], "email": pending["email"], "profile": profile,
         "test_account": bool(pending.get("test_account")), "channel": "email_or_whatsapp" if pending.get("whatsapp_sent") else "email"}, salt=_SALT
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
    if data.get("channel") == "email_or_whatsapp":
        mobile = str((data.get("profile") or {}).get("mobile") or "")
        _link_user(link, str(data["public_user_id"]), reason="existing I-STEM user registered with an OTP sent to email and WhatsApp",
                   evidence_type=EvidenceType.EMAIL_OR_WHATSAPP_OTP,
                   evidence_reference=f"registration OTP to {mask(user.email)} and on WhatsApp to number ending {mobile[-2:]}")
    else:
        _link_user(link, str(data["public_user_id"]), reason="existing I-STEM user registered with email OTP",
                   evidence_type=EvidenceType.EMAIL_OTP, evidence_reference=f"registration OTP to {mask(user.email)}")
    apply_istem_profile(user, link.istem_profile)
    if data.get("test_account") and not user.is_test_account:
        user.is_test_account = True  # registered through the I-STEM test link
        user.save(update_fields=["is_test_account"])
    return True

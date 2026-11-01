"""Registering with a verified I-STEM account in one step (1 Nov 2026).

After the email OTP to the address registered on I-STEM is verified (``registration_lookup.verify``), the portal account
is created at once, without a password and without the registration form: name, email, mobile and gender come from
I-STEM, and the organisation is matched to the external organisation list (same category, same State/UT, same name).
The account is approved (I-STEM already approved the person and the OTP proved the email), linked to the I-STEM user,
and signed in. Whatever the portal still needs (organisation if it did not match, programme validity, gender, mobile)
and a password for the next sign-in are asked one by one on the dashboard (``users.onboarding``); until a password is
set the user signs in with an email OTP.
"""

from __future__ import annotations

import logging
import re

from django.core import signing
from django.db import transaction

from .redaction import mask

logger = logging.getLogger(__name__)

# Registration user type -> external organisation list (same mapping as the registration form).
SUBCATEGORY_BY_USER_TYPE = {
    "external": "educational_institute",
    "RND": "govt_rnd",
    "Industry": "industries",
    "external_startup_msme": "external_startup_msme",
}
GENDERS = {"male": "male", "female": "female", "other": "other"}


class DirectSignupError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status


def _norm(text: str) -> str:
    text = (text or "").lower().replace("&", " and ")
    return " ".join(re.findall(r"[a-z0-9]+", text))


def match_state(name: str) -> str | None:
    """I-STEM's state name ("Uttarakhand") -> the portal's State/UT code ("uttarakhand"), or None."""
    from iic_booking.users.models.department import IndianState

    wanted = _norm(name)
    if not wanted:
        return None
    for code, label in IndianState.get_choices():
        if wanted in (_norm(str(label)), _norm(code)):
            return code
    return None


def match_department(organisation: str, state_code: str | None, user_type: str):
    """The external organisation with this name (ignoring case and punctuation) in the same category. Names are unique;
    one recorded in another State/UT than I-STEM's is not taken. None when there is no match."""
    from iic_booking.users.models import Department
    from iic_booking.users.models.department import DepartmentType

    wanted = _norm(organisation)
    subcategory = SUBCATEGORY_BY_USER_TYPE.get(user_type)
    if not wanted or not subcategory:
        return None
    qs = Department.objects.filter(department_type=DepartmentType.EXTERNAL, external_subcategory=subcategory)
    hits = [d for d in qs.only("id", "name", "state") if _norm(d.name) == wanted]
    if len(hits) != 1 or (state_code and hits[0].state and hits[0].state != state_code):
        return None
    return hits[0]


def _mobile(profile: dict) -> str | None:
    digits = "".join(ch for ch in str(profile.get("mobile") or "") if ch.isdigit())[-10:]
    return digits if len(digits) == 10 else None


def create_account(link_token: str, user_type: str):
    """Create, approve and link the portal account for a verified I-STEM user. Returns the user."""
    from iic_booking.users.models import User
    from iic_booking.users.models.user_type import UserType

    from .registration_lookup import LINK_TOKEN_MAX_AGE, _SALT, link_new_user

    try:
        data = signing.loads(link_token or "", salt=_SALT, max_age=LINK_TOKEN_MAX_AGE)
    except signing.BadSignature:
        raise DirectSignupError("expired", "This I-STEM verification has expired. Please look up your account again.")
    if user_type not in SUBCATEGORY_BY_USER_TYPE and user_type not in UserType.get_external_user_codes():
        raise DirectSignupError("invalid_user_type", "Choose the kind of organisation you belong to.")
    email = (data.get("email") or "").strip().lower()
    profile = data.get("profile") or {}
    if not email:
        raise DirectSignupError("invalid", "I-STEM did not return an email address for this account.")
    if User.objects.filter(email__iexact=email).exists():
        raise DirectSignupError("exists", "An account with this email already exists. Sign in instead.", 409)
    name = " ".join(p for p in (str(profile.get("first_name") or "").strip(), str(profile.get("last_name") or "").strip()) if p)
    state = match_state(str(profile.get("state") or ""))
    department = match_department(str(profile.get("organisation") or ""), state, user_type)
    with transaction.atomic():
        user = User.objects.create_user(
            email=email,
            password=None,  # unusable: the user signs in with an email OTP until they set one
            name=name or email.split("@")[0],
            gender=GENDERS.get(str(profile.get("gender") or "").strip().lower()),
            user_type=user_type,
            phone_number=_mobile(profile),
            department=department,
            is_active=True,
            email_verified=True,
            admin_approved=True,
        )
        if not link_new_user(user, link_token):
            raise DirectSignupError("expired", "This I-STEM verification has expired. Please look up your account again.")
    logger.info("Account created directly from I-STEM for %s (organisation matched: %s)", mask(email), bool(department))
    return user

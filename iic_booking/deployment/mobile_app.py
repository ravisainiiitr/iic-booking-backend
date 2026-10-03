"""
IIC Booking mobile app: who may use it, and the published Android builds.

The audience is a list of user type codes kept in ``MobileAppSettings`` (Main Administrator,
Admin Settings). It only limits signing in *through the app* (requests that say ``client=iic_app``,
app device-session enrolment and refresh); the website is never affected.
"""

from __future__ import annotations

from django.core.cache import cache

from iic_booking.users.models.user_type import UserType

APP_CLIENT = "iic_app"
AUDIENCE_CODE = "APP_AUDIENCE"
PORTAL_URL = "https://equip.iitr.ac.in"

_CACHE_KEY = "mobile_app:audience:v1"
_CACHE_SECONDS = 60

# Types a Main Administrator can pick. Staff roles that do day-to-day lab work come first.
SELECTABLE_USER_TYPES: list[str] = [
    UserType.MANAGER,
    UserType.OPERATOR,
    UserType.ADMIN,
    UserType.DEPT_ADMIN,
    UserType.FINANCE,
    UserType.EXTERNAL_RELATIONS,
    UserType.FACULTY,
    UserType.STUDENT,
]

_ROLE_PHRASES = {
    UserType.MANAGER: "Officers In Charge",
    UserType.OPERATOR: "Lab Operators",
    UserType.ADMIN: "Administrators",
    UserType.DEPT_ADMIN: "Department Administrators",
    UserType.FINANCE: "Accounts In Charge",
    UserType.EXTERNAL_RELATIONS: "External Relations Administrators",
    UserType.FACULTY: "IITR Faculty",
    UserType.STUDENT: "IITR Students",
}


def selectable_choices() -> list[dict]:
    labels = dict(UserType.get_choices())
    return [{"code": code, "name": str(labels.get(code, code))} for code in SELECTABLE_USER_TYPES]


def clean_audience(raw) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        raise ValueError("audience_user_types must be a list of user type codes.")
    allowed = set(SELECTABLE_USER_TYPES)
    out: list[str] = []
    for item in raw:
        code = str(item or "").strip()
        if code not in allowed:
            raise ValueError(f"Unknown or unsupported user type: {code or '(blank)'}")
        if code not in out:
            out.append(code)
    if not out:
        raise ValueError("Choose at least one user type.")
    return out


def audience_user_types() -> list[str]:
    cached = cache.get(_CACHE_KEY)
    if isinstance(cached, list):
        return cached
    from iic_booking.deployment.models import MobileAppSettings, default_mobile_app_audience

    try:
        types = list(MobileAppSettings.get_singleton().audience_user_types or [])
    except Exception:
        types = default_mobile_app_audience()
    cache.set(_CACHE_KEY, types, _CACHE_SECONDS)
    return types


def invalidate_audience_cache() -> None:
    cache.delete(_CACHE_KEY)


def user_in_app_audience(user) -> bool:
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    return getattr(user, "user_type", None) in set(audience_user_types())


def audience_message(types: list[str] | None = None) -> str:
    types = audience_user_types() if types is None else types
    phrases = [_ROLE_PHRASES.get(t, t) for t in SELECTABLE_USER_TYPES if t in types]
    phrases = [p for p in phrases if p != "Administrators"] or phrases
    if not phrases:
        who = "selected staff"
    elif len(phrases) == 1:
        who = phrases[0]
    else:
        who = ", ".join(phrases[:-1]) + " and " + phrases[-1]
    return (
        f"The IIC Booking app is currently available to {who}. "
        f"Please use {PORTAL_URL} in your browser."
    )


def is_app_client(request) -> bool:
    data = getattr(request, "data", None)
    value = ""
    if isinstance(data, dict):
        value = str(data.get("client") or "")
    if not value:
        value = str(getattr(request, "query_params", {}).get("client") or "")
    return value.strip().lower() == APP_CLIENT


def audience_refusal_payload() -> dict:
    return {"code": AUDIENCE_CODE, "error": audience_message(), "portal_url": PORTAL_URL}

"""WhatsApp settings. Nothing is sent unless every setting below is present and WHATSAPP_ENABLED is on."""

from __future__ import annotations

import re

from django.conf import settings

GRAPH_BASE = "https://graph.facebook.com"


def _s(name: str, default=""):
    return getattr(settings, name, default)


def missing_settings() -> list[str]:
    return [n for n in ("WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_ACCESS_TOKEN", "WHATSAPP_APP_SECRET",
                        "WHATSAPP_VERIFY_TOKEN") if not str(_s(n) or "").strip()]


def enabled() -> bool:
    return bool(_s("WHATSAPP_ENABLED", False)) and not missing_settings()


def api_version() -> str:
    return str(_s("WHATSAPP_API_VERSION", "v21.0") or "v21.0")


def phone_number_id() -> str:
    return str(_s("WHATSAPP_PHONE_NUMBER_ID") or "").strip()


def access_token() -> str:
    return str(_s("WHATSAPP_ACCESS_TOKEN") or "").strip()


def app_secret() -> str:
    return str(_s("WHATSAPP_APP_SECRET") or "").strip()


def verify_token() -> str:
    return str(_s("WHATSAPP_VERIFY_TOKEN") or "").strip()


def template_language() -> str:
    return str(_s("WHATSAPP_TEMPLATE_LANGUAGE", "en") or "en")


def waba_id() -> str:
    """The WhatsApp Business Account whose templates the portal manages. No default: it must be set on purpose."""
    return str(_s("WHATSAPP_WABA_ID") or "").strip()


def expected_waba_name() -> str:
    return str(_s("WHATSAPP_WABA_EXPECTED_NAME") or "").strip()


def template_name(event: str) -> str:
    """The Meta template for a portal event, from the catalogue; WHATSAPP_TEMPLATES may rename them."""
    from .template_catalogue import BY_EVENT

    names = dict(_s("WHATSAPP_TEMPLATES", {}) or {})
    entry = BY_EVENT.get(event)
    return str(names.get(event) or (entry.name if entry else ""))


def pilot_numbers() -> set[str]:
    """While set, messages go only to these numbers (E.164, e.g. +919876543210): a pilot before opening to all."""
    raw = _s("WHATSAPP_PILOT_NUMBERS", "") or ""
    items = raw if isinstance(raw, (list, tuple)) else re.split(r"[,;\s]*[,;\n][,;\s]*", str(raw))
    # Written the way people write numbers (9876543210, 91 98765 43210, +91-98765-43210): compared in the stored form,
    # +919876543210, so the format of the secret never blocks a pilot number (9 Nov 2026).
    from .service import OptInError, normalize_phone

    out = set()
    for x in items:
        text = str(x).strip()
        if not text:
            continue
        try:
            out.add(normalize_phone(text))
        except OptInError:
            out.add(text)
    return out


def allow_all() -> bool:
    return bool(_s("WHATSAPP_ALLOW_ALL", False))


def may_send_to(phone_e164: str) -> bool:
    """Fails closed: while a pilot list is set only those numbers; with no list, nobody unless WHATSAPP_ALLOW_ALL is on."""
    pilot = pilot_numbers()
    return phone_e164 in pilot if pilot else allow_all()


def retention_days() -> int:
    return max(1, int(_s("WHATSAPP_RETENTION_DAYS", 90) or 90))


def portal_url() -> str:
    return str(_s("FRONTEND_URL", "") or _s("PORTAL_BASE_URL", "") or "https://equip.iitr.ac.in").rstrip("/")

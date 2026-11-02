"""WhatsApp settings. Nothing is sent unless every setting below is present and WHATSAPP_ENABLED is on."""

from __future__ import annotations

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


def template_name(event: str) -> str:
    """The Meta-approved template for a portal event; WHATSAPP_TEMPLATES may rename them."""
    names = dict(_s("WHATSAPP_TEMPLATES", {}) or {})
    return str(names.get(event) or f"iic_{event}")


def pilot_numbers() -> set[str]:
    """While set, messages go only to these numbers (E.164, e.g. +919876543210): a pilot before opening to all."""
    raw = _s("WHATSAPP_PILOT_NUMBERS", "") or ""
    items = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
    return {str(x).strip() for x in items if str(x).strip()}


def portal_url() -> str:
    return str(_s("FRONTEND_URL", "") or _s("PORTAL_BASE_URL", "") or "https://equip.iitr.ac.in").rstrip("/")

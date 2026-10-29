"""Typed access to the I-STEM settings. Reads Django settings only; never touches the network."""

from django.conf import settings

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_LIVE = "live"
MODES = (MODE_OFF, MODE_SHADOW, MODE_LIVE)


def _setting(name: str, default=None):
    return getattr(settings, name, default)


def istem_enabled() -> bool:
    return bool(_setting("ISTEM_ENABLED", False))


def istem_mode() -> str:
    """``off`` unless the integration is enabled and ``ISTEM_MODE`` names a known mode."""
    if not istem_enabled():
        return MODE_OFF
    mode = str(_setting("ISTEM_MODE", MODE_OFF) or "").strip().lower()
    return mode if mode in MODES else MODE_OFF


def base_url() -> str:
    return str(_setting("ISTEM_BASE_URL", "") or "").strip()


def credentials() -> tuple[str, str]:
    return str(_setting("ISTEM_USERNAME", "") or ""), str(_setting("ISTEM_PASSWORD", "") or "")


def timeouts() -> tuple[float, float]:
    return float(_setting("ISTEM_CONNECT_TIMEOUT", 5)), float(_setting("ISTEM_READ_TIMEOUT", 30))


def ca_bundle() -> str:
    """``ISTEM_CA_BUNDLE``: a PEM file of CA certificates to verify I-STEM's TLS certificate with, or "" for the
    default (certifi) bundle. Verification is never turned off. [STAGING] istemstaging.iisc.ac.in chains to Let's
    Encrypt's newer "ISRG Root YR", which certifi 2026.06 does not carry yet: a bundle of certifi plus the Root YR
    certificate cross-signed by ISRG Root X1 verifies it."""
    return str(_setting("ISTEM_CA_BUNDLE", "") or "").strip()


def configured_institute_id() -> str:
    """``ISTEM_INSTITUTE_ID`` (IIT Roorkee: 213), used only when the token response names no institute ([STAGING]
    tokens carry none). When the token names one, that wins and a different configured value is refused."""
    value = str(_setting("ISTEM_INSTITUTE_ID", "") or "").strip()
    return value if value.isdigit() else ""


def max_attempts() -> int:
    return max(1, int(_setting("ISTEM_MAX_ATTEMPTS", 5)))


def user_creation_enabled() -> bool:
    """Remote Create User calls; off until the user-category mappings are confirmed. The institute_id sent is the one
    I-STEM returns with the access token, never a configured value."""
    return bool(_setting("ISTEM_USER_CREATION_ENABLED", False))


def accept_duplicate_user_id() -> bool:
    """Map automatically from a duplicate-user response; off until I-STEM documents that response's structure."""
    return bool(_setting("ISTEM_ACCEPT_DUPLICATE_USER_ID", False))


def otp_linking_enabled() -> bool:
    """Link a user I-STEM reports as already registered once they enter an OTP sent to the I-STEM email. Takes
    precedence over ``ISTEM_ACCEPT_DUPLICATE_USER_ID`` whenever the I-STEM email is known."""
    return bool(_setting("ISTEM_OTP_LINKING_ENABLED", False))


def silent_fields() -> dict:
    """``{ISTEM_SILENT_MODE_FIELD: ISTEM_SILENT_MODE_VALUE}`` sent with every booking call so I-STEM emails nobody,
    or {} while the field is unset. I-STEM has been asked for this flag; its name is set once they publish it."""
    name = str(_setting("ISTEM_SILENT_MODE_FIELD", "") or "").strip()
    return {name: str(_setting("ISTEM_SILENT_MODE_VALUE", "1"))} if name else {}


def confirm_enabled() -> bool:
    """Confirm Booking calls; off until the business trigger and approver identity are agreed."""
    return bool(_setting("ISTEM_CONFIRM_ENABLED", False))


def confirm_approved_by() -> str:
    """The approver name sent as ``approved_by`` on Confirm Booking (no default)."""
    return str(_setting("ISTEM_CONFIRM_APPROVED_BY", "") or "").strip()


def execute_enabled() -> bool:
    """FBR Execute calls; off until explicitly approved. Even when on, nothing triggers it automatically: completed
    bookings are only queued by ``istem_sync --enqueue``."""
    return bool(_setting("ISTEM_EXECUTE_ENABLED", False))


def execute_updated_by() -> str:
    """Optional ``updated_by`` for FBR Execute; empty means the field is omitted (I-STEM then uses a default)."""
    return str(_setting("ISTEM_EXECUTE_UPDATED_BY", "") or "").strip()


def missing_live_settings() -> list[str]:
    """Names of settings that must be present before any HTTP request is allowed."""
    missing = []
    if not base_url():
        missing.append("ISTEM_BASE_URL")
    username, password = credentials()
    if not username:
        missing.append("ISTEM_USERNAME")
    if not password:
        missing.append("ISTEM_PASSWORD")
    return missing


def network_allowed() -> bool:
    """True only in live mode with every connection setting present."""
    return istem_mode() == MODE_LIVE and not missing_live_settings()

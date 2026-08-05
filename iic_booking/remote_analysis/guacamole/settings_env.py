"""Apply production Guacamole / Remote Analysis settings from environment.

Does not change API contracts. Env vars override the admin singleton when present
so deployments can inject secrets without committing them to the database.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings

# Documented environment keys (Workstream 2).
ENV_GUACAMOLE_BASE_URL = "RA_GUACAMOLE_BASE_URL"
ENV_GUACAMOLE_API_URL = "RA_GUACAMOLE_API_URL"
ENV_GUACAMOLE_ADMIN_USERNAME = "RA_GUACAMOLE_ADMIN_USERNAME"
ENV_GUACAMOLE_ADMIN_PASSWORD = "RA_GUACAMOLE_ADMIN_PASSWORD"
ENV_GUACAMOLE_DATA_SOURCE = "RA_GUACAMOLE_DATA_SOURCE"
ENV_MOCK_GUACAMOLE = "RA_MOCK_GUACAMOLE"
ENV_VERIFY_TLS = "RA_GUACAMOLE_VERIFY_TLS"
ENV_APPLY_TO_DB = "RA_APPLY_ENV_SETTINGS"
ENV_TRANSPORT = "RA_TRANSPORT"
ENV_TUNNEL_GATEWAY_ADMIN_URL = "RA_TUNNEL_GATEWAY_ADMIN_URL"
ENV_TUNNEL_GATEWAY_WSS_URL = "RA_TUNNEL_GATEWAY_WSS_URL"
ENV_TUNNEL_ADAPTER_HOSTNAME = "RA_TUNNEL_ADAPTER_HOSTNAME"


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def overlay_from_environ(settings_obj: RemoteAnalysisSettings) -> RemoteAnalysisSettings:
    """
    Return the same instance with fields overridden from os.environ when set.
    Callers that persist the object will save overrides; GuacamoleClient uses
    get_solo() which applies overlays without requiring a DB write.
    """
    mapping = {
        ENV_GUACAMOLE_BASE_URL: "guacamole_base_url",
        ENV_GUACAMOLE_API_URL: "guacamole_api_url",
        ENV_GUACAMOLE_ADMIN_USERNAME: "guacamole_admin_username",
        ENV_GUACAMOLE_ADMIN_PASSWORD: "guacamole_admin_password",
        ENV_GUACAMOLE_DATA_SOURCE: "guacamole_data_source",
    }
    for env_key, attr in mapping.items():
        value = os.environ.get(env_key)
        if value is not None and value != "":
            setattr(settings_obj, attr, value.strip())

    if ENV_MOCK_GUACAMOLE in os.environ:
        settings_obj.mock_guacamole = _truthy(os.environ[ENV_MOCK_GUACAMOLE])

    if ENV_VERIFY_TLS in os.environ:
        settings_obj.verify_tls = _truthy(os.environ[ENV_VERIFY_TLS])

    if ENV_TRANSPORT in os.environ and os.environ[ENV_TRANSPORT].strip():
        mode = os.environ[ENV_TRANSPORT].strip().lower().replace("-", "_")
        # Reverse Tunnel is the only supported transport; ignore retired values.
        if mode == "reverse_tunnel":
            settings_obj.transport_mode = mode

    for env_key, attr in (
        (ENV_TUNNEL_GATEWAY_ADMIN_URL, "tunnel_gateway_admin_url"),
        (ENV_TUNNEL_GATEWAY_WSS_URL, "tunnel_gateway_wss_url"),
        (ENV_TUNNEL_ADAPTER_HOSTNAME, "tunnel_adapter_hostname"),
    ):
        value = os.environ.get(env_key)
        if value is not None and value != "":
            setattr(settings_obj, attr, value.strip())

    return settings_obj


def persist_from_environ() -> RemoteAnalysisSettings:
    """Load singleton, apply env overlays, and save (for bootstrapping production)."""
    from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings

    obj, _ = RemoteAnalysisSettings.objects.get_or_create(pk=1)
    overlay_from_environ(obj)
    obj.save()
    return obj


def production_guacamole_configured(settings_obj: RemoteAnalysisSettings) -> tuple[bool, list[str]]:
    """Validate that real Guacamole is configured when mock mode is off."""
    problems: list[str] = []
    if settings_obj.mock_guacamole:
        return True, problems
    if not (settings_obj.guacamole_api_url or "").strip():
        problems.append("guacamole_api_url missing")
    if not (settings_obj.guacamole_base_url or "").strip():
        problems.append("guacamole_base_url missing")
    if not (settings_obj.guacamole_admin_username or "").strip():
        problems.append("guacamole_admin_username missing")
    if not (settings_obj.guacamole_admin_password or "").strip():
        problems.append("guacamole_admin_password missing")
    return len(problems) == 0, problems

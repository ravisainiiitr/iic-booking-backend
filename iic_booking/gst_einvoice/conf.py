"""Configuration for the GST e-invoice sandbox demonstration. Environment variables only (never the database or the
browser), and only sandbox mode exists:

    GST_EINVOICE_ENABLED            "true" to show the module (default off)
    GST_EINVOICE_MODE               must be "sandbox"; any other value switches the module off
    GST_EINVOICE_PROVIDER           "mock" (default: simulated responses, nothing leaves the server) or "nic_sandbox"
    GST_EINVOICE_SANDBOX_BASE_URL   NIC sandbox base URL (default https://einv-apisandbox.nic.in)
    GST_EINVOICE_SANDBOX_CLIENT_ID / _CLIENT_SECRET / _USERNAME / _PASSWORD / _GSTIN
    GST_EINVOICE_SANDBOX_PUBLIC_KEY  the NIC *sandbox* public key (PEM text, or a path to the PEM file)

Points the NIC pages leave open, to confirm with the sandbox credentials at onboarding (defaults in brackets):

    GST_EINVOICE_SANDBOX_APPKEY_BYTES     length of the random AppKey: 32 or 16 [32]
    GST_EINVOICE_SANDBOX_CLIENT_ID_HEADER header spelling: client_id or client-id (client_secret follows it) [client_id]
    GST_EINVOICE_SANDBOX_VITAL_PREFIX     path prefix for auth and GSTIN [/eivital/v1.04]
    GST_EINVOICE_SANDBOX_CORE_PREFIX      path prefix for the invoice operations [/eicore/v1.03]

There is deliberately no production mode: no production host, credential name or switch exists in this module. A
base URL whose host is not on the sandbox allow-list, or that names a known production IRP host, refuses every call.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

from django.conf import settings

MODE_SANDBOX = "sandbox"
PROVIDER_MOCK = "mock"
PROVIDER_NIC_SANDBOX = "nic_sandbox"
PROVIDERS = (PROVIDER_MOCK, PROVIDER_NIC_SANDBOX)

DEFAULT_SANDBOX_BASE_URL = "https://einv-apisandbox.nic.in"
# The only hosts a request may go to.
SANDBOX_HOSTS = frozenset({"einv-apisandbox.nic.in"})
# Production IRP and e-way bill hosts: never contacted, even if configured by mistake.
PRODUCTION_HOSTS = frozenset({
    "einvoice1.gst.gov.in", "einv-api.nic.in", "api.einvoice1.gst.gov.in", "einvoice2.gst.gov.in",
    "einvoice3.gst.gov.in", "einvoice4.gst.gov.in", "einvoice5.gst.gov.in", "einvoice6.gst.gov.in",
    "api.einvoice6.gst.gov.in", "ewaybillgst.gov.in", "einvapi.trial.nic.in",
})


def _get(name: str, default: str = "") -> str:
    value = getattr(settings, name, None)
    if value is None:
        value = os.environ.get(name, default)
    return str(value if value is not None else default).strip()


def enabled() -> bool:
    return _get("GST_EINVOICE_ENABLED").lower() in ("1", "true", "yes", "on") and mode() == MODE_SANDBOX


def mode() -> str:
    """Always "sandbox" while enabled; anything else is treated as switched off (never as production)."""
    return MODE_SANDBOX if _get("GST_EINVOICE_MODE", MODE_SANDBOX).lower() == MODE_SANDBOX else "off"


def provider() -> str:
    value = _get("GST_EINVOICE_PROVIDER", PROVIDER_MOCK).lower()
    return value if value in PROVIDERS else PROVIDER_MOCK


def sandbox_base_url() -> str:
    return _get("GST_EINVOICE_SANDBOX_BASE_URL", DEFAULT_SANDBOX_BASE_URL).rstrip("/")


def sandbox_options() -> dict:
    """The configurable NIC points (never secrets)."""
    try:
        key_bytes = int(_get("GST_EINVOICE_SANDBOX_APPKEY_BYTES", "32"))
    except ValueError:
        key_bytes = 32
    header = _get("GST_EINVOICE_SANDBOX_CLIENT_ID_HEADER", "client_id").lower()

    def prefix(name, default):
        value = "/" + _get(name, default).strip("/")
        return value if all(c.isalnum() or c in "/._-" for c in value) else default

    return {
        "appkey_bytes": key_bytes if key_bytes in (16, 32) else 32,
        "client_id_header": header if header in ("client_id", "client-id") else "client_id",
        "vital_prefix": prefix("GST_EINVOICE_SANDBOX_VITAL_PREFIX", "/eivital/v1.04"),
        "core_prefix": prefix("GST_EINVOICE_SANDBOX_CORE_PREFIX", "/eicore/v1.03"),
    }


class ProductionEndpointRefused(Exception):
    """Raised before any network call to a host that is not the NIC sandbox."""


def assert_sandbox_url(url: str) -> str:
    """The URL unchanged when it points at the NIC sandbox over HTTPS; otherwise ProductionEndpointRefused."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        raise ProductionEndpointRefused(f"Only HTTPS sandbox URLs are allowed (got scheme {parsed.scheme!r}).")
    if host in PRODUCTION_HOSTS:
        raise ProductionEndpointRefused(f"{host} is a production GST IRP host; this module never calls production.")
    if host not in SANDBOX_HOSTS:
        raise ProductionEndpointRefused(f"{host or 'This host'} is not the NIC e-invoice sandbox.")
    return url


def sandbox_credentials() -> dict:
    """Sandbox-only credentials (the names carry SANDBOX so a production credential can never be picked up)."""
    key = _get("GST_EINVOICE_SANDBOX_PUBLIC_KEY")
    if key and not key.startswith("-----BEGIN") and os.path.isfile(key):
        with open(key, encoding="utf-8") as fh:
            key = fh.read()
    return {
        "client_id": _get("GST_EINVOICE_SANDBOX_CLIENT_ID"),
        "client_secret": _get("GST_EINVOICE_SANDBOX_CLIENT_SECRET"),
        "username": _get("GST_EINVOICE_SANDBOX_USERNAME"),
        "password": _get("GST_EINVOICE_SANDBOX_PASSWORD"),
        "gstin": _get("GST_EINVOICE_SANDBOX_GSTIN"),
        "public_key_pem": key,
    }


def missing_sandbox_credentials() -> list[str]:
    creds = sandbox_credentials()
    names = {"client_id": "GST_EINVOICE_SANDBOX_CLIENT_ID", "client_secret": "GST_EINVOICE_SANDBOX_CLIENT_SECRET",
             "username": "GST_EINVOICE_SANDBOX_USERNAME", "password": "GST_EINVOICE_SANDBOX_PASSWORD",
             "gstin": "GST_EINVOICE_SANDBOX_GSTIN", "public_key_pem": "GST_EINVOICE_SANDBOX_PUBLIC_KEY"}
    return [env for key, env in names.items() if not creds[key]]


def status() -> dict:
    """What the configuration page shows (never a secret)."""
    prov = provider()
    problem = ""
    if prov == PROVIDER_NIC_SANDBOX:
        missing = missing_sandbox_credentials()
        try:
            assert_sandbox_url(sandbox_base_url())
        except ProductionEndpointRefused as exc:
            problem = str(exc)
        if missing and not problem:
            problem = "Sandbox credentials missing: " + ", ".join(missing)
    return {
        "enabled": enabled(),
        "mode": mode(),
        "provider": prov,
        "provider_label": "Simulated sandbox (mock)" if prov == PROVIDER_MOCK else "NIC e-invoice sandbox",
        "simulated": prov == PROVIDER_MOCK,
        "sandbox_host": urlparse(sandbox_base_url()).hostname or "",
        "problem": problem,
        "production_available": False,
        "nic_options": sandbox_options(),
        "credentials_missing": missing_sandbox_credentials() if prov == PROVIDER_NIC_SANDBOX else [],
    }

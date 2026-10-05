"""
Exercise the portal's Guacamole REST client end to end against a live Guacamole.

Creates a throw-away user + RDP connection (hostname in TEST-NET-1, never contacted),
verifies credentials are stored, mints a user token, checks the user can see the
connection and that the client page loads, then deletes both. Prints no secrets.

Inside the portal (uses RemoteAnalysisSettings):
    docker exec -i iic-booking-backend-django-1 python manage.py shell < scripts/ops/guacamole-1.6/smoke_rest_client.py

Standalone (no Django; repo root on PYTHONPATH, `requests` installed):
    GUAC_API_URL=http://guacamole:8080/guacamole GUAC_ADMIN_USER=guacadmin GUAC_ADMIN_PASSWORD=... \
        python scripts/ops/guacamole-1.6/smoke_rest_client.py
"""

import os
import sys
import types
import uuid


def _django_ready():
    try:
        from django.conf import settings
    except ImportError:
        return False
    return settings.configured


def _install_settings_stub():
    class RemoteAnalysisSettings:
        mock_guacamole = False
        guacamole_api_url = os.environ["GUAC_API_URL"]
        guacamole_admin_username = os.environ.get("GUAC_ADMIN_USER", "guacadmin")
        guacamole_admin_password = os.environ["GUAC_ADMIN_PASSWORD"]
        guacamole_data_source = os.environ.get("GUAC_DATA_SOURCE", "postgresql")
        guacamole_base_url = os.environ.get("GUAC_BASE_URL", "")
        connection_timeout = 30
        verify_tls = False

        @classmethod
        def get_solo(cls):
            return cls()

    module = types.ModuleType("iic_booking.remote_analysis.session_models")
    module.RemoteAnalysisSettings = RemoteAnalysisSettings
    sys.modules["iic_booking.remote_analysis.session_models"] = module


def run_smoke():
    if not _django_ready():
        _install_settings_stub()

    import requests

    from iic_booking.remote_analysis.guacamole.client import GuacamoleClient, encode_client_identifier

    client = GuacamoleClient()
    if client.mock:
        raise SystemExit("FAIL Guacamole client is in mock mode - nothing to test")

    tag = uuid.uuid4().hex[:10]
    user = f"ra-smoke-{tag}"
    user_password = uuid.uuid4().hex
    conn_name = f"ra-smoke-{tag}"
    params = {
        "hostname": "192.0.2.10",
        "port": "3389",
        "username": "smoke-user",
        "password": uuid.uuid4().hex,
        "domain": "SMOKE",
        "security": "nla",
        "ignore-cert": "true",
        "width": "1920",
        "height": "1080",
        "color-depth": "24",
        "enable-drive": "false",
        "enable-audio": "true",
        "disable-audio": "",
        "disable-copy": "",
        "disable-paste": "",
        "enable-printing": "false",
        "disable-print": "true",
        "resize-method": "",
    }
    results = []
    conn_id = None

    def ok(step, detail=""):
        results.append(step)
        print(f"OK   {step}{(' - ' + detail) if detail else ''}")

    try:
        probe = client.health_probe()
        if not probe.get("ok"):
            raise RuntimeError(f"health_probe failed: {probe.get('error')}")
        ok("authenticate/health_probe", f"dataSource={client._data_source} latency_ms={probe['latency_ms']}")

        client.create_user(user, user_password)
        ok("create_user")

        created = client.create_connection(name=conn_name, parameters=params)
        conn_id = str(created.get("identifier") or "")
        if not conn_id:
            raise RuntimeError(f"create_connection returned no identifier: keys={sorted(created)}")
        ok("create_connection", f"identifier={conn_id}")

        client.update_connection_parameters(conn_id, parameters=params, name=conn_name)
        ok("update_connection_parameters (PUT)")

        stored = client.get_connection_parameters(conn_id)
        missing = [k for k in ("hostname", "username", "password", "width", "height", "color-depth") if not stored.get(k)]
        if missing:
            raise RuntimeError(f"parameters not stored: {missing}")
        if stored.get("password") != params["password"]:
            raise RuntimeError("stored password differs from the one sent")
        ok("get_connection_parameters", "credentials + display params persisted")

        client.grant_connection(user, conn_id)
        ok("grant_connection")

        user_token = client.create_user_token(user, user_password)
        ok("create_user_token")

        listing = requests.get(
            client._api(f"api/session/data/{client._data_source}/connections"),
            headers={"Guacamole-Token": user_token},
            timeout=30,
            verify=client._verify(),
        )
        listing.raise_for_status()
        if conn_id not in listing.json():
            raise RuntimeError("ephemeral user cannot see its granted connection")
        ok("user token sees granted connection")

        client_id = encode_client_identifier(conn_id, data_source=client._data_source)
        page = requests.get(client._api(""), timeout=30, verify=client._verify())
        if page.status_code != 200:
            raise RuntimeError(f"client page returned {page.status_code}")
        ok("client page", f"#/client/{client_id}?token=<redacted> served index ({len(page.content)} bytes)")

        requests.delete(
            client._api(f"api/tokens/{user_token}"),
            headers={"Guacamole-Token": user_token},
            timeout=30,
            verify=client._verify(),
        )
    finally:
        if conn_id:
            client.delete_connection(conn_id)
        client.delete_user(user)

    check = requests.get(
        client._api(f"api/session/data/{client._data_source}/connections/{conn_id}"),
        headers=client._headers(),
        timeout=30,
        verify=client._verify(),
    )
    if check.status_code != 404:
        raise RuntimeError(f"connection still present after delete (HTTP {check.status_code})")
    ok("delete_connection + delete_user", "cleanup verified")
    print(f"PASS {len(results)} checks")


run_smoke()

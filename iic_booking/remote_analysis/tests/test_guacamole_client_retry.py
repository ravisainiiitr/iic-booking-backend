"""Guacamole client retry / re-auth hardening (WS4)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from iic_booking.remote_analysis.guacamole.client import GuacamoleClient, GuacamoleClientError


@pytest.mark.django_db
def test_request_retries_once_on_connection_error(ra_settings):
    ra_settings.mock_guacamole = False
    ra_settings.guacamole_api_url = "https://guac.test"
    ra_settings.save()
    client = GuacamoleClient(ra_settings)
    client._auth_token = "tok"

    ok = MagicMock()
    ok.status_code = 200
    ok.content = b"{}"
    ok.json.return_value = {"ok": True}
    ok.raise_for_status = MagicMock()

    with patch(
        "iic_booking.remote_analysis.guacamole.client.requests.request",
        side_effect=[requests.ConnectionError("boom"), ok],
    ) as mocked:
        with patch.object(client, "authenticate", return_value="tok"):
            resp = client._request("GET", "https://guac.test/api/x")
    assert resp.status_code == 200
    assert mocked.call_count == 2


@pytest.mark.django_db
def test_request_reauths_on_401(ra_settings):
    ra_settings.mock_guacamole = False
    ra_settings.guacamole_api_url = "https://guac.test"
    ra_settings.save()
    client = GuacamoleClient(ra_settings)
    client._auth_token = "stale"

    unauthorized = MagicMock()
    unauthorized.status_code = 401
    unauthorized.raise_for_status = MagicMock(
        side_effect=requests.HTTPError("401")
    )

    ok = MagicMock()
    ok.status_code = 200
    ok.content = b"{}"
    ok.raise_for_status = MagicMock()

    with patch(
        "iic_booking.remote_analysis.guacamole.client.requests.request",
        side_effect=[unauthorized, ok],
    ):
        def _reauth():
            client._auth_token = "fresh"
            return "fresh"

        with patch.object(client, "authenticate", side_effect=_reauth) as auth:
            resp = client._request("GET", "https://guac.test/api/x")
    assert resp.status_code == 200
    assert auth.called
    assert client._auth_token == "fresh"


@pytest.mark.django_db
def test_request_does_not_retry_client_errors(ra_settings):
    ra_settings.mock_guacamole = False
    ra_settings.guacamole_api_url = "https://guac.test"
    ra_settings.save()
    client = GuacamoleClient(ra_settings)
    client._auth_token = "tok"

    bad = MagicMock()
    bad.status_code = 400
    bad.raise_for_status = MagicMock(side_effect=requests.HTTPError("400"))

    with patch(
        "iic_booking.remote_analysis.guacamole.client.requests.request",
        return_value=bad,
    ) as mocked:
        with patch.object(client, "authenticate", return_value="tok"):
            with pytest.raises(GuacamoleClientError):
                client._request("GET", "https://guac.test/api/x")
    assert mocked.call_count == 1

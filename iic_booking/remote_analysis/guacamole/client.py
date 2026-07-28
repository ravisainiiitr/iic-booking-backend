"""Low-level Guacamole REST client (internal use only)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import requests

from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings

logger = logging.getLogger(__name__)


class GuacamoleClientError(Exception):
    pass


class GuacamoleClient:
    """
    Talks to Apache Guacamole's REST API.
    Never return base URLs, auth tokens, or credentials to browsers.
    """

    def __init__(self, settings_obj: RemoteAnalysisSettings | None = None):
        self.settings = settings_obj or RemoteAnalysisSettings.get_solo()
        self._auth_token: str | None = None
        self._data_source = self.settings.guacamole_data_source or "postgresql"

    @property
    def mock(self) -> bool:
        return bool(self.settings.mock_guacamole) or not self.settings.guacamole_api_url

    def _timeout(self) -> int:
        return int(self.settings.connection_timeout or 30)

    def _verify(self) -> bool:
        return bool(self.settings.verify_tls)

    def _api(self, path: str) -> str:
        base = (self.settings.guacamole_api_url or "").rstrip("/")
        if not base:
            raise GuacamoleClientError("Guacamole API URL is not configured")
        return f"{base}/{path.lstrip('/')}"

    def authenticate(self) -> str:
        if self.mock:
            self._auth_token = f"mock-token-{uuid.uuid4().hex[:16]}"
            return self._auth_token

        url = self._api("api/tokens")
        try:
            resp = requests.post(
                url,
                data={
                    "username": self.settings.guacamole_admin_username,
                    "password": self.settings.guacamole_admin_password,
                },
                timeout=self._timeout(),
                verify=self._verify(),
            )
            resp.raise_for_status()
            payload = resp.json()
            token = payload.get("authToken") or payload.get("auth_token")
            if not token:
                raise GuacamoleClientError("Guacamole auth response missing authToken")
            self._auth_token = token
            ds = payload.get("dataSource")
            if ds:
                self._data_source = ds
            return token
        except requests.RequestException as exc:
            logger.exception("Guacamole authenticate failed")
            raise GuacamoleClientError(str(exc)) from exc

    def _headers(self) -> dict[str, str]:
        if not self._auth_token:
            self.authenticate()
        return {"Guacamole-Token": self._auth_token or ""}

    def health_check(self) -> bool:
        if self.mock:
            return True
        try:
            self.authenticate()
            return True
        except GuacamoleClientError:
            return False

    def create_connection(self, *, name: str, parameters: dict[str, Any], attributes: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.mock:
            conn_id = f"mock-conn-{uuid.uuid4().hex[:12]}"
            return {"identifier": conn_id, "name": name, "protocol": "rdp", "mock": True}

        self.authenticate()
        url = self._api(f"api/session/data/{self._data_source}/connections")
        body = {
            "name": name,
            "parentIdentifier": "ROOT",
            "protocol": "rdp",
            "parameters": parameters,
            "attributes": attributes or {
                "max-connections": "1",
                "max-connections-per-user": "1",
            },
        }
        try:
            resp = requests.post(
                url,
                json=body,
                headers=self._headers(),
                timeout=self._timeout(),
                verify=self._verify(),
            )
            resp.raise_for_status()
            return resp.json() if resp.content else {"identifier": resp.headers.get("Location", name)}
        except requests.RequestException as exc:
            logger.exception("Guacamole create_connection failed")
            raise GuacamoleClientError(str(exc)) from exc

    def create_user(self, username: str, password: str) -> dict[str, Any]:
        if self.mock:
            return {"username": username, "mock": True}

        self.authenticate()
        url = self._api(f"api/session/data/{self._data_source}/users")
        body = {
            "username": username,
            "password": password,
            "attributes": {
                "disabled": "",
                "expired": "",
                "access-window-start": "",
                "access-window-end": "",
                "valid-from": "",
                "valid-until": "",
                "timezone": None,
            },
        }
        try:
            resp = requests.post(
                url,
                json=body,
                headers=self._headers(),
                timeout=self._timeout(),
                verify=self._verify(),
            )
            if resp.status_code not in (200, 201, 204):
                resp.raise_for_status()
            return {"username": username}
        except requests.RequestException as exc:
            logger.exception("Guacamole create_user failed")
            raise GuacamoleClientError(str(exc)) from exc

    def grant_connection(self, username: str, connection_id: str) -> None:
        if self.mock:
            return
        self.authenticate()
        url = self._api(
            f"api/session/data/{self._data_source}/users/{username}/permissions"
        )
        patch = [
            {
                "op": "add",
                "path": f"/connectionPermissions/{connection_id}",
                "value": "READ",
            }
        ]
        try:
            resp = requests.patch(
                url,
                json=patch,
                headers=self._headers(),
                timeout=self._timeout(),
                verify=self._verify(),
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.exception("Guacamole grant_connection failed")
            raise GuacamoleClientError(str(exc)) from exc

    def delete_connection(self, connection_id: str) -> None:
        if self.mock or not connection_id:
            return
        self.authenticate()
        url = self._api(f"api/session/data/{self._data_source}/connections/{connection_id}")
        try:
            resp = requests.delete(
                url,
                headers=self._headers(),
                timeout=self._timeout(),
                verify=self._verify(),
            )
            if resp.status_code not in (200, 204, 404):
                resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Guacamole delete_connection failed: %s", exc)

    def delete_user(self, username: str) -> None:
        if self.mock or not username:
            return
        self.authenticate()
        url = self._api(f"api/session/data/{self._data_source}/users/{username}")
        try:
            resp = requests.delete(
                url,
                headers=self._headers(),
                timeout=self._timeout(),
                verify=self._verify(),
            )
            if resp.status_code not in (200, 204, 404):
                resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Guacamole delete_user failed: %s", exc)

    def create_user_token(self, username: str, password: str) -> str:
        """Obtain a short-lived Guacamole auth token for a temporary user (server-side)."""
        if self.mock:
            return f"mock-user-token-{uuid.uuid4().hex}"

        url = self._api("api/tokens")
        try:
            resp = requests.post(
                url,
                data={"username": username, "password": password},
                timeout=self._timeout(),
                verify=self._verify(),
            )
            resp.raise_for_status()
            payload = resp.json()
            token = payload.get("authToken") or payload.get("auth_token")
            if not token:
                raise GuacamoleClientError("Missing user authToken")
            return token
        except requests.RequestException as exc:
            raise GuacamoleClientError(str(exc)) from exc

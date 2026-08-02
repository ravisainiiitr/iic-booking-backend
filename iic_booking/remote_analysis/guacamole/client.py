"""Low-level Guacamole REST client (internal use only)."""

from __future__ import annotations

import base64
import logging
import time
import uuid
from typing import Any

import requests

from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings

logger = logging.getLogger(__name__)


class GuacamoleClientError(Exception):
    pass


def encode_client_identifier(
    connection_id: str,
    *,
    data_source: str = "postgresql",
    object_type: str = "c",
) -> str:
    """
    Build the Guacamole UI client identifier.

    Guacamole's ``#/client/<id>`` route expects base64(id + NUL + type + NUL + dataSource),
    where type is ``c`` (connection) or ``g`` (group). Passing a raw numeric id causes
    ``Illegal identifier - unknown type`` and guacd never starts the RDP tunnel.
    """
    raw = f"{connection_id}\0{object_type}\0{data_source}".encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


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

    def _request(
        self,
        method: str,
        url: str,
        *,
        authenticated: bool = True,
        allow_statuses: tuple[int, ...] = (),
        **kwargs: Any,
    ) -> requests.Response:
        """
        One transient retry on connection/5xx errors; re-auth once on 401 when authenticated.
        """
        kwargs.setdefault("timeout", self._timeout())
        kwargs.setdefault("verify", self._verify())
        extra_headers = dict(kwargs.pop("headers", None) or {})
        last_exc: Exception | None = None

        for attempt in range(2):
            try:
                headers = dict(extra_headers)
                if authenticated:
                    headers = {**headers, **self._headers()}
                resp = requests.request(method, url, headers=headers, **kwargs)
                if resp.status_code == 401 and authenticated and attempt == 0:
                    self._auth_token = None
                    logger.warning("Guacamole 401 — re-authenticating once")
                    continue
                if resp.status_code >= 500 and attempt == 0:
                    logger.warning(
                        "Guacamole %s %s returned %s — retrying once",
                        method,
                        url,
                        resp.status_code,
                    )
                    time.sleep(0.25)
                    continue
                if allow_statuses and resp.status_code in allow_statuses:
                    return resp
                resp.raise_for_status()
                return resp
            except requests.HTTPError as exc:
                raise GuacamoleClientError(str(exc)) from exc
            except requests.RequestException as exc:
                last_exc = exc
                if attempt == 0:
                    logger.warning(
                        "Guacamole %s %s failed (%s) — retrying once",
                        method,
                        url,
                        exc,
                    )
                    time.sleep(0.25)
                    continue
                raise GuacamoleClientError(str(exc)) from exc

        if last_exc:
            raise GuacamoleClientError(str(last_exc)) from last_exc
        raise GuacamoleClientError("Guacamole request failed")

    def health_check(self) -> bool:
        return self.health_probe().get("ok", False)

    def health_probe(self) -> dict[str, Any]:
        """Timed Guacamole connectivity probe for diagnostics / toolkit."""
        t0 = time.perf_counter()
        if self.mock:
            return {
                "ok": True,
                "status": "mock",
                "latency_ms": int((time.perf_counter() - t0) * 1000),
                "mock": True,
            }
        try:
            self.authenticate()
            return {
                "ok": True,
                "status": "ok",
                "latency_ms": int((time.perf_counter() - t0) * 1000),
                "mock": False,
            }
        except GuacamoleClientError as exc:
            return {
                "ok": False,
                "status": "unreachable",
                "latency_ms": int((time.perf_counter() - t0) * 1000),
                "mock": False,
                "error": str(exc)[:200],
            }

    def create_connection(self, *, name: str, parameters: dict[str, Any], attributes: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.mock:
            conn_id = f"mock-conn-{uuid.uuid4().hex[:12]}"
            return {"identifier": conn_id, "name": name, "protocol": "rdp", "mock": True}

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
            resp = self._request("POST", url, json=body)
            return resp.json() if resp.content else {"identifier": resp.headers.get("Location", name)}
        except GuacamoleClientError:
            logger.exception("Guacamole create_connection failed")
            raise

    def get_connection_parameters(self, connection_id: str) -> dict[str, Any]:
        """Fetch stored connection parameters (admin token). Used for diagnostics only."""
        if self.mock or not connection_id:
            return {"username": "mock", "password": "mock", "hostname": "mock-host"}
        url = self._api(
            f"api/session/data/{self._data_source}/connections/{connection_id}/parameters"
        )
        try:
            resp = self._request("GET", url)
            return resp.json() if resp.content else {}
        except GuacamoleClientError:
            logger.exception("Guacamole get_connection_parameters failed id=%s", connection_id)
            raise

    def update_connection_parameters(
        self,
        connection_id: str,
        *,
        parameters: dict[str, Any],
        name: str,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        """
        Full-replace connection config via PUT so username/password are reliably stored.
        Guacamole requires identifier + activeConnections on update.
        """
        if self.mock or not connection_id:
            return
        url = self._api(f"api/session/data/{self._data_source}/connections/{connection_id}")
        body = {
            "name": name,
            "identifier": str(connection_id),
            "parentIdentifier": "ROOT",
            "protocol": "rdp",
            "parameters": parameters,
            "attributes": attributes
            or {
                "max-connections": "1",
                "max-connections-per-user": "1",
            },
            "activeConnections": 0,
        }
        try:
            self._request("PUT", url, json=body, allow_statuses=(200, 204))
        except GuacamoleClientError:
            logger.exception("Guacamole update_connection_parameters failed id=%s", connection_id)
            raise

    def create_user(self, username: str, password: str) -> dict[str, Any]:
        if self.mock:
            return {"username": username, "mock": True}

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
            self._request("POST", url, json=body, allow_statuses=(200, 201, 204))
            return {"username": username}
        except GuacamoleClientError:
            logger.exception("Guacamole create_user failed")
            raise

    def grant_connection(self, username: str, connection_id: str) -> None:
        if self.mock:
            return
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
            self._request("PATCH", url, json=patch)
        except GuacamoleClientError:
            logger.exception("Guacamole grant_connection failed")
            raise

    def delete_connection(self, connection_id: str) -> None:
        if self.mock or not connection_id:
            return
        url = self._api(f"api/session/data/{self._data_source}/connections/{connection_id}")
        try:
            self._request("DELETE", url, allow_statuses=(200, 204, 404))
        except GuacamoleClientError as exc:
            logger.warning("Guacamole delete_connection failed: %s", exc)

    def delete_user(self, username: str) -> None:
        if self.mock or not username:
            return
        url = self._api(f"api/session/data/{self._data_source}/users/{username}")
        try:
            self._request("DELETE", url, allow_statuses=(200, 204, 404))
        except GuacamoleClientError as exc:
            logger.warning("Guacamole delete_user failed: %s", exc)

    def create_user_token(self, username: str, password: str) -> str:
        """Obtain a short-lived Guacamole auth token for a temporary user (server-side)."""
        if self.mock:
            return f"mock-user-token-{uuid.uuid4().hex}"

        url = self._api("api/tokens")
        try:
            resp = self._request(
                "POST",
                url,
                authenticated=False,
                data={"username": username, "password": password},
            )
            payload = resp.json()
            token = payload.get("authToken") or payload.get("auth_token")
            if not token:
                raise GuacamoleClientError("Missing user authToken")
            return token
        except GuacamoleClientError:
            raise

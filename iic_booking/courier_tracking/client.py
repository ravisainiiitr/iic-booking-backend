"""TrackCourier.io API client.

Only three endpoints are used: ``GET /couriers`` and ``GET /couriers/{slug}`` (not counted against the quota) and
``GET /track`` (counted). The key travels only in the ``X-API-Key`` header and is never logged; neither is a URL
(the track URL carries the tracking number). Errors are classified by ``error.code``, not the HTTP status, as the
provider documents. Callers get an ``ApiResult``; this module never raises for HTTP or network failures.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import requests
from django.conf import settings

from .constants import ERROR_CODE_OUTCOMES, LookupOutcome

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.trackcourier.io/v1"


@dataclass
class Usage:
    used: int | None = None
    quota: int | None = None
    plan: str = ""

    @classmethod
    def from_payload(cls, payload: Any) -> "Usage | None":
        if not isinstance(payload, dict) or not isinstance(payload.get("usage"), dict):
            return None
        raw = payload["usage"]

        def _int(value):
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        return cls(used=_int(raw.get("used")), quota=_int(raw.get("quota")), plan=str(raw.get("plan") or "")[:32])


@dataclass
class ApiResult:
    ok: bool
    outcome: str
    data: Any = None
    usage: Usage | None = None
    http_status: int | None = None
    error_code: str = ""
    message: str = ""  # provider's error message, for logs/admins only; never shown to end users verbatim
    duration_ms: int = 0
    extra: dict = field(default_factory=dict)


def tracking_hash(number: str) -> str:
    return hashlib.sha256((number or "").encode("utf-8")).hexdigest()[:16]


def masked(number: str) -> str:
    number = number or ""
    return f"...{number[-4:]}" if len(number) > 4 else "****"


class TrackCourierClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None, timeout: tuple[float, float] | None = None):
        self.api_key = api_key if api_key is not None else getattr(settings, "TRACKCOURIER_API_KEY", "")
        self.base_url = (base_url or getattr(settings, "TRACKCOURIER_API_BASE_URL", "") or DEFAULT_BASE_URL).rstrip("/")
        read = float(getattr(settings, "TRACKCOURIER_TIMEOUT_SECONDS", 20) or 20)
        self.timeout = timeout or (min(5.0, read), read)
        if not self.base_url.startswith("https://"):
            raise ValueError("TrackCourier.io requires HTTPS.")

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    # -- public ------------------------------------------------------------------------------------------------------

    def list_couriers(self) -> ApiResult:
        result = self._get("/couriers", what="couriers")
        if result.ok:
            couriers = (result.data or {}).get("couriers") if isinstance(result.data, dict) else None
            if not isinstance(couriers, list):
                return ApiResult(False, LookupOutcome.MALFORMED_RESPONSE, http_status=result.http_status,
                                 message="couriers list missing", duration_ms=result.duration_ms)
            clean = []
            for item in couriers:
                if isinstance(item, dict) and item.get("slug") and item.get("name"):
                    clean.append({"slug": str(item["slug"]).strip()[:64], "name": str(item["name"]).strip()[:128]})
            result.data = clean
        return result

    def get_courier(self, slug: str) -> ApiResult:
        return self._get(f"/couriers/{requests.utils.quote(slug, safe='')}", what="courier")

    def track(self, courier_slug: str, tracking_number: str) -> ApiResult:
        result = self._get(
            "/track",
            params={"courier": courier_slug, "tracking_number": tracking_number},
            what=f"track {courier_slug} {masked(tracking_number)}",
        )
        if result.ok and not isinstance(result.data, dict):
            return ApiResult(False, LookupOutcome.MALFORMED_RESPONSE, usage=result.usage, http_status=result.http_status,
                             message="tracking data missing", duration_ms=result.duration_ms)
        return result

    # -- internals ---------------------------------------------------------------------------------------------------

    def _get(self, path: str, *, params: dict | None = None, what: str) -> ApiResult:
        if not self.api_key:
            return ApiResult(False, LookupOutcome.AUTH_ERROR, error_code="API_KEY_REQUIRED",
                             message="TRACKCOURIER_API_KEY is not configured")
        started = time.monotonic()
        try:
            response = requests.get(
                f"{self.base_url}{path}",
                params=params,
                headers={"X-API-Key": self.api_key, "Accept": "application/json"},
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.Timeout:
            return self._log(ApiResult(False, LookupOutcome.TIMEOUT, message="request timed out",
                                       duration_ms=_ms(started)), what)
        except requests.RequestException as exc:
            return self._log(ApiResult(False, LookupOutcome.NETWORK_ERROR, message=type(exc).__name__,
                                       duration_ms=_ms(started)), what)
        duration = _ms(started)
        try:
            payload = response.json()
        except ValueError:
            outcome = LookupOutcome.SERVICE_ERROR if response.status_code >= 500 else LookupOutcome.MALFORMED_RESPONSE
            return self._log(ApiResult(False, outcome, http_status=response.status_code, message="non-JSON response",
                                       duration_ms=duration), what)
        usage = Usage.from_payload(payload)
        if isinstance(payload, dict) and payload.get("success") is True and 200 <= response.status_code < 300:
            return self._log(ApiResult(True, LookupOutcome.SUCCESS, data=payload.get("data"), usage=usage,
                                       http_status=response.status_code, duration_ms=duration,
                                       extra={"proof_of_delivery": payload.get("proof_of_delivery")}), what)
        error = payload.get("error") if isinstance(payload, dict) else None
        code = str(error.get("code") or "") if isinstance(error, dict) else ""
        message = str(error.get("message") or "")[:300] if isinstance(error, dict) else ""
        outcome = ERROR_CODE_OUTCOMES.get(code)
        if outcome is None:
            # Undocumented error shape: classify conservatively by status class only.
            outcome = LookupOutcome.SERVICE_ERROR if response.status_code >= 500 else LookupOutcome.MALFORMED_RESPONSE
        return self._log(ApiResult(False, outcome, usage=usage, http_status=response.status_code, error_code=code,
                                   message=message, duration_ms=duration,
                                   extra={"did_you_mean": error.get("did_you_mean")} if isinstance(error, dict) else {}),
                         what)

    @staticmethod
    def _log(result: ApiResult, what: str) -> ApiResult:
        level = logging.INFO if result.ok else logging.WARNING
        logger.log(
            level,
            "TrackCourier %s -> %s (http %s, code %s, %s ms%s)",
            what,
            result.outcome,
            result.http_status,
            result.error_code or "-",
            result.duration_ms,
            f", usage {result.usage.used}/{result.usage.quota}" if result.usage else "",
        )
        return result


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)

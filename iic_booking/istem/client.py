"""Centralised I-STEM HTTP client (contract in ``contract.py``).

- Refuses to exist unless the integration is in live mode with every connection setting present.
- The access token is kept in process memory only (never in the database or logs) and reacquired before the
  documented one-hour expiry. The token response also identifies the account (``institute_id``, ``user_id``,
  ``user_entity``); the institute is always taken from there, never from configuration.
- A request refused for authentication (HTTP 401, or a documented ``status: ERROR`` token message such as "Token not
  found") was not processed, so it is repeated once with a fresh token; this holds for POST too.
- Otherwise no automatic retries: the outbox decides what may be sent again.
- Logs carry the operation, HTTP status, duration and error category, never bodies, credentials or tokens.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from requests.adapters import HTTPAdapter

from . import conf, contract
from .choices import ErrorCategory
from .errors import IstemConfigError, IstemError

logger = logging.getLogger("iic_booking.istem")

TOKEN_EXPIRY_MARGIN_SECONDS = 300
MAX_MESSAGE_LENGTH = 200
# [STAGING] The institute filter is ignored, so the whole catalogue (about 31,000 rows, ~30 ms a row) is read and
# filtered here: large pages and a page cap that covers it.
EQUIPMENT_PAGE_SIZE = 1000
EQUIPMENT_MAX_PAGES = 60
# Get All Bookings is read newest request first; a lookup stops once the page has passed the request it looks for.
LISTING_PAGE_SIZE = 100
LISTING_MAX_PAGES = 30
# A refused request whose message is about the token ([SPEC] example: "Token not found"). Refusal happens before any
# processing, so the request can be repeated with a fresh token.
TOKEN_ERROR_MESSAGE = re.compile(r"\btoken\b", re.IGNORECASE)


@dataclass(frozen=True)
class TransportResponse:
    status_code: int
    text: str


class RequestsTransport:
    """``requests``-based transport with explicit timeouts and no automatic retries or redirects."""

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        # Always verified: a configured CA bundle replaces the default one, it never disables checking.
        self.session.verify = conf.ca_bundle() or True
        adapter = HTTPAdapter(max_retries=0)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def send(self, method: str, url: str, *, headers: dict, data: dict | None = None, params: dict | None = None,
             timeout: tuple[float, float]) -> TransportResponse:
        try:
            resp = self.session.request(
                method, url, headers=headers, data=data, params=params, timeout=timeout, allow_redirects=False
            )
        except requests.exceptions.ConnectTimeout as exc:
            raise IstemError(ErrorCategory.CONNECT_FAILED, "Connection to I-STEM timed out before the request was sent.") from exc
        except requests.exceptions.SSLError as exc:
            raise IstemError(ErrorCategory.TLS, "TLS handshake with I-STEM failed; the request was not sent.") from exc
        except requests.exceptions.ReadTimeout as exc:
            raise IstemError(ErrorCategory.READ_TIMEOUT, "I-STEM did not answer in time; the request may have been processed.") from exc
        except requests.exceptions.ConnectionError as exc:
            if _never_connected(exc):
                raise IstemError(ErrorCategory.CONNECT_FAILED, "Could not connect to I-STEM; the request was not sent.") from exc
            raise IstemError(ErrorCategory.CONNECTION_LOST, "Connection to I-STEM was lost; the request may have been processed.") from exc
        except requests.exceptions.RequestException as exc:
            raise IstemError(ErrorCategory.CONNECTION_LOST, "The I-STEM request failed in transit; it may have been processed.") from exc
        return TransportResponse(resp.status_code, resp.text)


def _never_connected(exc: BaseException) -> bool:
    """True when the failure happened while opening the connection (DNS, refused, connect timeout)."""
    from urllib3 import exceptions as u3

    seen = set()
    stack = [exc]
    while stack:
        cur = stack.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, (u3.NewConnectionError, u3.ConnectTimeoutError, u3.NameResolutionError)):
            return True
        stack.extend([getattr(cur, "reason", None), cur.__cause__, cur.__context__])
        stack.extend(a for a in getattr(cur, "args", ()) if isinstance(a, BaseException))
    return False


@dataclass(frozen=True)
class AuthInfo:
    """Who the integration's token belongs to ([SPEC] /auth/token). Not secret; never contains the token."""

    institute_id: str | None
    user_id: str | None
    user_entity: str


# Process-wide token cache: {(base_url, username): (token, monotonic_expiry, AuthInfo)}.
_token_lock = threading.Lock()
_token_cache: dict[tuple[str, str], tuple[str, float, AuthInfo]] = {}


def clear_token_cache() -> None:
    with _token_lock:
        _token_cache.clear()


def _safe_message(body: dict) -> str:
    msg = body.get("message")
    text = msg if isinstance(msg, str) else ""
    return text.strip()[:MAX_MESSAGE_LENGTH]


def _as_remote_id(value: Any) -> str | None:
    """A documented numeric identifier as a string, or None when absent or malformed."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return str(value)
    if isinstance(value, str) and value.strip().isdigit() and len(value.strip()) <= 20 and int(value.strip()) > 0:
        return value.strip()
    return None


def _is_success(body: dict) -> bool | None:
    """True / False for a recognised status, None when the status field is missing or unrecognised."""
    status = body.get("status", body.get("result"))
    if isinstance(status, str):
        s = status.strip().lower()
        if s in contract.SUCCESS_STATUS_VALUES:
            return True
        if s in {"false", "fail", "failed", "failure", "error"}:
            return False
        return None
    if status is False:
        return False
    return None


def _is_token_refusal(body: dict) -> bool:
    return _is_success(body) is False and bool(TOKEN_ERROR_MESSAGE.search(_safe_message(body)))


def _is_not_found(body: dict) -> bool:
    """Documented "nothing matched" answers of the read-only endpoints (status ERROR, message "... not found")."""
    return _is_success(body) is False and ("not found" in _safe_message(body).lower() or "no matching" in _safe_message(body).lower())


@dataclass(frozen=True)
class CreateUserResult:
    created: bool  # True: documented success; False: duplicate-user response
    user_id: str | None  # remote public user id when it could be validated
    message: str
    email: str | None = None  # duplicate only: the email registered on I-STEM, when the response carries one
    matched_on: str = ""  # duplicate only: "email" or "mobile", from the message
    profile: dict = field(default_factory=dict)  # duplicate only: the existing user's record, as from the lookup


# Profile keys read from a Public User Lookup row -> the names I-STEM may use for them (first present wins).
LOOKUP_PROFILE_KEYS = {
    # [STAGING] The lookup row spells them user_firstname / user_lastname, padded with spaces.
    "first_name": ("user_first_name", "user_firstname", "first_name"),
    "last_name": ("user_last_name", "user_lastname", "last_name"),
    "salutation": ("user_salutation",),
    "designation": ("user_designation",),
    "mobile": ("user_contactno", "contact_no", "mobile"),
    "organisation": ("user_organisation", "organisation", "organization"),
    "department": ("user_department", "department"),
    "address": ("user_address", "address"),
    "city": ("user_city", "city"),
    "district": ("user_district", "district"),
    "state": ("user_state", "state"),
    "country": ("user_country", "country"),
    "pin": ("user_pin", "pincode", "pin"),
    "gstin": ("gstin", "user_gstin", "booking_gstn"),
    "billing_name": ("billing_user_name", "billing_name"),
    "billing_address": ("billing_address",),
    "user_type": ("user_type", "user_category"),
}


def _lookup_profile(row: dict) -> dict:
    profile = {}
    email = _registered_email(row)
    if email:
        profile["email"] = email
    for key, names in LOOKUP_PROFILE_KEYS.items():
        for name in names:
            value = row.get(name)
            if isinstance(value, (str, int)) and str(value).strip():
                profile[key] = str(value).strip()[:255]
                break
    return profile


def _registered_email(row: Any) -> str | None:
    """A syntactically valid email from a user row, under any of the key names I-STEM uses for it."""
    if not isinstance(row, dict):
        return None
    for key in ("user_email", "email", "email_id", "emailid", "user_emailid"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            try:
                validate_email(value.strip())
            except ValidationError:
                return None
            return value.strip().lower()
    return None


@dataclass(frozen=True)
class CreateBookingResult:
    request_id: str  # new_service_id
    fbr_no: str | None
    existing: bool = False  # I-STEM returned an identical existing booking (idempotent repeat)


@dataclass(frozen=True)
class ConfirmBookingResult:
    request_id: str
    confirmation_id: str
    already_confirmed: bool = False  # [SPEC] already_confirmed: result is {} and only confirmation_id is returned


@dataclass(frozen=True)
class ExecuteResult:
    request_id: str
    confirmation_id: str
    payment_id: str
    already_executed: bool
    message: str


@dataclass(frozen=True)
class EquipmentPage:
    rows: list[dict]  # {"equipment_id": str, "equipment_name": str}
    total: int | None
    raw_count: int = 0  # rows I-STEM returned on the page, before dropping other institutes' equipment


class IstemClient:
    def __init__(self, transport=None, *, clock: Callable[[], float] = time.monotonic):
        if not conf.network_allowed():
            missing = conf.missing_live_settings()
            detail = f" Missing: {', '.join(missing)}." if missing and conf.istem_mode() == conf.MODE_LIVE else ""
            raise IstemConfigError(f"I-STEM requests are not allowed in mode '{conf.istem_mode()}'.{detail}")
        self.transport = transport or RequestsTransport()
        self.clock = clock
        self.base = conf.base_url().rstrip("/") + "/"
        self.username, self._password = conf.credentials()
        self.timeout = conf.timeouts()

    # --- authentication ------------------------------------------------------------------------------------------
    def _cache_key(self) -> tuple[str, str]:
        return (self.base, self.username)

    def _session(self, *, force: bool = False) -> tuple[str, AuthInfo]:
        with _token_lock:
            cached = _token_cache.get(self._cache_key())
            if cached and not force and cached[1] > self.clock():
                return cached[0], cached[2]
            token, lifetime, info = self._authenticate()
            _token_cache[self._cache_key()] = (token, self.clock() + max(60, lifetime - TOKEN_EXPIRY_MARGIN_SECONDS), info)
            return token, info

    def _token(self, *, force: bool = False) -> str:
        return self._session(force=force)[0]

    def session_info(self) -> AuthInfo:
        """The account behind the token (authenticates when no valid token is cached)."""
        return self._session()[1]

    def _forget_token(self, token: str) -> None:
        with _token_lock:
            cached = _token_cache.get(self._cache_key())
            if cached and cached[0] == token:
                _token_cache.pop(self._cache_key(), None)

    def _authenticate(self) -> tuple[str, int, AuthInfo]:
        started = self.clock()
        try:
            resp = self.transport.send(
                "POST",
                self.base + contract.AUTH_TOKEN_PATH,
                headers={"accept": "*/*", "Content-Type": contract.FORM_CONTENT_TYPE},
                data={"username": self.username, "password": self._password},
                timeout=self.timeout,
            )
        except IstemError as exc:
            self._log("auth", None, started, ErrorCategory.AUTH_UNAVAILABLE)
            raise IstemError(ErrorCategory.AUTH_UNAVAILABLE, f"Could not obtain an I-STEM token ({exc.category}).") from exc
        if resp.status_code in (401, 403):
            self._log("auth", resp.status_code, started, ErrorCategory.AUTH)
            raise IstemError(ErrorCategory.AUTH, "I-STEM rejected the integration credentials.", http_status=resp.status_code)
        if resp.status_code >= 500:
            self._log("auth", resp.status_code, started, ErrorCategory.AUTH_UNAVAILABLE)
            raise IstemError(ErrorCategory.AUTH_UNAVAILABLE, f"I-STEM token service answered HTTP {resp.status_code}.", http_status=resp.status_code)
        body = self._json(resp, "auth", started, category_on_error=ErrorCategory.AUTH_UNAVAILABLE)
        message = body.get("message")
        token = message.get("access_token") if isinstance(message, dict) else None
        # [SPEC] wrong credentials: HTTP 200 with {"result": "Error", "message": "Invalid Login Credentials"}.
        if _is_success(body) is not True or not isinstance(token, str) or not token.strip():
            self._log("auth", resp.status_code, started, ErrorCategory.AUTH)
            raise IstemError(ErrorCategory.AUTH, "I-STEM did not issue an access token.", http_status=resp.status_code)
        expires_in = message.get("expires_in")
        lifetime = expires_in if isinstance(expires_in, int) and 0 < expires_in <= 86400 else contract.DOCUMENTED_TOKEN_LIFETIME_SECONDS
        entity = message.get("user_entity")
        institute_id = _as_remote_id(message.get("institute_id"))
        configured = conf.configured_institute_id()
        if institute_id and configured and configured != institute_id:
            self._log("auth", resp.status_code, started, ErrorCategory.CONFIG)
            raise IstemConfigError(
                f"ISTEM_INSTITUTE_ID is {configured} but the I-STEM token belongs to institute {institute_id}."
            )
        info = AuthInfo(
            # [STAGING] The token names no institute: the configured one stands in.
            institute_id=institute_id or configured or None,
            user_id=_as_remote_id(message.get("user_id")),
            user_entity=entity.strip()[:16] if isinstance(entity, str) else "",
        )
        self._log("auth", resp.status_code, started, None)
        return token.strip(), lifetime, info

    # --- transport helpers ---------------------------------------------------------------------------------------
    def _log(self, op: str, http_status, started: float, category) -> None:
        logger.info(
            "istem op=%s http_status=%s duration_ms=%d category=%s",
            op, http_status, int((self.clock() - started) * 1000), category or "-",
        )

    def _json(self, resp: TransportResponse, op: str, started: float, *, category_on_error=ErrorCategory.INVALID_RESPONSE) -> dict:
        try:
            body = json.loads(resp.text)
        except (TypeError, ValueError):
            self._log(op, resp.status_code, started, category_on_error)
            raise IstemError(category_on_error, "I-STEM returned a response that is not JSON.", http_status=resp.status_code) from None
        if not isinstance(body, dict):
            self._log(op, resp.status_code, started, category_on_error)
            raise IstemError(category_on_error, "I-STEM returned an unexpected response structure.", http_status=resp.status_code)
        return body

    @staticmethod
    def _parsed_token_refusal(resp: TransportResponse) -> bool:
        if resp.status_code != 200:
            return False
        try:
            body = json.loads(resp.text)
        except (TypeError, ValueError):
            return False
        return isinstance(body, dict) and _is_token_refusal(body)

    def _send(self, method: str, path: str, token: str, *, data, params) -> TransportResponse:
        headers = {"accept": "*/*", contract.TOKEN_HEADER: token}
        if data is not None:
            headers["Content-Type"] = contract.FORM_CONTENT_TYPE
        return self.transport.send(method, self.base + path, headers=headers, data=data, params=params, timeout=self.timeout)

    def _call(self, op: str, method: str, path: str, *, data: dict | None = None, params: dict | None = None) -> dict:
        """One request, repeated once with a fresh token if it was refused for authentication (never processed)."""
        if data is not None and op in contract.SILENT_OPERATIONS:
            data = {**data, **conf.silent_fields()}
        started = self.clock()
        token = self._token()
        try:
            resp = self._send(method, path, token, data=data, params=params)
            if resp.status_code == 401 or self._parsed_token_refusal(resp):
                self._forget_token(token)
                token = self._token(force=True)
                resp = self._send(method, path, token, data=data, params=params)
        except IstemError as exc:
            self._log(op, None, started, exc.category)
            raise
        status = resp.status_code
        if status == 401:
            self._forget_token(token)  # refused again: the next call authenticates afresh
        category = None
        if status in (401, 403):
            category = ErrorCategory.AUTH if status == 401 else ErrorCategory.FORBIDDEN
        elif status == 429:
            category = ErrorCategory.RATE_LIMITED
        elif status >= 500:
            category = ErrorCategory.SERVER_ERROR
        elif status == 404:
            category = ErrorCategory.NOT_FOUND
        elif 400 <= status < 500:
            category = ErrorCategory.VALIDATION
        elif status != 200:
            category = ErrorCategory.INVALID_RESPONSE
        if category:
            self._log(op, status, started, category)
            raise IstemError(category, f"I-STEM answered HTTP {status}.", http_status=status)
        body = self._json(resp, op, started)
        if _is_token_refusal(body):
            self._forget_token(token)
            self._log(op, status, started, ErrorCategory.AUTH)
            raise IstemError(ErrorCategory.AUTH, "I-STEM refused the access token twice.", http_status=status)
        self._log(op, status, started, None)
        return body

    # --- operations ----------------------------------------------------------------------------------------------
    def create_user(self, payload: dict) -> CreateUserResult:
        """POST api/public-user. Documented success: {"status": "SUCCESS", "message": ..., "user_id": <int>}.

        A response that says the email/mobile already exists is returned as ``created=False``; its user id is only
        taken from the documented ``user_id`` key, and only when it is a valid identifier.
        """
        body = self._call("create_user", "POST", contract.CREATE_USER_PATH, data=payload)
        success = _is_success(body)
        message = _safe_message(body)
        if success is True:
            user_id = _as_remote_id(body.get("user_id"))
            if not user_id:
                raise IstemError(ErrorCategory.INVALID_RESPONSE, "Create User succeeded without a valid user_id.")
            return CreateUserResult(True, user_id, message)
        lowered = message.lower()
        if success is False and "already" in lowered and ("exist" in lowered or "registered" in lowered):
            # [STAGING] The existing user is in data[0] ({"result": "ERROR", "message": "Mobile number already exists",
            # "data": [{"user_id": "60016", ...}]}); a top-level user_id is taken first if I-STEM ever sends one.
            # The registered email is read from the same place. [STAGING] Since 2026-10-29 data[0] is the user's full
            # record (same keys as the lookup); before, it carried only user_id, user_name and user_status.
            existing = _as_remote_id(body.get("user_id"))
            email = _registered_email(body) if existing else None
            profile = {}
            data = body.get("data")
            if not existing and isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
                existing = _as_remote_id(data[0].get("user_id"))
                email = _registered_email(data[0])
                profile = _lookup_profile(data[0])
            matched_on = "email" if "email" in lowered else "mobile" if ("mobile" in lowered or "contact" in lowered) else ""
            return CreateUserResult(False, existing, message, email=email if existing else None, matched_on=matched_on,
                                    profile=profile if existing else {})
        if success is False:
            raise IstemError(ErrorCategory.BUSINESS_ERROR, f"I-STEM refused Create User: {message or 'no message'}")
        raise IstemError(ErrorCategory.INVALID_RESPONSE, "Create User returned an unrecognised status.")

    def lookup_public_user(self, *, user_email: str = "", user_contactno: str = "") -> list[dict]:
        """GET api/public-user-lookup (read-only). Active users matching the email and/or mobile: a list of
        ``{"user_id", "user_name", "profile"}`` (empty when nothing matched). ``profile`` holds whatever of the user's
        record I-STEM returns (requested: email, organisation, department, address, state, GSTIN, user type); keys
        I-STEM does not send are absent."""
        params = {k: v for k, v in (("user_email", user_email), ("user_contactno", user_contactno)) if v}
        if not params:
            raise IstemConfigError("Public user lookup needs an email or a mobile number.")
        body = self._call("lookup_user", "GET", contract.PUBLIC_USER_LOOKUP_PATH, params=params)
        if _is_not_found(body):
            return []
        if _is_success(body) is not True:
            raise IstemError(ErrorCategory.INVALID_RESPONSE, "Public user lookup returned an unexpected structure.")
        result = body.get("result")
        rows = [result] if isinstance(result, dict) else result if isinstance(result, list) else None
        if rows is None:
            raise IstemError(ErrorCategory.INVALID_RESPONSE, "Public user lookup returned no result.")
        out = []
        for row in rows:
            user_id = _as_remote_id(row.get("user_id")) if isinstance(row, dict) else None
            if user_id:
                name = row.get("user_name")
                out.append({"user_id": user_id, "user_name": name.strip()[:64] if isinstance(name, str) else "",
                            "profile": _lookup_profile(row)})
        return out

    def list_equipment(self, *, institute_id: str | None = None, page: int = 1, limit: int = EQUIPMENT_PAGE_SIZE,
                       equipment_name: str = "") -> EquipmentPage:
        """GET api/equipment-list (read-only, Active equipment only; an institute token is restricted to its own
        institute and the institute_id parameter is then ignored by I-STEM). ``equipment_name`` narrows the listing
        to that name ([STAGING] exact, fast); rows of another institute are always dropped."""
        params = {"page": page, "limit": limit}
        if institute_id:
            params["institute_id"] = institute_id
        if equipment_name:
            params["equipment_name"] = equipment_name
        body = self._call("equipment_list", "GET", contract.EQUIPMENT_LIST_PATH, params=params)
        result = body.get("result")
        if _is_success(body) is False and (result == [] or result is None) and "no equipment" in _safe_message(body).lower():
            return EquipmentPage([], 0)
        if _is_success(body) is not True or not isinstance(result, list):
            raise IstemError(ErrorCategory.INVALID_RESPONSE, "Equipment List returned an unexpected structure.")
        rows = []
        for row in result:
            equipment_id = _as_remote_id(row.get("equipment_id")) if isinstance(row, dict) else None
            if not equipment_id:
                continue
            # [STAGING] The institute_id parameter is ignored and every institute's equipment comes back: rows of
            # another institute are dropped here.
            row_institute = _as_remote_id(row.get("institute_id"))
            if institute_id and row_institute and row_institute != str(institute_id):
                continue
            name = row.get("equipment_name")
            rows.append({"equipment_id": equipment_id, "equipment_name": name.strip()[:255] if isinstance(name, str) else ""})
        total = body.get("totalrecord")
        return EquipmentPage(rows, total if isinstance(total, int) and not isinstance(total, bool) and total >= 0 else None,
                             raw_count=len(result))

    def list_all_equipment(self, *, institute_id: str | None = None) -> list[dict]:
        """Every page of the Equipment List (bounded). Raises if the listing cannot be read completely."""
        rows: list[dict] = []
        seen = 0
        for page in range(1, EQUIPMENT_MAX_PAGES + 1):
            chunk = self.list_equipment(institute_id=institute_id, page=page, limit=EQUIPMENT_PAGE_SIZE)
            rows.extend(chunk.rows)
            seen += chunk.raw_count
            # Paging follows what I-STEM returned, not what was kept after dropping other institutes' rows.
            if not chunk.raw_count or chunk.raw_count < EQUIPMENT_PAGE_SIZE or (chunk.total is not None and seen >= chunk.total):
                return rows
        raise IstemError(ErrorCategory.INVALID_RESPONSE, "Equipment List did not end within the page limit.")

    def find_listed_booking(self, request_id: str, status_code: str) -> dict | None:
        """The custodian's booking ``request_id`` as listed by GET api/get-all-bookings for ``status_code``, or None.

        [STAGING] The only read that carries a booking's confirmation_id and its FBR number (``fbc_no``) by request
        id. Rows come newest request first, so reading stops at the first page that has passed ``request_id``.
        Returns {"request_id", "confirmation_id", "fbr_no", "status_code"} with validated identifiers.
        """
        wanted = _as_remote_id(request_id)
        if not wanted:
            raise IstemConfigError("A booking lookup needs a numeric request_id.")
        for page in range(1, LISTING_MAX_PAGES + 1):
            body = self._call("get_all_bookings", "GET", contract.GET_ALL_BOOKINGS_PATH,
                              params={"status_code": status_code, "page": page, "limit": LISTING_PAGE_SIZE})
            result = body.get("result")
            if _is_success(body) is not True or not isinstance(result, list):
                raise IstemError(ErrorCategory.INVALID_RESPONSE, "Get All Bookings returned an unexpected structure.")
            ids = []
            for row in result:
                if not isinstance(row, dict):
                    continue
                row_id = _as_remote_id(row.get("book_req_id"))
                if row_id == wanted:
                    fbr = str(row.get("fbc_no") or row.get("generated_fbc_no") or "").strip()
                    return {
                        "request_id": row_id,
                        "confirmation_id": _as_remote_id(row.get("confirmation_id")),
                        "fbr_no": fbr if fbr.isdigit() and len(fbr) <= 64 else None,
                        "status_code": str(row.get("status_code") or "").strip()[:64],
                    }
                if row_id:
                    ids.append(int(row_id))
            if len(result) < LISTING_PAGE_SIZE or (ids and max(ids) < int(wanted)):
                return None
        return None

    def create_booking(self, payload: dict) -> CreateBookingResult:
        """POST api/create-booking. Success carries new_service_id and fbr_no; an identical existing booking is
        returned the same way (idempotent). [STAGING] Success carries no fbr_no: it is then read from Get All Bookings
        ("Pending"); None when that read cannot find it (the service flags the link for review)."""
        body = self._call("create_booking", "POST", contract.CREATE_BOOKING_PATH, data=payload)
        success = _is_success(body)
        if success is False:
            raise IstemError(ErrorCategory.BUSINESS_ERROR, f"I-STEM refused Create Booking: {_safe_message(body) or 'no message'}")
        request_id = _as_remote_id(body.get("new_service_id"))
        if success is not True or not request_id:
            raise IstemError(ErrorCategory.INVALID_RESPONSE, "Create Booking response has no valid new_service_id; the booking may exist.")
        fbr = body.get("fbr_no")
        fbr_no = str(fbr).strip() if isinstance(fbr, (str, int)) and not isinstance(fbr, bool) and str(fbr).strip() else None
        if fbr_no is not None and (not fbr_no.isalnum() or len(fbr_no) > 64):
            fbr_no = None
        if fbr_no is None:
            fbr_no = self._listed_fbr_no(request_id)
        return CreateBookingResult(request_id, fbr_no, existing="already exists" in _safe_message(body).lower())

    def _listed_fbr_no(self, request_id: str) -> str | None:
        """The FBR number of a just-created booking from Get All Bookings; None if the read fails or misses (the
        booking itself was created, so this must not turn the create into a failure)."""
        try:
            row = self.find_listed_booking(request_id, "Pending")
        except IstemError:
            logger.info("istem op=create_booking fbr_no lookup failed request_id=%s", request_id)
            return None
        return row["fbr_no"] if row else None

    def confirm_booking(self, payload: dict, request_id: str) -> ConfirmBookingResult:
        """POST api/confirm-booking. [SPEC] success: top-level confirmation_id and result.request_id; already
        confirmed (idempotent): confirmation_id with result {}. A success without a confirmation_id, or echoing another
        request_id, is treated as an uncertain outcome."""
        body = self._call("confirm_booking", "POST", contract.CONFIRM_BOOKING_PATH, data=payload)
        success = _is_success(body)
        if success is False:
            raise IstemError(ErrorCategory.BUSINESS_ERROR, f"I-STEM refused Confirm Booking: {_safe_message(body) or 'no message'}")
        result = body.get("result")
        if success is not True or not isinstance(result, dict):
            raise IstemError(ErrorCategory.INVALID_RESPONSE, "Confirm Booking returned an unexpected structure; it may have been applied.")
        echoed = _as_remote_id(result.get("request_id"))
        if result.get("request_id") is not None and echoed != str(request_id):
            raise IstemError(ErrorCategory.INVALID_RESPONSE, "Confirm Booking answered for a different request_id.")
        confirmation_id = _as_remote_id(body.get("confirmation_id")) or _as_remote_id(result.get("confirmation_id"))
        if not confirmation_id:
            # [STAGING] The answer never carries it: read it from Get All Bookings ("Slot Allocated"). A failed or
            # empty read keeps the documented "may have been applied" outcome, which reconciliation resolves later.
            try:
                row = self.find_listed_booking(str(request_id), contract.CONFIRM_BOOKING_STATUS)
            except IstemError:
                row = None
            confirmation_id = row["confirmation_id"] if row else None
        if not confirmation_id:
            raise IstemError(ErrorCategory.INVALID_RESPONSE, "Confirm Booking succeeded without a confirmation_id; it may have been applied.")
        return ConfirmBookingResult(str(request_id), confirmation_id, already_confirmed=echoed is None)

    def execute_fbr(self, payload: dict) -> ExecuteResult:
        """POST api/fbr-execute. [SPEC] success (also when already executed): status SUCCESS, payment_id, request_id,
        confirmation_id. The echoed identifiers must be the ones sent; otherwise the outcome is uncertain."""
        body = self._call("fbr_execute", "POST", contract.FBR_EXECUTE_PATH, data=payload)
        success = _is_success(body)
        message = _safe_message(body)
        if success is False:
            raise IstemError(ErrorCategory.BUSINESS_ERROR, f"I-STEM refused FBR Execute: {message or 'no message'}")
        if success is not True:
            raise IstemError(ErrorCategory.INVALID_RESPONSE, "FBR Execute returned an unrecognised response; it may have been applied.")
        request_id = _as_remote_id(body.get("request_id"))
        confirmation_id = _as_remote_id(body.get("confirmation_id"))
        payment_id = _as_remote_id(body.get("payment_id"))
        if request_id != str(payload.get("request_id")) or confirmation_id != str(payload.get("confirmation_id")):
            raise IstemError(ErrorCategory.INVALID_RESPONSE, "FBR Execute answered for a different request_id or confirmation_id.")
        if not payment_id:
            raise IstemError(ErrorCategory.INVALID_RESPONSE, "FBR Execute succeeded without a valid payment_id; it may have been applied.")
        return ExecuteResult(request_id, confirmation_id, payment_id, already_executed="already" in message.lower(), message=message)

    def get_bookings(self, **params) -> list[dict]:
        """GET api/get-bookings (read-only; [SPEC] fbr_no and status_code are both required). Rows carry request_id and
        fbr_status_code. "booking not found" is an empty list."""
        body = self._call("get_bookings", "GET", contract.GET_BOOKINGS_PATH, params={k: v for k, v in params.items() if v not in (None, "")})
        if _is_not_found(body):
            return []
        if _is_success(body) is not True or not isinstance(body.get("result"), list):
            raise IstemError(ErrorCategory.INVALID_RESPONSE, "Get Bookings returned an unexpected structure.")
        rows = [row for row in body["result"] if isinstance(row, dict)]
        if params.get("status_code") == contract.CONFIRM_BOOKING_STATUS:
            # [STAGING] Get Bookings rows carry no confirmation_id: it is added from Get All Bookings.
            for row in rows:
                request_id = _as_remote_id(row.get("request_id"))
                if request_id and not _as_remote_id(row.get("confirmation_id")):
                    listed = self.find_listed_booking(request_id, contract.CONFIRM_BOOKING_STATUS)
                    if listed and listed["confirmation_id"]:
                        row["confirmation_id"] = listed["confirmation_id"]
        return rows

    def get_fbr_details(self, fbr_no: str) -> list[dict]:
        """GET api/get-fbr-details?fbr_no= (read-only). Rows carry fbr_no, status_code and grand_tot. "booking not
        found" is an empty list.

        [STAGING] Get FBR Details answers "booking not found" for every FBR. The status is then read from Get Bookings,
        which needs the status as a filter: each documented status is tried and the one that finds the FBR is
        returned as ``{"fbr_no", "status_code"}`` (grand_tot is not available that way)."""
        body = self._call("get_fbr_details", "GET", contract.GET_FBR_DETAILS_PATH, params={"fbr_no": fbr_no})
        if _is_not_found(body):
            for status_code in contract.REMOTE_STATUS_CODES:
                rows = self.get_bookings(fbr_no=fbr_no, status_code=status_code)
                if any(str(r.get("fbr_no", "")).strip() == str(fbr_no) for r in rows):
                    return [{"fbr_no": str(fbr_no), "status_code": status_code}]
            return []
        if _is_success(body) is not True or not isinstance(body.get("result"), list):
            raise IstemError(ErrorCategory.INVALID_RESPONSE, "Get FBR Details returned an unexpected structure.")
        return [row for row in body["result"] if isinstance(row, dict)]

    def cancel_booking(self, request_id: str, *, cancelled_on: str, remarks: str, cancelled_by: str) -> str:
        """POST api/cancel-booking. [STAGING] verified: {"status": "Sucess", "message": "Done", "file_path": "cancelled
        Sucessfully"}. Not called by the service yet (booking-app hooks still need approval). Returns the message."""
        body = self._call("cancel_booking", "POST", contract.CANCEL_BOOKING_PATH, data={
            "request_id": str(request_id), "cancelled_date": cancelled_on, "remarks": remarks,
            "cancelled": "true", "cancelled_by": cancelled_by,
        })
        success = _is_success(body)
        if success is False:
            raise IstemError(ErrorCategory.BUSINESS_ERROR, f"I-STEM refused Cancel Booking: {_safe_message(body) or 'no message'}")
        if success is not True:
            raise IstemError(ErrorCategory.INVALID_RESPONSE, "Cancel Booking returned an unrecognised response; it may have been applied.")
        return _safe_message(body)

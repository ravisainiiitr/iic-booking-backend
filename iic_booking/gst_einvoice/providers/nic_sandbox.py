"""NIC e-invoice SANDBOX adapter (einv-apisandbox.nic.in), written to the NIC sandbox developer pages (v1.03 / v1.04):

1. Authenticate: POST with headers client_id, client_secret, Gstin; body {"Data": base64(RSA-PKCS1(NIC sandbox public
   key, base64(JSON{UserName, Password, AppKey, ForceRefreshAccessToken})))}. AppKey = 32 random bytes, base64.
   The answer carries AuthToken, Sek (the session key, AES-ECB encrypted with the AppKey) and TokenExpiry.
2. Later calls: headers client_id, client_secret, Gstin, user_name, AuthToken; request JSON AES-ECB encrypted with the
   decrypted Sek, base64, in {"Data": ...}; response Data decrypted the same way.

Every URL passes ``conf.assert_sandbox_url`` before a request is made, so a production IRP host is never contacted.
Points the NIC pages leave open (path prefixes, header spelling, AppKey length) are kept in PATHS / below and must be
confirmed with the sandbox credentials at onboarding; see docs/gst-einvoice/SANDBOX_ONBOARDING.md.
"""

from __future__ import annotations

import base64
import json
import os
import time
from datetime import datetime

import requests
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from django.core.cache import cache

from .. import conf
from ..payload import redact
from .base import ALL_OPERATIONS, IrpProvider, ProviderResult

TIMEOUT = (10, 30)  # connect, read
PATHS = {
    "auth": "/eivital/v1.04/auth",
    "gstin": "/eivital/v1.04/Master/gstin/{gstin}",
    "generate": "/eicore/v1.03/Invoice",
    "get_irn": "/eicore/v1.03/Invoice/irn/{irn}",
    "get_by_doc": "/eicore/v1.03/Invoice/irnbydocdetails",
    "cancel": "/eicore/v1.03/Invoice/Cancel",
}
TOKEN_CACHE_KEY = "gst_einvoice:nic_sandbox:session"


def _aes_ecb(key: bytes, data: bytes, *, encrypt: bool) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    if encrypt:
        padder = sym_padding.PKCS7(128).padder()
        enc = cipher.encryptor()
        return enc.update(padder.update(data) + padder.finalize()) + enc.finalize()
    dec = cipher.decryptor()
    raw = dec.update(data) + dec.finalize()
    unpadder = sym_padding.PKCS7(128).unpadder()
    return unpadder.update(raw) + unpadder.finalize()


class NicSandboxProvider(IrpProvider):
    name = "nic_sandbox"
    simulated = False
    label = "the NIC e-invoice sandbox"

    def __init__(self, session: requests.Session | None = None):
        self.http = session or requests.Session()
        self.base = conf.assert_sandbox_url(conf.sandbox_base_url())
        self.creds = conf.sandbox_credentials()

    def capabilities(self) -> set[str]:
        return set(ALL_OPERATIONS)

    # --- plumbing ----------------------------------------------------------------------------------------------------
    def _url(self, key: str, **kw) -> str:
        return conf.assert_sandbox_url(self.base + PATHS[key].format(**kw))

    def _headers(self, token: str | None = None) -> dict:
        h = {"Content-Type": "application/json", "client_id": self.creds["client_id"],
             "client_secret": self.creds["client_secret"], "Gstin": self.creds["gstin"]}
        if token:
            h.update({"user_name": self.creds["username"], "AuthToken": token})
        return h

    def _session(self, force: bool = False) -> tuple[str, bytes]:
        """(AuthToken, SEK bytes), cached until shortly before expiry."""
        cached = None if force else cache.get(TOKEN_CACHE_KEY)
        if cached:
            return cached["token"], base64.b64decode(cached["sek"])
        missing = conf.missing_sandbox_credentials()
        if missing:
            raise RuntimeError("Sandbox credentials missing: " + ", ".join(missing))
        app_key = os.urandom(32)
        body = json.dumps({"UserName": self.creds["username"], "Password": self.creds["password"],
                           "AppKey": base64.b64encode(app_key).decode(), "ForceRefreshAccessToken": force})
        public_key = serialization.load_pem_public_key(self.creds["public_key_pem"].encode())
        encrypted = public_key.encrypt(base64.b64encode(body.encode()), asym_padding.PKCS1v15())
        resp = self.http.post(self._url("auth"), headers=self._headers(),
                              json={"Data": base64.b64encode(encrypted).decode()}, timeout=TIMEOUT)
        data = resp.json()
        if str(data.get("Status")) != "1":
            raise RuntimeError(f"Sandbox authentication refused: {self._error_text(data)}")
        inner = data["Data"]
        sek = _aes_ecb(app_key, base64.b64decode(inner["Sek"]), encrypt=False)
        try:
            expiry = datetime.strptime(inner.get("TokenExpiry", ""), "%Y-%m-%d %H:%M:%S")
            ttl = max(60, int((expiry - datetime.now()).total_seconds()) - 600)
        except ValueError:
            ttl = 50 * 60  # sandbox tokens last 60 minutes
        cache.set(TOKEN_CACHE_KEY, {"token": inner["AuthToken"], "sek": base64.b64encode(sek).decode()}, ttl)
        return inner["AuthToken"], sek

    @staticmethod
    def _error_text(data: dict) -> str:
        details = data.get("ErrorDetails")
        if isinstance(details, str):
            try:
                details = json.loads(base64.b64decode(details))
            except (ValueError, TypeError):
                return details[:300]
        if isinstance(details, list) and details:
            first = details[0]
            return f"{first.get('ErrorCode', '')} {first.get('ErrorMessage', '')}".strip()
        return str(data.get("error") or data.get("message") or "no error detail")[:300]

    def _call(self, method: str, url: str, *, body: dict | None = None, request_log: dict) -> ProviderResult:
        started = time.monotonic()
        try:
            token, sek = self._session()
        except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
            return ProviderResult(False, error_message=f"Authentication failed: {exc}", request=request_log)
        payload = None
        if body is not None:
            payload = {"Data": base64.b64encode(_aes_ecb(sek, json.dumps(body).encode(), encrypt=True)).decode()}
        try:
            resp = self.http.request(method, url, headers=self._headers(token), json=payload, timeout=TIMEOUT)
        except (requests.Timeout, requests.ConnectionError) as exc:
            # Sent, but no answer: it may have been processed. The caller must look it up before repeating.
            return ProviderResult(False, uncertain=True, error_message=f"No answer from the sandbox ({type(exc).__name__}).",
                                  request=request_log, duration_ms=int((time.monotonic() - started) * 1000))
        ms = int((time.monotonic() - started) * 1000)
        try:
            data = resp.json()
        except ValueError:
            return ProviderResult(False, uncertain=resp.status_code >= 500, http_status=resp.status_code,
                                  error_message=f"Sandbox answered HTTP {resp.status_code} without JSON.", request=request_log, duration_ms=ms)
        if str(data.get("Status")) == "1" and data.get("Data"):
            decrypted = json.loads(_aes_ecb(sek, base64.b64decode(data["Data"]), encrypt=False))
            return ProviderResult(True, data=decrypted, http_status=resp.status_code, request=request_log,
                                  response=redact(decrypted), duration_ms=ms)
        message = self._error_text(data)
        code = message.split(" ", 1)[0] if message[:1].isdigit() else ""
        info = data.get("InfoDtls") or []
        return ProviderResult(False, error_code=code, error_message=message, http_status=resp.status_code,
                              data={"info": info}, request=request_log, response=redact({"Status": data.get("Status"), "InfoDtls": info}),
                              uncertain=resp.status_code >= 500, duration_ms=ms)

    # --- operations --------------------------------------------------------------------------------------------------
    def gstin_details(self, gstin: str) -> ProviderResult:
        return self._call("GET", self._url("gstin", gstin=gstin), request_log={"operation": "Get GSTIN details", "Gstin": gstin})

    def generate_irn(self, payload: dict, *, notes: str = "") -> ProviderResult:
        result = self._call("POST", self._url("generate"), body=payload, request_log=redact(payload))
        if not result.ok and result.error_code == "2150":
            for item in result.data.get("info") or []:
                if isinstance(item, dict) and isinstance(item.get("Desc"), dict):
                    result.data["duplicate"] = item["Desc"]
        return result

    def get_by_document(self, doc_type: str, doc_number: str, doc_date: str) -> ProviderResult:
        from urllib.parse import urlencode

        url = self._url("get_by_doc") + "?" + urlencode({"doctype": doc_type, "docnum": doc_number, "docdate": doc_date})
        return self._call("GET", url, request_log={"operation": "Get IRN by document", "docnum": doc_number, "docdate": doc_date})

    def get_irn(self, irn: str) -> ProviderResult:
        return self._call("GET", self._url("get_irn", irn=irn), request_log={"operation": "Get e-invoice by IRN", "Irn": irn})

    def cancel_irn(self, irn: str, reason: str, remarks: str) -> ProviderResult:
        body = {"Irn": irn, "CnlRsn": reason, "CnlRem": remarks[:100]}
        return self._call("POST", self._url("cancel"), body=body, request_log=body)

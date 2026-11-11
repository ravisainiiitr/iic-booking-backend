"""The NIC sandbox adapter against a fake NIC sandbox (no network): authentication with the sandbox public key,
SEK decryption, encrypted Generate IRN, the configurable header spelling / AppKey length / path prefixes, and that
secrets never reach the attempt log."""

import base64
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from iic_booking.gst_einvoice.providers.nic_sandbox import NicSandboxProvider, _aes_ecb

pytestmark = pytest.mark.django_db


class FakeResponse:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data


class FakeNic:
    """Answers like einv-apisandbox.nic.in, from its documented protocol."""

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.sek = b"S" * 32
        self.calls = []
        self.app_key = None

    def public_pem(self):
        return self.key.public_key().public_bytes(serialization.Encoding.PEM,
                                                  serialization.PublicFormat.SubjectPublicKeyInfo).decode()

    def post(self, url, headers=None, json=None, timeout=None):
        return self.request("POST", url, headers=headers, json=json, timeout=timeout)

    def request(self, method, url, headers=None, json=None, timeout=None):
        import json as js

        self.calls.append((method, url, dict(headers or {})))
        if url.endswith("/auth"):
            inner = js.loads(base64.b64decode(self.key.decrypt(base64.b64decode(json["Data"]), padding.PKCS1v15())))
            assert inner["UserName"] == "sandbox-user" and inner["Password"] == "sandbox-pass"
            self.app_key = base64.b64decode(inner["AppKey"])
            sek = base64.b64encode(_aes_ecb(self.app_key, self.sek, encrypt=True)).decode()
            return FakeResponse({"Status": 1, "Data": {"AuthToken": "tok-123", "Sek": sek,
                                                       "TokenExpiry": "2099-01-01 00:00:00"}})
        assert headers["AuthToken"] == "tok-123"
        body = js.loads(_aes_ecb(self.sek, base64.b64decode(json["Data"]), encrypt=False))
        answer = {"AckNo": 112010000000123, "AckDt": "2026-11-11 10:00:00", "Irn": "a" * 64, "Status": "ACT",
                  "SignedInvoice": "eyJ.signed.inv", "SignedQRCode": "eyJ.signed.qr", "DocNo": body["DocDtls"]["No"]}
        return FakeResponse({"Status": 1, "Data": base64.b64encode(
            _aes_ecb(self.sek, js.dumps(answer).encode(), encrypt=True)).decode()})


@pytest.fixture
def nic(settings):
    fake = FakeNic()
    settings.GST_EINVOICE_PROVIDER = "nic_sandbox"
    settings.GST_EINVOICE_SANDBOX_CLIENT_ID = "sandbox-client"
    settings.GST_EINVOICE_SANDBOX_CLIENT_SECRET = "sandbox-secret"
    settings.GST_EINVOICE_SANDBOX_USERNAME = "sandbox-user"
    settings.GST_EINVOICE_SANDBOX_PASSWORD = "sandbox-pass"
    settings.GST_EINVOICE_SANDBOX_GSTIN = "05AAALI0033R1Z5"
    settings.GST_EINVOICE_SANDBOX_PUBLIC_KEY = fake.public_pem()
    return fake


PAYLOAD = {"Version": "1.1", "DocDtls": {"Typ": "INV", "No": "EBP/26-27/0001", "Dt": "11/11/2026"},
           "SellerDtls": {"Gstin": "05AAALI0033R1Z5"}, "BuyerDtls": {"Gstin": "07AAACX0000X1Z2"},
           "ValDtls": {"TotInvVal": 10620}, "ItemList": [{"HsnCd": "998397"}]}


def test_generate_irn_round_trip(nic):
    result = NicSandboxProvider(session=nic).generate_irn(PAYLOAD)
    assert result.ok and result.data["Irn"] == "a" * 64 and result.data["DocNo"] == "EBP/26-27/0001"
    auth_url = nic.calls[0][1]
    assert auth_url == "https://einv-apisandbox.nic.in/eivital/v1.04/auth"
    assert nic.calls[1][1] == "https://einv-apisandbox.nic.in/eicore/v1.03/Invoice"
    assert nic.calls[0][2]["client_id"] == "sandbox-client" and len(nic.app_key) == 32
    assert "sandbox-pass" not in json.dumps(result.request) and "sandbox-pass" not in json.dumps(result.response)
    assert result.response["SignedQRCode"].startswith("<signed JWT")


def test_open_points_follow_configuration(nic, settings):
    from django.core.cache import cache

    cache.clear()
    settings.GST_EINVOICE_SANDBOX_CLIENT_ID_HEADER = "client-id"
    settings.GST_EINVOICE_SANDBOX_APPKEY_BYTES = "16"
    settings.GST_EINVOICE_SANDBOX_CORE_PREFIX = "/eicore/v1.04"
    assert NicSandboxProvider(session=nic).generate_irn(PAYLOAD).ok
    headers = nic.calls[0][2]
    assert headers["client-id"] == "sandbox-client" and headers["client-secret"] == "sandbox-secret"
    assert "client_id" not in headers and len(nic.app_key) == 16
    assert nic.calls[1][1].endswith("/eicore/v1.04/Invoice")


def test_missing_credentials_are_reported_not_sent(settings):
    settings.GST_EINVOICE_PROVIDER = "nic_sandbox"
    for name in ("CLIENT_ID", "CLIENT_SECRET", "USERNAME", "PASSWORD", "GSTIN", "PUBLIC_KEY"):
        setattr(settings, f"GST_EINVOICE_SANDBOX_{name}", "")
    from django.core.cache import cache

    cache.clear()
    sent = []

    class NoNetwork:
        def post(self, *a, **k):
            sent.append(a)

        request = post

    result = NicSandboxProvider(session=NoNetwork()).generate_irn(PAYLOAD)
    assert not result.ok and "credentials missing" in result.error_message and sent == []
    from iic_booking.gst_einvoice import conf

    assert "GST_EINVOICE_SANDBOX_PASSWORD" in conf.status()["problem"]

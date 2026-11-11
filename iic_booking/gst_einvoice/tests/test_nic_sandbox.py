"""The NIC sandbox adapter against a fake NIC sandbox (no network): authentication with the sandbox public key,
SEK decryption, encrypted Generate IRN, the configurable header spelling / AppKey length / path prefixes, and that
secrets never reach the attempt log.

The fake never decrypts with the RSA private key in this process: on some Windows builds of ``cryptography`` an RSA
private-key operation corrupts the process's floating-point state (the portal itself only ever uses NIC's public
key). The AppKey is captured through the adapter's seam instead, and the encrypted credentials are checked once in a
child process."""

import base64
import json
import subprocess
import sys

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

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
        self.auth_blob = ""

    def public_pem(self):
        return self.key.public_key().public_bytes(serialization.Encoding.PEM,
                                                  serialization.PublicFormat.SubjectPublicKeyInfo).decode()

    def post(self, url, headers=None, json=None, timeout=None):
        return self.request("POST", url, headers=headers, json=json, timeout=timeout)

    def request(self, method, url, headers=None, json=None, timeout=None):
        import json as js

        self.calls.append((method, url, dict(headers or {})))
        if url.endswith("/auth"):
            self.auth_blob = json["Data"]
            assert len(base64.b64decode(self.auth_blob)) == 256  # RSA-2048 ciphertext
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
def nic(settings, monkeypatch):
    fake = FakeNic()

    def capture(length):
        fake.app_key = bytes(range(length))
        return fake.app_key

    monkeypatch.setattr(NicSandboxProvider, "_new_app_key", staticmethod(capture))
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


def test_credentials_are_encrypted_with_the_sandbox_key(nic, tmp_path):
    NicSandboxProvider(session=nic).generate_irn(PAYLOAD)
    key_file = tmp_path / "nic.pem"
    key_file.write_bytes(nic.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
    code = ("import base64,json,sys;from cryptography.hazmat.primitives import serialization;"
            "from cryptography.hazmat.primitives.asymmetric import padding;"
            "k=serialization.load_pem_private_key(open(sys.argv[1],'rb').read(),None);"
            "d=json.loads(base64.b64decode(k.decrypt(base64.b64decode(sys.argv[2]),padding.PKCS1v15())));"
            "print(d['UserName'],d['Password'],len(base64.b64decode(d['AppKey'])),d['ForceRefreshAccessToken'])")
    out = subprocess.run([sys.executable, "-c", code, str(key_file), nic.auth_blob], capture_output=True, text=True,
                         timeout=60, stdin=subprocess.DEVNULL)
    assert out.stdout.split() == ["sandbox-user", "sandbox-pass", "32", "False"], out.stderr


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


def test_full_b2b_flow_through_the_nic_adapter(nic, monkeypatch):
    """The whole B2B path with provider nic_sandbox (fake NIC answering): IRN of the IRP's form, not simulated."""
    from django.core.cache import cache

    from iic_booking.gst_einvoice import service
    from iic_booking.gst_einvoice.providers import nic_sandbox

    from .conftest import main_admin

    cache.clear()
    monkeypatch.setattr(nic_sandbox.requests, "Session", lambda: nic)
    inv = service.create_demo({"legal_name": "Demo Analytics Pvt Ltd", "gstin": "07AAACX0000X1Z2", "address1": "12 Ring Road",
                               "location": "New Delhi", "pincode": "110001", "amount": "9000"}, main_admin())
    assert inv.status == "irn_generated" and inv.provider == "nic_sandbox" and not inv.simulated
    assert inv.irn == "a" * 64 and inv.signed_qr == "eyJ.signed.qr" and inv.ack_number == "112010000000123"
    with inv.pdf.open("rb") as fh:
        assert fh.read().startswith(b"%PDF-")
    assert inv.attempts.get().outcome == "success"

"""Token detection without a real token: a fake Windows folder tree with fake PE files, and a fake PKCS#11 layer."""

import datetime as dt
import os
import struct
import sys

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sric_signer  # noqa: E402
import sric_token  # noqa: E402


def pe(path, machine):
    """A minimal file with a DOS + PE header for ``machine`` (0x14C = 32-bit, 0x8664 = 64-bit)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    head = bytearray(512)
    head[:2] = b"MZ"
    struct.pack_into("<I", head, 0x3C, 0x80)
    head[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", head, 0x84, machine)
    with open(path, "wb") as fh:
        fh.write(bytes(head))
    return path


@pytest.fixture
def windows(tmp_path, monkeypatch):
    root = tmp_path
    sys32, wow, pf, pf86 = (root / "Windows" / "System32", root / "Windows" / "SysWOW64", root / "Program Files",
                            root / "Program Files (x86)")
    pe(str(sys32 / "eTPKCS11.dll"), 0x8664)                       # SafeNet, 64-bit
    pe(str(wow / "eps2003csp11.dll"), 0x014C)                     # ePass2003, 32-bit
    pe(str(pf / "Gemalto" / "IDGo 800" / "IDPrimePKCS11.dll"), 0x8664)
    pe(str(pf86 / "Watchdata" / "ProxKey" / "x86" / "SignatureP11.dll"), 0x014C)
    pe(str(pf / "SomeGame" / "eTPKCS11.dll"), 0x8664)             # not a vendor folder: ignored
    pe(str(sys32 / "kernel32.dll"), 0x8664)                       # not a token driver: ignored
    (sys32 / "WDPKCS.dll").write_bytes(b"not a PE file")          # unreadable architecture: listed, not usable
    env = {"SystemRoot": str(root / "Windows"), "ProgramFiles": str(pf), "ProgramW6432": str(pf),
           "ProgramFiles(x86)": str(pf86)}
    monkeypatch.setattr(sric_token, "own_arch", lambda: "64-bit")
    return env


def test_pe_architecture(tmp_path):
    assert sric_token.dll_arch(pe(str(tmp_path / "a.dll"), 0x8664)) == "64-bit"
    assert sric_token.dll_arch(pe(str(tmp_path / "b.dll"), 0x014C)) == "32-bit"
    (tmp_path / "c.dll").write_bytes(b"MZ" + b"\0" * 10)
    assert sric_token.dll_arch(str(tmp_path / "c.dll")) == ""
    assert sric_token.dll_arch(str(tmp_path / "missing.dll")) == ""


def test_search_roots_cover_system_and_program_files(windows):
    roots = sric_token.search_roots(windows)
    names = [os.path.basename(r) for r in roots]
    assert names[:2] == ["System32", "SysWOW64"]
    assert "Program Files" in names and "Program Files (x86)" in names
    assert len(roots) == len({os.path.normcase(r) for r in roots})  # ProgramW6432 == ProgramFiles: listed once


def test_finds_known_drivers_and_matches_architecture(windows):
    drivers = sric_token.find_drivers(sric_token.search_roots(windows))
    found = {(d.vendor, os.path.basename(d.path), d.arch, d.usable) for d in drivers}
    assert ("SafeNet / eToken", "eTPKCS11.dll", "64-bit", True) in found
    assert ("Gemalto / Thales IDPrime", "IDPrimePKCS11.dll", "64-bit", True) in found
    assert ("ePass2003", "eps2003csp11.dll", "32-bit", False) in found
    assert ("ProxKey / Watchdata", "SignatureP11.dll", "32-bit", False) in found
    assert ("ProxKey / Watchdata", "WDPKCS.dll", "", False) in found
    assert not any("SomeGame" in d.path for d in drivers)  # only vendor folders under Program Files
    assert [d.usable for d in drivers] == sorted((d.usable for d in drivers), reverse=True)  # usable first


def test_a_32_bit_signer_uses_32_bit_drivers(windows, monkeypatch):
    monkeypatch.setattr(sric_token, "own_arch", lambda: "32-bit")
    usable = {os.path.basename(d.path) for d in sric_token.find_drivers(sric_token.search_roots(windows)) if d.usable}
    assert usable == {"eps2003csp11.dll", "SignatureP11.dll"}


def test_every_requested_vendor_is_known():
    names = {n.lower() for _v, ns in sric_token.KNOWN_DRIVERS for n in ns}
    for dll in ("etpkcs11.dll", "eps2003csp11.dll", "eps2003csp11v2.dll", "signaturep11.dll", "wdpkcs.dll",
                "mtoken_pkcs11.dll", "cryptoida_pkcs11.dll", "idprimepkcs11.dll", "gclib.dll", "hypersecurep11.dll",
                "trustkeyp11.dll", "bit4xpki.dll", "aetpkss1.dll"):
        assert dll in names
    assert any(v == "Longmai" for v, _n in sric_token.KNOWN_DRIVERS)


# --- fake PKCS#11 layer ---------------------------------------------------------------------------------------------

def _cert_der(cn="SURESH KUMAR", issuer="Capricorn Sub CA for Individual DSC 2022"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
            .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer)]))
            .public_key(key.public_key()).serial_number(7).not_valid_before(now)
            .not_valid_after(now + dt.timedelta(days=700)).sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.DER)


class FakeObj(dict):
    pass


class FakeSession:
    def __init__(self, objects):
        self.objects = objects

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_objects(self, attrs):
        return list(self.objects)


class FakeToken:
    def __init__(self, label, objects, manufacturer="SafeNet, Inc.", serial=b"0A1B2C"):
        self.label, self.manufacturer_id, self.serial, self.objects = label, manufacturer, serial, objects

    def open(self):
        return FakeSession(self.objects)


class FakeSlot:
    def __init__(self, slot_id, token):
        self.slot_id, self.token = slot_id, token

    def get_token(self):
        return self.token


class FakeLib:
    def __init__(self, slots):
        self.slots = slots

    def get_slots(self, token_present=False):
        assert token_present
        return self.slots


def _drivers(tmp_path):
    return [sric_token.Driver("SafeNet / eToken", str(tmp_path / "eTPKCS11.dll"), "64-bit", True),
            sric_token.Driver("ePass2003", str(tmp_path / "eps2003csp11.dll"), "32-bit", False)]


def test_lists_plugged_in_tokens_with_their_certificates(tmp_path):
    from pkcs11 import Attribute

    cert = FakeObj({Attribute.VALUE: _cert_der(), Attribute.ID: b"\x01\x02", Attribute.LABEL: "Signing"})
    lib = FakeLib([FakeSlot(1, FakeToken("SURESH KUMAR ", [cert]))])
    calls = []
    tokens = sric_token.list_tokens(_drivers(tmp_path), loader=lambda p: calls.append(p) or lib)
    assert calls == [str(tmp_path / "eTPKCS11.dll")]  # the 32-bit driver is never loaded by a 64-bit signer
    [t] = tokens
    assert (t.label, t.manufacturer, t.serial, t.slot_id) == ("SURESH KUMAR", "SafeNet, Inc.", "0A1B2C", 1)
    [c] = t.certificates
    assert c.subject == "SURESH KUMAR" and c.issuer.startswith("Capricorn") and c.cert_id == "0102"
    assert "SURESH KUMAR" in t.title


def test_uninitialised_slots_are_not_offered(tmp_path):
    from pkcs11 import Attribute

    cert = FakeObj({Attribute.VALUE: _cert_der(), Attribute.ID: b"", Attribute.LABEL: "Signing"})
    ready, empty = FakeToken("DSC", [cert]), FakeToken("", [])
    ready.flags, empty.flags = 0x400, 0x0
    lib = FakeLib([FakeSlot(0, ready), FakeSlot(1, empty)])
    assert [t.label for t in sric_token.list_tokens(_drivers(tmp_path), loader=lambda p: lib)] == ["DSC"]


def test_no_token_plugged_in_and_broken_driver(tmp_path):
    assert sric_token.list_tokens(_drivers(tmp_path), loader=lambda p: FakeLib([])) == []

    def broken(path):
        raise OSError("[WinError 193] %1 is not a valid Win32 application")

    [t] = sric_token.list_tokens(_drivers(tmp_path), loader=broken)
    assert "could not be loaded" in t.error and "architecture" in t.error


def test_friendly_messages_for_pin_lock_and_absence():
    from pkcs11 import exceptions as e

    assert "PIN is wrong" in sric_token.friendly_error(e.PinIncorrect())
    assert "locked" in sric_token.friendly_error(e.PinLocked())
    assert "No DSC token is plugged in" in sric_token.friendly_error(e.TokenNotPresent())
    assert "removed" in sric_token.friendly_error(e.DeviceRemoved())
    assert "PIN is wrong" in sric_token.friendly_error(RuntimeError("CKR_PIN_INCORRECT"))


@pytest.mark.parametrize("error, text", [("PinIncorrect", "PIN is wrong"), ("PinLocked", "locked"),
                                         ("TokenNotPresent", "No DSC token")])
def test_open_signer_reports_token_errors(monkeypatch, error, text):
    from pkcs11 import exceptions as e
    from pyhanko.sign import pkcs11 as ph

    def fail(*a, **k):
        try:
            raise getattr(e, error)()
        except Exception as inner:
            raise RuntimeError("pyHanko could not open the session") from inner

    monkeypatch.setattr(ph, "open_pkcs11_session", fail)
    with pytest.raises(sric_signer.SignerError) as err:
        sric_signer.open_signer(pkcs11_lib="eTPKCS11.dll", token_label="T", pin="1234")
    assert text in str(err.value)


def test_open_signer_passes_token_and_certificate(monkeypatch):
    from pyhanko.sign import pkcs11 as ph

    seen = {}

    class Session:
        def close(self):
            seen["closed"] = True

    def open_session(lib, token_criteria=None, user_pin=None):
        seen.update(lib=lib, label=token_criteria.label, serial=token_criteria.serial, pin=user_pin)
        return Session()

    def signer(session, cert_id=None, key_id=None, **kw):
        seen.update(cert_id=cert_id, key_id=key_id)
        return "signer"

    monkeypatch.setattr(ph, "open_pkcs11_session", open_session)
    monkeypatch.setattr(ph, "PKCS11Signer", signer)
    got, session = sric_signer.open_signer(pkcs11_lib="x.dll", token_label="DSC", token_serial="0A1B", cert_id="0102",
                                           pin="9999")
    assert got == "signer"
    assert seen == {"lib": "x.dll", "label": "DSC", "serial": b"0A1B", "pin": "9999", "cert_id": b"\x01\x02",
                    "key_id": b"\x01\x02"}
    with pytest.raises(sric_signer.SignerError):
        sric_signer.open_signer(pkcs11_lib="x.dll", pin="")


def test_signing_stops_after_a_pin_error():
    class Portal:
        def __init__(self):
            self.downloads = 0

        def prepared_pdf(self, item):
            self.downloads += 1
            return b"%PDF-"

    portal = Portal()
    items = [{"invoice_number": f"EBP/26-27/000{i}", "buyer": "B", "total": "1"} for i in range(3)]
    original = sric_signer.sign_pdf
    sric_signer.sign_pdf = lambda *a, **k: (_ for _ in ()).throw(sric_signer.SignerError("The PIN is wrong."))
    try:
        done, failed = sric_signer.sign_items(portal, items, object(), box=(0, 0, 1, 1), field_name="F", log=lambda m: None)
    finally:
        sric_signer.sign_pdf = original
    assert (done, failed, portal.downloads) == (0, 1, 1)  # a wrong PIN is not retried on the next invoices


def test_config_never_keeps_the_pin(tmp_path, monkeypatch):
    monkeypatch.setattr(sric_signer, "CONFIG", str(tmp_path / "c" / "config.json"))
    sric_signer.save_config({"pkcs11_lib": "x.dll", "pin": "1234", "token_label": "DSC"})
    assert sric_signer.load_config() == {"pkcs11_lib": "x.dll", "token_label": "DSC"}


def test_self_test_runs():
    report = sric_signer.self_test()
    assert report.startswith("SELF-TEST OK") and "pyHanko signed a test PDF" in report

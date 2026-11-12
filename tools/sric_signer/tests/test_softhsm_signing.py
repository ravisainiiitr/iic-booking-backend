"""A real PKCS#11 signing run against SoftHSM2 (a software token), through exactly the code path a DSC token uses:
detection, ``open_signer`` (PIN login), ``sign_pdf`` and the portal's own signature check (``gst_einvoice.dsc``).

Runs when SoftHSM2 is installed (``softhsm2-util`` on PATH and its module found, or ``SOFTHSM2_MODULE`` set);
skipped otherwise. A real USB DSC token is not exercised here.
"""

import datetime as dt
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import sric_signer  # noqa: E402
import sric_token  # noqa: E402

MODULE_CANDIDATES = ("/usr/lib/softhsm/libsofthsm2.so", "/usr/lib/x86_64-linux-gnu/softhsm/libsofthsm2.so",
                     "/usr/local/lib/softhsm/libsofthsm2.so", r"C:\SoftHSM2\lib\softhsm2-x64.dll")
MODULE = os.environ.get("SOFTHSM2_MODULE") or next((p for p in MODULE_CANDIDATES if os.path.exists(p)), "")
UTIL = shutil.which("softhsm2-util")
pytestmark = pytest.mark.skipif(not (MODULE and UTIL), reason="SoftHSM2 is not installed: real PKCS#11 signing "
                                                              "is not verified in this run")
LABEL, PIN, CERT_ID = "SRIC TEST DSC", "123456", b"\x01"


@pytest.fixture(scope="module")
def token(tmp_path_factory):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from pkcs11 import Attribute, CertificateType, ObjectClass
    from pkcs11.util.rsa import decode_rsa_private_key

    tokens = tmp_path_factory.mktemp("softhsm-tokens")
    conf = tokens.parent / "softhsm2.conf"
    conf.write_text(f"directories.tokendir = {tokens}\nobjectstore.backend = file\n")
    os.environ["SOFTHSM2_CONF"] = str(conf)
    subprocess.run([UTIL, "--init-token", "--free", "--label", LABEL, "--pin", PIN, "--so-pin", "12345678"],
                   check=True, capture_output=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "TEST DSC - SoftHSM Signatory")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=30)).sign(key, hashes.SHA256()))
    import pkcs11

    lib = pkcs11.lib(MODULE)
    with lib.get_token(token_label=LABEL).open(rw=True, user_pin=PIN) as session:
        attrs = decode_rsa_private_key(key.private_bytes(serialization.Encoding.DER,
                                                         serialization.PrivateFormat.TraditionalOpenSSL,
                                                         serialization.NoEncryption()))
        attrs.update({Attribute.ID: CERT_ID, Attribute.LABEL: "dsc", Attribute.TOKEN: True, Attribute.SIGN: True,
                      Attribute.PRIVATE: True, Attribute.SENSITIVE: True})
        session.create_object(attrs)
        session.create_object({
            Attribute.CLASS: ObjectClass.CERTIFICATE, Attribute.CERTIFICATE_TYPE: CertificateType.X_509,
            Attribute.VALUE: cert.public_bytes(serialization.Encoding.DER), Attribute.ID: CERT_ID,
            Attribute.LABEL: "dsc", Attribute.TOKEN: True,
            Attribute.SUBJECT: cert.subject.public_bytes(),
        })
    return cert


def _driver():
    return sric_token.Driver("SoftHSM2", MODULE, sric_token.own_arch(), usable=True)


def _pdf():
    from pypdf import PdfWriter

    w = PdfWriter()
    w.add_blank_page(595, 842)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def _portal_dsc():
    """The portal's verifier (iic_booking/gst_einvoice/dsc.py), loaded without Django."""
    fake_tz = types.ModuleType("django.utils.timezone")
    fake_tz.now = lambda: dt.datetime.now(dt.timezone.utc)
    sys.modules.setdefault("django", types.ModuleType("django"))
    sys.modules.setdefault("django.utils", types.ModuleType("django.utils"))
    sys.modules["django.utils.timezone"] = fake_tz
    sys.modules["django.utils"].timezone = fake_tz
    path = os.path.join(HERE, "..", "..", "..", "iic_booking", "gst_einvoice", "dsc.py")
    spec = importlib.util.spec_from_file_location("portal_dsc", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["portal_dsc"] = mod  # dataclasses look the module up while the class is being made
    spec.loader.exec_module(mod)
    return mod


def test_detects_the_token_and_its_certificate(token):
    [info] = sric_token.list_tokens([_driver()])
    assert info.label == LABEL and not info.error
    [cert] = info.certificates
    assert cert.subject == "TEST DSC - SoftHSM Signatory" and cert.cert_id == CERT_ID.hex()


def test_wrong_pin_is_reported_plainly(token):
    with pytest.raises(sric_signer.SignerError) as err:
        sric_signer.open_signer(pkcs11_lib=MODULE, token_label=LABEL, cert_id=CERT_ID.hex(), pin="000000")
    assert "PIN is wrong" in str(err.value)


def test_signs_through_pkcs11_and_the_portal_accepts_it(token):
    from cryptography.hazmat.primitives import serialization
    from pyhanko.keys import load_cert_from_pemder  # noqa: F401 - imported to fail early if pyHanko is incomplete
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.validation import validate_pdf_signature
    from pyhanko_certvalidator import ValidationContext
    from asn1crypto import x509 as ax

    prepared = _pdf()
    box = (338.0, 96.0, 553.0, 156.0)
    signer, session = sric_signer.open_signer(pkcs11_lib=MODULE, token_label=LABEL, cert_id=CERT_ID.hex(), pin=PIN)
    try:
        signed = sric_signer.sign_pdf(prepared, signer, box=box, field_name="SRIC_Signatory")
    finally:
        session.close()
    trust = ax.Certificate.load(token.public_bytes(serialization.Encoding.DER))
    status = validate_pdf_signature(PdfFileReader(io.BytesIO(signed)).embedded_signatures[0],
                                    ValidationContext(trust_roots=[trust]))
    assert status.intact and status.valid and status.coverage.name == "ENTIRE_FILE"

    dsc = _portal_dsc()
    row = types.SimpleNamespace(
        pk=1, is_active=True, sha256_fingerprint="softhsm", subject="TEST DSC - SoftHSM Signatory",
        certificate_pem=token.public_bytes(serialization.Encoding.PEM).decode(),
        not_before=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1),
        not_after=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1))
    assert dsc.verify_signed_pdf(prepared, signed, [row]).certificate_id == 1
    tampered = bytearray(signed)
    tampered[len(prepared) // 2] ^= 1
    with pytest.raises(dsc.SignatureRejected):
        dsc.verify_signed_pdf(prepared, bytes(tampered), [row])

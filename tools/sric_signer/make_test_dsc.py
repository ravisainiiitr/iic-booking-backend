"""Makes a TEST DSC for the sandbox demonstration: a throw-away test CA and a signatory certificate in a .p12 file
(protected by a PIN you choose), plus the public certificate (.cer) to register in the portal.

It is NOT a DSC: no licensed Certifying Authority issued it, so Adobe Reader shows its signature as "validity
unknown" (the integrity check still works: change one figure and the signature turns invalid). For real invoices
SRIC's signatory uses their Class 3 DSC token.

    python make_test_dsc.py --out ./test-dsc --name "SRIC Demo Signatory"
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import os

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COUNTRY_NAME, "IN"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "IIT Roorkee (SANDBOX TEST ONLY)"),
                      x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def make(out: str, name: str, pin: str, days: int = 365) -> tuple[str, str]:
    os.makedirs(out, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = _name("SANDBOX TEST CA - not a licensed Certifying Authority")
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
          .not_valid_after(now + dt.timedelta(days=days))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .sign(ca_key, hashes.SHA256()))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = (x509.CertificateBuilder().subject_name(_name(f"TEST DSC - {name}")).issuer_name(ca_name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=True, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.EMAIL_PROTECTION]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    p12 = pkcs12.serialize_key_and_certificates(
        b"test-dsc", key, cert, [ca], serialization.BestAvailableEncryption(pin.encode()))
    p12_path, cer_path = os.path.join(out, "test-dsc.p12"), os.path.join(out, "test-dsc.cer")
    with open(p12_path, "wb") as fh:
        fh.write(p12)
    with open(cer_path, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    return p12_path, cer_path


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="test-dsc")
    ap.add_argument("--name", default="SRIC Demo Signatory")
    args = ap.parse_args()
    pin = os.environ.get("SRIC_SIGNER_TEST_PIN") or getpass.getpass("PIN for the test DSC file: ")
    p12, cer = make(args.out, args.name, pin)
    print(f"Test DSC (keep private): {p12}\nCertificate to register in the portal: {cer}")

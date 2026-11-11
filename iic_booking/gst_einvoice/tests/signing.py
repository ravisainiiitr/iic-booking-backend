"""Test-only PDF signing, standing in for the signer utility (pyHanko + DSC token) inside the backend test suite:
a test key pair and certificate, and a detached CMS signature added to a PDF as one incremental update."""

from __future__ import annotations

import datetime as dt
import io
import re

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import NameOID

from iic_booking.gst_einvoice import dsc


def test_identity(cn="TEST DSC - Pytest Signatory", days=30):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=days)).sign(key, hashes.SHA256()))
    return key, cert


def cert_pem(cert) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def sign(data: bytes, key, cert, *, box=dsc.SIGNATURE_BOX, extra_annotation=False, change_content=False) -> bytes:
    from pypdf import PdfWriter
    from pypdf.generic import (
        ArrayObject,
        ByteStringObject,
        DecodedStreamObject,
        DictionaryObject,
        FloatObject,
        NameObject,
        NumberObject,
        TextStringObject,
    )

    writer = PdfWriter(io.BytesIO(data), incremental=True)
    page = writer.pages[0]
    sig = DictionaryObject({
        NameObject("/Type"): NameObject("/Sig"), NameObject("/Filter"): NameObject("/Adobe.PPKLite"),
        NameObject("/SubFilter"): NameObject("/adbe.pkcs7.detached"),
        NameObject("/ByteRange"): ArrayObject([NumberObject(0), NumberObject(9999999999), NumberObject(9999999999),
                                               NumberObject(9999999999)]),
        NameObject("/Contents"): ByteStringObject(b"\x00" * 8192),
    })
    sig_ref = writer._add_object(sig)
    widget = DictionaryObject({
        NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Widget"),
        NameObject("/FT"): NameObject("/Sig"), NameObject("/T"): TextStringObject(dsc.SIGNATURE_FIELD),
        NameObject("/Rect"): ArrayObject([FloatObject(v) for v in box]), NameObject("/V"): sig_ref,
        NameObject("/F"): NumberObject(4), NameObject("/P"): page.indirect_reference,
    })
    widget_ref = writer._add_object(widget)
    annots = page.get("/Annots") or ArrayObject()
    annots.append(widget_ref)
    if extra_annotation:
        annots.append(writer._add_object(DictionaryObject({
            NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/FreeText"),
            NameObject("/Rect"): ArrayObject([FloatObject(v) for v in (40, 400, 300, 440)]),
            NameObject("/Contents"): TextStringObject("Total: Rs. 1.00")})))
    page[NameObject("/Annots")] = annots
    if change_content:
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 30 Tf 100 400 Td (PAID 1.00) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
    root = writer._root_object
    acro = DictionaryObject({NameObject("/Fields"): ArrayObject([widget_ref]), NameObject("/SigFlags"): NumberObject(3)})
    root[NameObject("/AcroForm")] = writer._add_object(acro)
    out = io.BytesIO()
    writer.write(out)
    pdf = out.getvalue()

    # Fill in the byte range and the CMS signature (placeholders have fixed widths).
    start = len(data)
    m = re.compile(rb"/ByteRange\s*\[[^\]]*\]").search(pdf, start)
    hole = re.compile(rb"/Contents\s*<(0+)>").search(pdf, start)
    b, c = hole.start(1) - 1, hole.end(1) + 1
    ranges = f"/ByteRange [0 {b} {c} {len(pdf) - c}]".encode()
    ranges = ranges + b" " * (m.end() - m.start() - len(ranges))
    pdf = pdf[:m.start()] + ranges + pdf[m.end():]
    signed_data = pdf[:b] + pdf[c:]
    cms = (pkcs7.PKCS7SignatureBuilder().set_data(signed_data).add_signer(cert, key, hashes.SHA256())
           .sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.DetachedSignature, pkcs7.PKCS7Options.Binary]))
    hex_sig = cms.hex().encode()
    hex_sig = hex_sig + b"0" * (c - b - 2 - len(hex_sig))
    return pdf[:b + 1] + hex_sig + pdf[c - 1:]

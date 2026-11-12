"""Checks a PDF that an SRIC signatory signed with their DSC on their own computer before the portal accepts it.

Accepted only when ALL of these hold (each failure is reported in plain words):

1. The signed file is the prepared PDF *plus* one incremental update: its first bytes are exactly the prepared PDF
   (SHA-256 recorded when it was prepared), so not one character of the invoice was changed.
2. The update adds exactly one signature whose /ByteRange covers the whole file except its own /Contents.
3. The CMS (PKCS#7) signature's messageDigest equals the digest of the signed byte range, and the signature over the
   signed attributes verifies with the public key of a *registered, active, in-date* certificate of the uploader.
4. The update leaves every page's content as it was, and adds no annotation other than signature widgets placed in
   the invoice's signature box (no hidden overlays).

Only ``cryptography`` and ``pypdf`` are used (no PKCS#11 or private key on the server: signing happens in the token).
Certificate revocation is handled by deactivating the registration in the portal (CRL/OCSP checks are an onboarding
item, see docs/gst-einvoice/README.md).
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

# The rectangle (PDF points, from the bottom-left of page 1) reserved for the DSC signature on a B2C invoice.
# ``pdf.render`` draws the box there and the signer utility places its visible signature inside it.
SIGNATURE_BOX = (338.0, 96.0, 553.0, 156.0)
SIGNATURE_FIELD = "SRIC_Signatory"
BOX_TOLERANCE = 6.0

OID_SIGNED_DATA = "1.2.840.113549.1.7.2"
OID_MESSAGE_DIGEST = "1.2.840.113549.1.9.4"
DIGESTS = {
    "2.16.840.1.101.3.4.2.1": hashes.SHA256,
    "2.16.840.1.101.3.4.2.2": hashes.SHA384,
    "2.16.840.1.101.3.4.2.3": hashes.SHA512,
}
RSA_PSS = "1.2.840.113549.1.1.10"


class SignatureRejected(Exception):
    pass


# --- minimal DER reader -----------------------------------------------------------------------------------------------

@dataclass
class Tlv:
    tag: int
    start: int      # offset of the tag byte
    body: int       # offset of the content
    end: int        # offset after the content
    raw: bytes      # the whole buffer

    @property
    def content(self) -> bytes:
        return self.raw[self.body:self.end]

    @property
    def encoded(self) -> bytes:
        return self.raw[self.start:self.end]

    def children(self) -> list["Tlv"]:
        out, pos = [], self.body
        while pos < self.end:
            child = read_tlv(self.raw, pos)
            out.append(child)
            pos = child.end
        return out


def read_tlv(buf: bytes, pos: int = 0) -> Tlv:
    if pos + 2 > len(buf):
        raise SignatureRejected("The signature data is truncated.")
    tag = buf[pos]
    if tag & 0x1F == 0x1F:
        raise SignatureRejected("Unsupported ASN.1 tag in the signature.")
    length = buf[pos + 1]
    body = pos + 2
    if length & 0x80:
        n = length & 0x7F
        if n == 0 or n > 4:
            raise SignatureRejected("Indefinite or oversized length in the signature (not DER).")
        length = int.from_bytes(buf[body:body + n], "big")
        body += n
    end = body + length
    if end > len(buf):
        raise SignatureRejected("The signature data is truncated.")
    return Tlv(tag, pos, body, end, buf)


def oid_text(tlv: Tlv) -> str:
    if tlv.tag != 0x06:
        raise SignatureRejected("Expected an object identifier in the signature.")
    data = tlv.content
    first = data[0]
    parts = [str(min(first // 40, 2)), str(first - 40 * min(first // 40, 2))]
    value = 0
    for byte in data[1:]:
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            parts.append(str(value))
            value = 0
    return ".".join(parts)


@dataclass
class CmsSignature:
    digest_oid: str
    message_digest: bytes
    signed_attrs_der: bytes   # re-tagged as SET OF for verification
    signature: bytes
    signature_alg_oid: str
    certificates: list = field(default_factory=list)


def parse_cms(der: bytes) -> CmsSignature:
    """The single SignerInfo of a detached CMS SignedData (as PDF signatures carry)."""
    content_info = read_tlv(der, 0)
    ci = content_info.children()
    if len(ci) < 2 or oid_text(ci[0]) != OID_SIGNED_DATA:
        raise SignatureRejected("The signature is not a CMS SignedData object.")
    signed_data = ci[1].children()[0]  # [0] EXPLICIT SignedData
    parts = signed_data.children()
    certificates = []
    signer_infos = None
    for part in parts[3:]:
        if part.tag == 0xA0:  # [0] IMPLICIT certificates
            for cert in part.children():
                if cert.tag == 0x30:
                    try:
                        certificates.append(x509.load_der_x509_certificate(cert.encoded))
                    except ValueError:
                        pass
        elif part.tag == 0x31:
            signer_infos = part
    if signer_infos is None:
        raise SignatureRejected("The signature has no signer information.")
    infos = signer_infos.children()
    if len(infos) != 1:
        raise SignatureRejected("Exactly one signer is expected.")
    si = infos[0].children()
    # version, sid, digestAlgorithm, [0] signedAttrs, signatureAlgorithm, signature, [1] unsignedAttrs?
    digest_oid = oid_text(si[2].children()[0])
    idx = 3
    if si[idx].tag != 0xA0:
        raise SignatureRejected("The signature has no signed attributes.")
    attrs = si[idx]
    message_digest = None
    for attr in attrs.children():
        kids = attr.children()
        if oid_text(kids[0]) == OID_MESSAGE_DIGEST:
            message_digest = kids[1].children()[0].content
    if message_digest is None:
        raise SignatureRejected("The signature has no message digest.")
    # The signature covers the DER of the attributes with the SET OF tag (0x31), not the [0] tag.
    signed_attrs_der = b"\x31" + attrs.encoded[1:]
    sig_alg = oid_text(si[idx + 1].children()[0])
    signature = si[idx + 2].content
    return CmsSignature(digest_oid, message_digest, signed_attrs_der, signature, sig_alg, certificates)


def verify_with(cert: x509.Certificate, cms: CmsSignature) -> bool:
    algo = DIGESTS.get(cms.digest_oid)
    if algo is None:
        raise SignatureRejected("The signature uses an unsupported digest (SHA-256/384/512 expected).")
    key = cert.public_key()
    try:
        if isinstance(key, rsa.RSAPublicKey):
            if cms.signature_alg_oid == RSA_PSS:
                key.verify(cms.signature, cms.signed_attrs_der,
                           padding.PSS(mgf=padding.MGF1(algo()), salt_length=padding.PSS.AUTO), algo())
            else:
                key.verify(cms.signature, cms.signed_attrs_der, padding.PKCS1v15(), algo())
        elif isinstance(key, ec.EllipticCurvePublicKey):
            key.verify(cms.signature, cms.signed_attrs_der, ec.ECDSA(algo()))
        else:
            return False
    except InvalidSignature:
        return False
    return True


# --- PDF structure ----------------------------------------------------------------------------------------------------

_BYTE_RANGE = re.compile(rb"/ByteRange\s*\[\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*\]")


@dataclass
class Verified:
    certificate_id: int
    fingerprint: str
    subject: str
    signed_sha256: str


def _pages_content(data: bytes) -> list[bytes]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    out = []
    for page in reader.pages:
        contents = page.get_contents()
        out.append(contents.get_data() if contents is not None else b"")
    return out


def _annotations(data: bytes) -> list[list[dict]]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages:
        items = []
        for ref in page.get("/Annots") or []:
            annot = ref.get_object()
            parent = annot.get("/Parent")
            field_type = annot.get("/FT") or (parent.get_object().get("/FT") if parent is not None else None)
            items.append({"subtype": str(annot.get("/Subtype")), "ft": str(field_type) if field_type else "",
                          "rect": [float(v) for v in (annot.get("/Rect") or [0, 0, 0, 0])]})
        pages.append(items)
    return pages


def _inside_box(rect: list[float]) -> bool:
    x1, y1, x2, y2 = min(rect[0], rect[2]), min(rect[1], rect[3]), max(rect[0], rect[2]), max(rect[1], rect[3])
    bx1, by1, bx2, by2 = SIGNATURE_BOX
    t = BOX_TOLERANCE
    return x1 >= bx1 - t and y1 >= by1 - t and x2 <= bx2 + t and y2 <= by2 + t


def verify_signed_pdf(prepared: bytes, signed: bytes, certificates) -> Verified:
    """``certificates``: the uploader's active registered ``SignerCertificate`` rows. Raises SignatureRejected."""
    from django.utils import timezone

    if not signed.startswith(b"%PDF-"):
        raise SignatureRejected("The file is not a PDF.")
    if len(signed) <= len(prepared) or signed[:len(prepared)] != prepared:
        raise SignatureRejected("The signed file is not the invoice the portal prepared (its content differs).")
    appended = signed[len(prepared):]
    ranges = list(_BYTE_RANGE.finditer(appended))
    if len(ranges) != 1 or _BYTE_RANGE.search(prepared):
        raise SignatureRejected("Exactly one signature must be added to the prepared invoice.")
    a, b, c, d = (int(x) for x in ranges[0].groups())
    if a != 0 or b <= len(prepared) or c <= b or c + d != len(signed):
        raise SignatureRejected("The signature does not cover the whole invoice.")
    hole = signed[b:c]
    if not (hole.startswith(b"<") and hole.endswith(b">")):
        raise SignatureRejected("The signature container is malformed.")
    try:
        # The container is zero-padded after the DER object; its own length says where it ends.
        padded = bytes.fromhex(re.sub(rb"\s", b"", hole[1:-1]).decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        raise SignatureRejected("The signature container is malformed.")
    cms_der = padded[:read_tlv(padded, 0).end]
    cms = parse_cms(cms_der)
    algo = DIGESTS.get(cms.digest_oid)
    if algo is None:
        raise SignatureRejected("The signature uses an unsupported digest (SHA-256/384/512 expected).")
    h = hashes.Hash(algo())
    h.update(signed[a:a + b])
    h.update(signed[c:c + d])
    if h.finalize() != cms.message_digest:
        raise SignatureRejected("The signature does not match the file (it was changed after signing).")

    now = timezone.now()
    match = None
    for row in certificates:
        cert = x509.load_pem_x509_certificate(row.certificate_pem.encode())
        if not row.is_active or not (row.not_before <= now <= row.not_after):
            continue
        if verify_with(cert, cms):
            match = row
            break
    if match is None:
        raise SignatureRejected("The signature was not made with your registered, active DSC certificate.")

    # No change to what the invoice shows: page content identical, only signature widgets in the signature box added.
    try:
        before, after = _pages_content(prepared), _pages_content(signed)
        annots_before, annots_after = _annotations(prepared), _annotations(signed)
    except Exception as exc:  # noqa: BLE001 - any parse failure is a refusal, never an acceptance
        raise SignatureRejected(f"The signed PDF could not be read ({type(exc).__name__}).")
    if before != after:
        raise SignatureRejected("The signed PDF changes the invoice's page content.")
    for page_before, page_after in zip(annots_before, annots_after):
        for annot in page_after[len(page_before):]:
            if annot["subtype"] != "/Widget" or annot["ft"] != "/Sig":
                raise SignatureRejected("The signed PDF adds something other than the signature.")
            if annot["rect"] != [0.0, 0.0, 0.0, 0.0] and not _inside_box(annot["rect"]):
                raise SignatureRejected("The visible signature is outside the signatory box.")
    return Verified(match.pk, match.sha256_fingerprint, match.subject, hashlib.sha256(signed).hexdigest())


def certificate_details(pem_or_der: bytes) -> dict:
    """Facts of an uploaded certificate (.cer/.crt, PEM or DER) for registration. Raises ValueError."""
    data = pem_or_der.strip()
    cert = x509.load_pem_x509_certificate(data) if data.startswith(b"-----BEGIN") else x509.load_der_x509_certificate(data)
    from cryptography.hazmat.primitives.serialization import Encoding

    return {
        "certificate": cert,
        "pem": cert.public_bytes(Encoding.PEM).decode(),
        "fingerprint": cert.fingerprint(hashes.SHA256()).hex(),
        "subject": cert.subject.rfc4514_string()[:400],
        "issuer": cert.issuer.rfc4514_string()[:400],
        "serial": format(cert.serial_number, "X")[:80],
        "not_before": cert.not_valid_before_utc,
        "not_after": cert.not_valid_after_utc,
    }

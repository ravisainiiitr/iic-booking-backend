"""Finding the signatory's DSC token: PKCS#11 drivers of the common Indian DSC tokens, the tokens plugged in, and
plain-language messages for the usual failures (no token, wrong PIN, locked token).

* ``find_drivers`` looks in System32, SysWOW64 and Program Files (both) for the known driver DLLs, and keeps only
  those built for this program's own architecture (a 64-bit signer can only load 64-bit drivers, and the reverse);
  drivers of the other architecture are reported, so the screen can say why they are not usable.
* ``list_tokens`` opens each usable driver and lists the tokens actually plugged in, with the certificates on them
  (subject, issuer, expiry). No PIN is needed for this: certificates are public objects.
* ``friendly_error`` turns PKCS#11 errors into messages a signatory can act on.

Nothing here logs in, signs or sends anything anywhere.
"""

from __future__ import annotations

import os
import struct
import sys
from dataclasses import dataclass, field

# (vendor, DLL file names). Names are matched case-insensitively.
KNOWN_DRIVERS: list[tuple[str, tuple[str, ...]]] = [
    ("SafeNet / eToken", ("eTPKCS11.dll",)),
    ("ePass2003", ("eps2003csp11.dll", "eps2003csp11v2.dll")),
    ("ProxKey / Watchdata", ("SignatureP11.dll", "WDPKCS.dll", "WatchdataPKCS11.dll")),
    ("mToken", ("mtoken_pkcs11.dll", "CryptoIDA_pkcs11.dll")),
    ("Gemalto / Thales IDPrime", ("IDPrimePKCS11.dll", "IDPrimePKCS1164.dll", "gclib.dll")),
    ("HyperPKI", ("hypersecurep11.dll",)),
    ("Trust Key", ("TrustKeyP11.dll",)),
    ("Bit4id", ("bit4xpki.dll",)),
    ("SafeSign", ("aetpkss1.dll",)),
    ("Longmai", ("mtoken_gm3000.dll", "gm3000_pkcs11.dll", "longmai_pkcs11.dll", "lmpkcs11.dll")),
]
# Folders under Program Files that hold token software (searched a few levels deep).
VENDOR_FOLDERS = ("safenet", "gemalto", "thales", "eToken", "epass", "feitian", "watchdata", "proxkey", "mtoken",
                  "longmai", "hypersecu", "hyperpki", "trustkey", "bit4id", "a.e.t", "safesign", "capricorn", "emudhra",
                  "e-mudhra", "ncode", "vsign", "sify")
MAX_DEPTH = 4

MACHINE = {0x014C: "32-bit", 0x8664: "64-bit", 0xAA64: "ARM64"}


def own_arch() -> str:
    """The architecture this program runs as ("64-bit" / "32-bit")."""
    if sys.platform == "win32" and os.environ.get("PROCESSOR_ARCHITECTURE", "").upper() == "ARM64" and \
            struct.calcsize("P") == 8:
        return "ARM64"
    return "64-bit" if struct.calcsize("P") == 8 else "32-bit"


def dll_arch(path: str) -> str:
    """The architecture a Windows DLL was built for, from its PE header ("" when it is not a PE file)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return ""
    if len(head) < 0x40 or head[:2] != b"MZ":
        return ""
    offset = struct.unpack_from("<I", head, 0x3C)[0]
    if offset + 6 > len(head) or head[offset:offset + 4] != b"PE\0\0":
        return ""
    return MACHINE.get(struct.unpack_from("<H", head, offset + 4)[0], "")


@dataclass
class Driver:
    vendor: str
    path: str
    arch: str
    usable: bool

    @property
    def label(self) -> str:
        return f"{self.vendor} ({os.path.basename(self.path)}, {self.arch or 'unknown'})"


def search_roots(environ=None) -> list[str]:
    env = os.environ if environ is None else environ
    windir = env.get("SystemRoot") or env.get("windir") or r"C:\Windows"
    roots = [os.path.join(windir, "System32"), os.path.join(windir, "SysWOW64")]
    if own_arch() == "32-bit":
        roots.append(os.path.join(windir, "Sysnative"))  # the real 64-bit System32 seen from a 32-bit program
    for name in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        if env.get(name):
            roots.append(env[name])
    out, seen = [], set()
    for root in roots:
        key = os.path.normcase(os.path.abspath(root))
        if key not in seen:
            seen.add(key)
            out.append(root)
    return out


def _names() -> dict[str, str]:
    return {name.lower(): vendor for vendor, names in KNOWN_DRIVERS for name in names}


def _walk(root: str, depth: int, wanted: dict[str, str], found: list[tuple[str, str]]):
    try:
        entries = list(os.scandir(root))
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_file(follow_symlinks=False):
                vendor = wanted.get(entry.name.lower())
                if vendor:
                    found.append((vendor, entry.path))
            elif depth > 0 and entry.is_dir(follow_symlinks=False):
                lower = entry.name.lower()
                # Program Files: only vendor folders (and anything below them) are searched.
                if depth < MAX_DEPTH or any(v in lower for v in VENDOR_FOLDERS):
                    _walk(entry.path, depth - 1, wanted, found)
        except OSError:
            continue


def find_drivers(roots: list[str] | None = None) -> list[Driver]:
    """Known DSC token drivers on this computer, usable ones (this program's architecture) first."""
    wanted = _names()
    hits: list[tuple[str, str]] = []
    for root in roots if roots is not None else search_roots():
        if os.path.basename(root).lower() in ("system32", "syswow64", "sysnative"):
            _walk(root, 0, wanted, hits)          # driver DLLs sit directly in the system folders
        else:
            _walk(root, MAX_DEPTH, wanted, hits)  # Program Files: vendor folders only, a few levels deep
    mine = own_arch()
    drivers, seen = [], set()
    for vendor, path in hits:
        key = os.path.normcase(os.path.realpath(path))
        if key in seen:
            continue
        seen.add(key)
        arch = dll_arch(path)
        drivers.append(Driver(vendor, path, arch, usable=(arch == mine)))
    drivers.sort(key=lambda d: (not d.usable, d.vendor, d.path.lower()))
    return drivers


# --- tokens ---------------------------------------------------------------------------------------------------------

@dataclass
class TokenCert:
    subject: str
    issuer: str
    not_after: str
    cert_id: str        # CKA_ID, hex (pairs the certificate with its private key)
    label: str = ""


@dataclass
class TokenInfo:
    driver: Driver
    slot_id: int
    label: str
    manufacturer: str
    serial: str
    certificates: list[TokenCert] = field(default_factory=list)
    error: str = ""

    @property
    def title(self) -> str:
        who = self.certificates[0].subject if self.certificates else "no certificate found"
        return f"{self.label or 'Token'} ({self.manufacturer}) - {who}"


def _cn(name) -> str:
    try:
        from cryptography.x509.oid import NameOID

        parts = name.get_attributes_for_oid(NameOID.COMMON_NAME)
        return parts[0].value if parts else name.rfc4514_string()
    except Exception:  # noqa: BLE001
        return str(name)


def _certificates(token) -> list[TokenCert]:
    from cryptography import x509
    from pkcs11 import Attribute, ObjectClass

    out = []
    with token.open() as session:  # no login: certificates are public objects
        for obj in session.get_objects({Attribute.CLASS: ObjectClass.CERTIFICATE}):
            try:
                cert = x509.load_der_x509_certificate(bytes(obj[Attribute.VALUE]))
            except Exception:  # noqa: BLE001
                continue
            try:
                cid = bytes(obj[Attribute.ID]).hex()
            except Exception:  # noqa: BLE001
                cid = ""
            try:
                label = str(obj[Attribute.LABEL])
            except Exception:  # noqa: BLE001
                label = ""
            out.append(TokenCert(subject=_cn(cert.subject), issuer=_cn(cert.issuer),
                                 not_after=cert.not_valid_after_utc.strftime("%d %b %Y"), cert_id=cid, label=label))
    return out


def _text(value) -> str:
    if isinstance(value, bytes):
        value = value.decode("ascii", "replace")
    return str(value or "").strip()


def list_tokens(drivers: list[Driver], loader=None) -> list[TokenInfo]:
    """The tokens plugged in, for every usable driver. ``loader`` (tests) returns a python-pkcs11 ``lib``."""
    if loader is None:
        import pkcs11

        loader = pkcs11.lib
    tokens = []
    for driver in drivers:
        if not driver.usable:
            continue
        try:
            lib = loader(driver.path)
            slots = lib.get_slots(token_present=True)
        except Exception as exc:  # noqa: BLE001 - a broken driver must not stop the others
            tokens.append(TokenInfo(driver, -1, "", "", "", error=friendly_error(exc)))
            continue
        for slot in slots:
            try:
                token = slot.get_token()
                info = TokenInfo(driver, slot.slot_id, (token.label or "").strip(),
                                 (token.manufacturer_id or "").strip(), _text(getattr(token, "serial", "")))
                try:
                    info.certificates = _certificates(token)
                except Exception as exc:  # noqa: BLE001
                    info.error = friendly_error(exc)
                tokens.append(info)
            except Exception as exc:  # noqa: BLE001
                tokens.append(TokenInfo(driver, getattr(slot, "slot_id", -1), "", "", "", error=friendly_error(exc)))
    return tokens


NO_TOKEN = ("No DSC token is plugged in. Insert the token, wait for its light, then press Detect token. "
            "If it is plugged in, install the token's driver software (from the token's vendor) and try again.")


def friendly_error(exc: BaseException) -> str:
    """A message the signatory can act on, for the usual PKCS#11 failures."""
    name = type(exc).__name__
    messages = {
        "PinIncorrect": "The PIN is wrong. Check it and try again: the token locks itself after a few wrong PINs.",
        "PinLocked": ("The token is locked after too many wrong PINs. It cannot sign until it is unlocked with its "
                      "admin (SO) PIN, using the token vendor's software or the issuer of the DSC."),
        "PinExpired": "The token's PIN has expired. Change it with the token vendor's software first.",
        "PinLenRange": "The PIN has the wrong length for this token.",
        "PinInvalid": "The PIN contains characters this token does not accept.",
        "TokenNotPresent": NO_TOKEN,
        "NoSuchToken": NO_TOKEN,
        "DeviceRemoved": "The token was removed. Plug it in again and retry.",
        "TokenNotRecognised": "The token is not recognised by this driver. Choose the driver that came with the token.",
        "DeviceError": "The token reported an error. Remove it, plug it in again and retry.",
        "UserNotLoggedIn": "The token did not accept the login. Enter the PIN again.",
    }
    if name in messages:
        return messages[name]
    text = str(exc) or name
    lowered = text.lower()
    if "pin_incorrect" in lowered or "pin incorrect" in lowered:
        return messages["PinIncorrect"]
    if "pin_locked" in lowered or ("locked" in lowered and "pin" in lowered):
        return messages["PinLocked"]
    if "no token" in lowered or "token not present" in lowered or "no slots" in lowered:
        return NO_TOKEN
    if isinstance(exc, OSError) or "cannot load" in lowered or "loadlibrary" in lowered or "error 193" in lowered:
        return (f"The driver could not be loaded ({text}). It may be for the other architecture ({own_arch()} signer) "
                "or damaged; reinstall the token software or choose another driver.")
    return f"The token reported: {text}"

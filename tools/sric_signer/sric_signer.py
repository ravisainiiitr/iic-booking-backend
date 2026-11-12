"""SRIC Invoice Signer: signs the portal's tax invoices (buyers without a GSTIN) with the signatory's DSC, on the
signatory's own computer. Dean SRIC proposal, section 4 and Figure 3.

* The utility lists the invoices waiting for signature (portal API, device token linked in the portal under
  GST invoices > Signing). The signatory ticks them, previews each PDF, picks the DSC token (found automatically,
  see ``sric_token``) and enters its PIN here.
* The PIN goes only to the token (PKCS#11); the private key never leaves the token. Nothing but the signed PDFs is
  uploaded, and the portal accepts a signed PDF only if it verifies with the signatory's registered certificate and
  matches the prepared invoice byte for byte.
* Before signing, the downloaded PDF's SHA-256 is compared with the one the portal listed, so the signatory signs
  exactly what was prepared.

Key sources:
    the DSC token (PKCS#11): found automatically, or ``--pkcs11-lib`` / "Browse for driver..."
    --pkcs12  a .p12/.pfx file. ONLY for the sandbox demonstration with a TEST DSC (make_test_dsc.py).

    "SRIC Invoice Signer.exe"                       the window
    "SRIC Invoice Signer.exe" --detect              list token drivers and plugged-in tokens (console build / python)
    python sric_signer.py --cli --portal https://equip.iitr.ac.in --token-file token.txt --all
    python sric_signer.py --cli --portal http://localhost:8000 --token-file token.txt --pkcs12 test-dsc.p12 --all
    "SRIC Invoice Signer.exe" --self-test result.txt   checks the bundled pieces load (used by the build)
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
from urllib.parse import urlparse

import requests

import sric_token
from version import APP_NAME as APP
from version import VERSION

CONFIG = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "SRIC Invoice Signer", "config.json")
TIMEOUT = (10, 60)
EXPORT_KINDS = [
    ("all", "All three (zip)"),
    ("json", "GSTR-1 JSON (upload)"),
    ("invoice_register", "Invoice Register (Excel)"),
    ("credit_note_register", "Credit Note Register (Excel)"),
]


class SignerError(Exception):
    pass


# --- portal ------------------------------------------------------------------------------------------------------------

class Portal:
    def __init__(self, base: str, token: str):
        parsed = urlparse(base)
        local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
            raise SignerError("The portal address must start with https:// (http:// only for localhost).")
        if not token.strip():
            raise SignerError("Paste the signing utility token from the portal (GST invoices > Signing (DSC)).")
        self.base = base.rstrip("/")
        self.http = requests.Session()
        self.http.headers.update({"Authorization": f"SricSigner {token.strip()}", "User-Agent": f"{APP}/{VERSION}"})

    def _get(self, path: str):
        try:
            resp = self.http.get(self.base + path, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise SignerError(f"The portal could not be reached ({type(exc).__name__}). Check the address and the "
                              "network.") from exc
        if resp.status_code == 401:
            raise SignerError("The portal did not accept this utility's token (revoked or mistyped).")
        if resp.status_code >= 400:
            raise SignerError(self._message(resp))
        return resp

    @staticmethod
    def _message(resp) -> str:
        try:
            return resp.json().get("error") or f"HTTP {resp.status_code}"
        except ValueError:
            return f"HTTP {resp.status_code}"

    def whoami(self) -> dict:
        return self._get("/api/v1/gst-einvoice/signer/api/whoami/").json()

    def queue(self) -> dict:
        return self._get("/api/v1/gst-einvoice/signer/api/queue/").json()

    def prepared_pdf(self, item: dict) -> bytes:
        data = self._get(item["pdf_url"]).content
        if hashlib.sha256(data).hexdigest() != item["pdf_sha256"]:
            raise SignerError(f"Invoice {item['invoice_number']}: the downloaded file is not the one the portal "
                              "prepared (SHA-256 differs). Not signed.")
        return data

    def gstr1(self, start: str, end: str, kind: str) -> tuple[str, bytes]:
        """(file name, content): a monthly B2C file built by the portal (``kind``: json, xlsx, invoice_register,
        credit_note_register, or all three in one zip); one file per month, a zip for several."""
        import re

        from urllib.parse import urlencode

        resp = self._get("/api/v1/gst-einvoice/signer/api/gstr1/?" + urlencode({"from": start, "to": end, "kind": kind}))
        found = re.search(r'filename="([^"]+)"', resp.headers.get("Content-Disposition", ""))
        return (found.group(1) if found else f"GSTR1-B2C.{kind}"), resp.content

    def upload(self, item: dict, signed: bytes) -> dict:
        name = f"{item['invoice_number'].replace('/', '-')}-signed.pdf"
        resp = self.http.post(self.base + item["upload_url"], files={"file": (name, signed, "application/pdf")},
                              timeout=TIMEOUT)
        if resp.status_code >= 400:
            raise SignerError(f"Invoice {item['invoice_number']}: {self._message(resp)}")
        return resp.json()


# --- signing ------------------------------------------------------------------------------------------------------------

def open_signer(*, pkcs11_lib: str = "", token_label: str = "", token_serial: str = "", cert_id: str = "",
                pkcs12: str = "", pin: str):
    """(pyHanko signer, session or None). PKCS#11: the PIN logs in to the token and the token signs. PKCS#12: test
    DSC only. Raises SignerError with a message the signatory can act on."""
    from pyhanko.sign import signers

    if pkcs11_lib:
        from pyhanko.config.pkcs11 import TokenCriteria
        from pyhanko.sign import pkcs11

        if not pin:
            raise SignerError("Enter the token's PIN.")
        criteria = TokenCriteria(label=token_label or None, serial=token_serial.encode() if token_serial else None)
        try:
            session = pkcs11.open_pkcs11_session(pkcs11_lib, token_criteria=criteria, user_pin=pin)
        except Exception as exc:  # noqa: BLE001 - PIN, lock and presence errors come from the driver
            raise SignerError(sric_token.friendly_error(_root(exc))) from exc
        try:
            ident = bytes.fromhex(cert_id) if cert_id else None
            signer = pkcs11.PKCS11Signer(session, cert_id=ident, key_id=ident) if ident else pkcs11.PKCS11Signer(
                session, cert_label=None, key_label=None)
        except Exception as exc:  # noqa: BLE001
            session.close()
            raise SignerError(f"The DSC certificate or its key was not found on the token: {exc}") from exc
        return signer, session
    if pkcs12:
        try:
            signer = signers.SimpleSigner.load_pkcs12(pfx_file=pkcs12, passphrase=pin.encode() if pin else None)
        except Exception:  # noqa: BLE001
            signer = None
        if signer is None:
            raise SignerError("The test DSC file could not be opened (wrong PIN or not a .p12 file).")
        return signer, None
    raise SignerError("Choose the DSC token (Detect token, or Browse for driver...) or, for the sandbox only, a test "
                      "DSC file.")


def _root(exc: BaseException) -> BaseException:
    """The innermost cause (pyHanko wraps the driver's PKCS#11 error)."""
    seen = set()
    while (exc.__cause__ or exc.__context__) and id(exc) not in seen:
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return exc


def sign_pdf(data: bytes, signer, *, box, field_name: str) -> bytes:
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import fields, signers
    from pyhanko.stamp import TextStampStyle

    writer = IncrementalPdfFileWriter(io.BytesIO(data))
    fields.append_signature_field(writer, fields.SigFieldSpec(sig_field_name=field_name, box=tuple(box), on_page=0))
    meta = signers.PdfSignatureMetadata(field_name=field_name, md_algorithm="sha256", location="IIT Roorkee",
                                        reason="Tax invoice issued by SRIC, IIT Roorkee")
    style = TextStampStyle(stamp_text="Digitally signed by %(signer)s\nDate: %(ts)s\nAuthorised Signatory, SRIC",
                           border_width=0)
    out = io.BytesIO()
    try:
        signers.PdfSigner(meta, signer=signer, stamp_style=style).sign_pdf(writer, output=out)
    except Exception as exc:  # noqa: BLE001 - a PIN/lock/removal during signing comes back from the token
        raise SignerError(sric_token.friendly_error(_root(exc))) from exc
    return out.getvalue()


def sign_items(portal: Portal, items: list[dict], signer, *, box, field_name: str, log=print) -> tuple[int, int]:
    done = failed = 0
    for item in items:
        try:
            data = portal.prepared_pdf(item)
            signed = sign_pdf(data, signer, box=box, field_name=field_name)
            result = portal.upload(item, signed)
            log(f"Signed and uploaded {item['invoice_number']} ({item['buyer']}, Rs. {item['total']}): {result['status']}")
            done += 1
        except Exception as exc:  # noqa: BLE001 - report each invoice and carry on with the rest
            log(f"NOT signed {item.get('invoice_number')}: {exc}")
            failed += 1
            if isinstance(exc, SignerError) and ("PIN" in str(exc) or "locked" in str(exc) or "removed" in str(exc)):
                break  # the token itself refused: do not hammer it (a wrong PIN counts towards the lock)
    return done, failed


# --- configuration -------------------------------------------------------------------------------------------------------

def load_config() -> dict:
    try:
        with open(CONFIG, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG, "w", encoding="utf-8") as fh:
        json.dump({k: v for k, v in cfg.items() if k != "pin"}, fh, indent=2)  # never the PIN


# --- detection (shared by the window, --detect and the CLI) --------------------------------------------------------------

def detect(extra_driver: str = "") -> tuple[list[sric_token.Driver], list[sric_token.TokenInfo]]:
    drivers = sric_token.find_drivers()
    if extra_driver and os.path.isfile(extra_driver) and all(
            os.path.normcase(d.path) != os.path.normcase(extra_driver) for d in drivers):
        arch = sric_token.dll_arch(extra_driver)
        drivers.insert(0, sric_token.Driver("Chosen driver", extra_driver, arch, usable=arch == sric_token.own_arch()))
    return drivers, sric_token.list_tokens(drivers)


def detect_report(extra_driver: str = "") -> str:
    drivers, tokens = detect(extra_driver)
    lines = [f"{APP} {VERSION} ({sric_token.own_arch()})", "Drivers found:"]
    lines += [f"  {'OK ' if d.usable else 'n/a'} {d.label}  {d.path}" for d in drivers] or ["  none"]
    lines.append("Tokens plugged in:")
    for t in tokens:
        lines.append(f"  [{t.driver.vendor}] {t.error}" if t.error and not t.label else f"  {t.title}")
        for c in t.certificates:
            lines.append(f"      certificate: {c.subject} (issued by {c.issuer}, valid until {c.not_after})")
    if not tokens:
        lines.append("  " + sric_token.NO_TOKEN)
    return "\n".join(lines)


# --- self-test (the build runs it on the frozen exe) ---------------------------------------------------------------------

def _dist_version(name: str) -> str:
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:  # noqa: BLE001
        return "?"


def _blank_pdf() -> bytes:
    """A one-page PDF with a correct cross-reference table (for the self-test)."""
    objects = [b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
               b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]/Resources<<>>>>"]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<</Size {len(objects) + 1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def self_test() -> str:
    """Loads every native piece the exe bundles and signs a PDF with a throw-away key; returns a report."""
    import datetime as dt

    import pkcs11
    import pkcs11._pkcs11  # noqa: F401 - the compiled PKCS#11 binding
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID
    from pyhanko.sign import signers

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "self-test")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now - dt.timedelta(minutes=1)).not_valid_after(now + dt.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    with tempfile.TemporaryDirectory() as tmp:
        p12 = os.path.join(tmp, "t.p12")
        with open(p12, "wb") as fh:
            fh.write(pkcs12.serialize_key_and_certificates(b"t", key, cert, None,
                                                           serialization.BestAvailableEncryption(b"1234")))
        signer = signers.SimpleSigner.load_pkcs12(pfx_file=p12, passphrase=b"1234")
    signed = sign_pdf(_blank_pdf(), signer, box=(338, 96, 553, 156), field_name="SelfTest")
    drivers = sric_token.find_drivers()
    try:
        import tkinter  # noqa: F401

        tk_ok = "yes"
    except Exception as exc:  # noqa: BLE001
        tk_ok = f"NO ({exc})"
    return "\n".join([
        f"SELF-TEST OK {APP} {VERSION} {sric_token.own_arch()} frozen={getattr(sys, 'frozen', False)}",
        f"python-pkcs11 {_dist_version('python-pkcs11')} binding loaded ({pkcs11.__name__})",
        f"pyHanko {_dist_version('pyHanko')}",
        f"pyHanko signed a test PDF ({len(signed)} bytes)",
        f"tkinter: {tk_ok}",
        f"token drivers found: {len(drivers)} ({sum(d.usable for d in drivers)} usable)",
    ])


# --- command line --------------------------------------------------------------------------------------------------------

def cli(args) -> int:
    token = args.token or ""
    if args.token_file:
        with open(args.token_file, encoding="utf-8") as fh:
            token = fh.read().strip()
    if not token:
        token = getpass.getpass("Signing utility token: ")
    portal = Portal(args.portal, token)
    who = portal.whoami()
    print(f"{APP} {VERSION} - {who['user']} on '{who['device']}' (portal mode: {who['mode']})")
    for cert in who["certificates"]:
        print(f"  registered DSC: {cert['subject']}{'  [TEST DSC]' if cert['is_test'] else ''}")
    queue = portal.queue()
    items = queue["results"]
    if args.invoice:
        items = [i for i in items if i["invoice_number"] in args.invoice]
    print(f"{len(items)} invoice(s) waiting for signature:")
    for i in items:
        print(f"  {i['invoice_number']}  {i['invoice_date']}  {i['buyer']}  Rs. {i['total']}  (receipt {i['receipt_number'] or '-'})")
    if not items:
        return 0
    if not (args.all or args.invoice):
        print("Nothing signed: pass --all or --invoice NUMBER.")
        return 0
    if args.pkcs12 and who["mode"] != "sandbox":
        raise SignerError("A test DSC file can only be used with a sandbox portal.")
    lib, label, serial, cert_id = args.pkcs11_lib or "", args.token_label or "", "", ""
    if not args.pkcs12 and not lib:
        _drivers, tokens = detect()
        usable = [t for t in tokens if t.label and not t.error]
        if not usable:
            raise SignerError(sric_token.NO_TOKEN)
        if len(usable) > 1 and not label:
            raise SignerError("More than one token is plugged in; pass --token-label (see --detect).")
        chosen = next((t for t in usable if not label or t.label == label), None)
        if chosen is None:
            raise SignerError(f"No plugged-in token is labelled {label!r} (see --detect).")
        lib, label, serial = chosen.driver.path, chosen.label, chosen.serial
        cert_id = chosen.certificates[0].cert_id if chosen.certificates else ""
        print(f"Using token {chosen.title}")
    pin = os.environ.get("SRIC_SIGNER_TEST_PIN", "") if args.pkcs12 else ""
    pin = pin or getpass.getpass("DSC PIN (goes only to the token): ")
    signer, session = open_signer(pkcs11_lib=lib, token_label=label, token_serial=serial, cert_id=cert_id,
                                  pkcs12=args.pkcs12 or "", pin=pin)
    try:
        done, failed = sign_items(portal, items, signer, box=queue["signature_box"], field_name=queue["signature_field"])
    finally:
        if session is not None:
            session.close()
    print(f"Done: {done} signed, {failed} not signed.")
    return 1 if failed else 0


def gstr1_cli(args) -> int:
    token = args.token or ""
    if args.token_file:
        with open(args.token_file, encoding="utf-8") as fh:
            token = fh.read().strip()
    portal = Portal(args.portal, token or getpass.getpass("Signing utility token: "))
    name, data = portal.gstr1(args.gstr1[0], args.gstr1[1], args.gstr1_format)
    path = os.path.join(args.out, name)
    with open(path, "wb") as fh:
        fh.write(data)
    print(f"Saved {path} ({len(data)} bytes)")
    return 0


# --- window ---------------------------------------------------------------------------------------------------------------

def _resource(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def gui() -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    cfg = load_config()
    root = tk.Tk()
    root.title(f"{APP} {VERSION}")
    root.geometry("980x680")
    try:
        root.iconbitmap(_resource(os.path.join("assets", "sric_signer.ico")))
    except Exception:  # noqa: BLE001 - the icon is cosmetic
        pass
    navy = "#153F79"
    state = {"portal": None, "queue": {}, "items": [], "tokens": [], "choices": []}

    top = tk.Frame(root, bg=navy)
    top.pack(fill="x")
    tk.Label(top, text=APP, fg="white", bg=navy, font=("Segoe UI", 14, "bold")).pack(side="left", padx=12, pady=8)
    status = tk.Label(top, text="Not connected", fg="#FDE68A", bg=navy, font=("Segoe UI", 9))
    status.pack(side="right", padx=12)

    conn = tk.Frame(root)
    conn.pack(fill="x", padx=12, pady=6)
    tk.Label(conn, text="Portal").grid(row=0, column=0, sticky="w")
    portal_var = tk.StringVar(value=cfg.get("portal", "https://equip.iitr.ac.in"))
    tk.Entry(conn, textvariable=portal_var, width=40).grid(row=0, column=1, sticky="w")
    tk.Label(conn, text="Utility token").grid(row=0, column=2, sticky="w", padx=(12, 0))
    token_var = tk.StringVar(value=cfg.get("token", ""))
    tk.Entry(conn, textvariable=token_var, width=34, show="*").grid(row=0, column=3, sticky="w")

    cols = ("number", "date", "buyer", "taxable", "tax", "total", "receipt")
    tree = ttk.Treeview(root, columns=cols, show="headings", selectmode="extended", height=11)
    for col, label, width in zip(cols, ("Invoice No.", "Date", "Buyer", "Taxable", "GST", "Total", "Receipt"),
                                 (130, 85, 270, 85, 75, 85, 170), strict=True):
        tree.heading(col, text=label)
        tree.column(col, width=width, anchor="w")
    tree.pack(fill="both", expand=True, padx=12)

    log = tk.Text(root, height=6, font=("Consolas", 9))

    def say(msg):
        log.insert("end", msg + "\n")
        log.see("end")
        root.update_idletasks()

    def connect():
        try:
            portal = Portal(portal_var.get(), token_var.get())
            who = portal.whoami()
            state["portal"] = portal
            status.config(text=f"{who['user']} | {who['device']} | {who['mode'].upper()}")
            cfg.update({"portal": portal_var.get(), "token": token_var.get()})
            save_config(cfg)
            refresh()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP, str(exc))

    def refresh():
        if state["portal"] is None:
            return
        try:
            state["queue"] = state["portal"].queue()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP, str(exc))
            return
        state["items"] = state["queue"]["results"]
        tree.delete(*tree.get_children())
        for i, item in enumerate(state["items"]):
            tree.insert("", "end", iid=str(i), values=(item["invoice_number"], item["invoice_date"], item["buyer"],
                                                         item["taxable_value"], item["tax"], item["total"],
                                                         item["receipt_number"] or "-"))
        say(f"{len(state['items'])} invoice(s) waiting for your signature.")

    def chosen_items():
        return [state["items"][int(i)] for i in tree.selection()]

    def preview():
        for item in chosen_items()[:3]:
            try:
                data = state["portal"].prepared_pdf(item)
                path = os.path.join(tempfile.gettempdir(), f"preview-{item['invoice_number'].replace('/', '-')}.pdf")
                with open(path, "wb") as fh:
                    fh.write(data)
                if hasattr(os, "startfile"):
                    os.startfile(path)  # noqa: S606 - opens the user's PDF reader
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror(APP, str(exc))

    # --- DSC token ---
    keys = tk.LabelFrame(root, text="DSC token")
    keys.pack(fill="x", padx=12, pady=6)
    source = tk.StringVar(value=cfg.get("source", "pkcs11"))
    tk.Radiobutton(keys, text="DSC token", variable=source, value="pkcs11").grid(row=0, column=0, sticky="w")
    token_choice = tk.StringVar()
    token_box = ttk.Combobox(keys, textvariable=token_choice, state="readonly", width=78)
    token_box.grid(row=0, column=1, sticky="w")
    token_note = tk.Label(keys, text="", fg="#92400E", anchor="w", justify="left", wraplength=760)
    token_note.grid(row=1, column=1, columnspan=4, sticky="w")
    tk.Radiobutton(keys, text="Test DSC file (.p12, sandbox only)", variable=source, value="pkcs12").grid(
        row=2, column=0, sticky="w")
    p12_var = tk.StringVar(value=cfg.get("pkcs12", ""))
    tk.Entry(keys, textvariable=p12_var, width=60).grid(row=2, column=1, sticky="w")
    tk.Button(keys, text="Browse", command=lambda: p12_var.set(filedialog.askopenfilename(
        filetypes=[("PKCS#12", "*.p12 *.pfx")]) or p12_var.get())).grid(row=2, column=2)
    tk.Label(keys, text="PIN").grid(row=0, column=4, padx=(16, 4))
    pin_var = tk.StringVar()
    tk.Entry(keys, textvariable=pin_var, show="•", width=14).grid(row=0, column=5)

    def show_tokens(result):
        drivers, tokens = result
        state["tokens"] = tokens
        choices = []
        for t in tokens:
            if t.error or not t.label:
                continue
            for c in t.certificates or [sric_token.TokenCert("no certificate on this token", "", "", "")]:
                choices.append((f"{t.label} ({t.manufacturer}) - {c.subject}"
                                + (f", valid until {c.not_after}" if c.not_after else ""), t, c))
        state["choices"] = choices
        token_box["values"] = [c[0] for c in choices]
        remembered = next((i for i, (_txt, t, c) in enumerate(choices)
                           if t.driver.path == cfg.get("pkcs11_lib") and t.label == cfg.get("token_label")
                           and (not cfg.get("cert_id") or c.cert_id == cfg.get("cert_id"))), 0 if choices else None)
        if remembered is not None:
            token_box.current(remembered)
        notes = [t.error for t in tokens if t.error]
        other_arch = [d for d in drivers if not d.usable]
        if not drivers:
            notes.append("No DSC token driver was found on this computer. Install the token's software from its "
                         "vendor, or use Browse for driver... to point to its PKCS#11 DLL.")
        elif not choices:
            notes.append(sric_token.NO_TOKEN)
        if other_arch and not choices:
            notes.append("Found only drivers for the other architecture (" + ", ".join(d.label for d in other_arch)
                         + f"); this signer is {sric_token.own_arch()}.")
        token_note.config(text=" ".join(dict.fromkeys(notes)))
        say(f"Token detection: {len(drivers)} driver(s), {len(choices)} certificate(s) on plugged-in tokens.")

    def run_detect():
        token_note.config(text="Looking for DSC tokens...")

        def work():
            try:
                result = detect(cfg.get("pkcs11_lib_manual", ""))
            except Exception as exc:  # noqa: BLE001
                root.after(0, lambda: token_note.config(text=f"Detection failed: {exc}"))
                return
            root.after(0, lambda: show_tokens(result))

        threading.Thread(target=work, daemon=True).start()

    def browse_driver():
        path = filedialog.askopenfilename(title="PKCS#11 driver of the DSC token",
                                          filetypes=[("PKCS#11 driver", "*.dll"), ("All files", "*.*")])
        if path:
            arch = sric_token.dll_arch(path)
            if arch and arch != sric_token.own_arch():
                messagebox.showwarning(APP, f"This driver is {arch}; this signer is {sric_token.own_arch()}. Choose "
                                            "the driver for the same architecture.")
                return
            cfg["pkcs11_lib_manual"] = path
            save_config(cfg)
            run_detect()

    bar = tk.Frame(keys)
    bar.grid(row=0, column=2, columnspan=2, padx=6)
    tk.Button(bar, text="Detect token", command=run_detect).pack(side="left")
    tk.Button(bar, text="Browse for driver...", command=browse_driver).pack(side="left", padx=4)

    def sign():
        items = chosen_items()
        if not items:
            messagebox.showinfo(APP, "Tick (select) the invoices to sign.")
            return
        lib = label = serial = cert_id = ""
        if source.get() == "pkcs11":
            if not state["choices"] or token_box.current() < 0:
                messagebox.showerror(APP, sric_token.NO_TOKEN)
                return
            _txt, tok, cert = state["choices"][token_box.current()]
            lib, label, serial, cert_id = tok.driver.path, tok.label, tok.serial, cert.cert_id
        total = sum(float(i["total"]) for i in items)
        if not messagebox.askyesno(APP, f"Sign {len(items)} invoice(s), total Rs. {total:,.2f}, with your DSC?"):
            return
        cfg.update({"source": source.get(), "pkcs12": p12_var.get(), "pkcs11_lib": lib, "token_label": label,
                    "cert_id": cert_id})
        save_config(cfg)
        session = None
        try:
            signer, session = open_signer(pkcs11_lib=lib, token_label=label, token_serial=serial, cert_id=cert_id,
                                          pkcs12=p12_var.get() if source.get() == "pkcs12" else "", pin=pin_var.get())
            done, failed = sign_items(state["portal"], items, signer, box=state["queue"]["signature_box"],
                                      field_name=state["queue"]["signature_field"], log=say)
            messagebox.showinfo(APP, f"{done} signed and uploaded; {failed} not signed.")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP, str(exc))
        finally:
            pin_var.set("")
            if session is not None:
                session.close()
        refresh()

    # --- GSTR-1 export (built by the portal; one file per month, a zip when the range covers several) ---
    export = tk.LabelFrame(root, text="Monthly export: GSTR-1 JSON and registers (B2C: buyers without a GSTIN)")
    export.pack(fill="x", padx=12, pady=(0, 6))
    import datetime as _dt

    first = _dt.date.today().replace(day=1)
    row1 = tk.Frame(export)
    row1.pack(fill="x")
    row2 = tk.Frame(export)
    row2.pack(fill="x")
    tk.Label(row1, text="From").pack(side="left", padx=(6, 2))
    from_var = tk.StringVar(value=first.isoformat())
    tk.Entry(row1, textvariable=from_var, width=12).pack(side="left")
    tk.Label(row1, text="To").pack(side="left", padx=(8, 2))
    to_var = tk.StringVar(value=_dt.date.today().isoformat())
    tk.Entry(row1, textvariable=to_var, width=12).pack(side="left")
    tk.Label(row1, text="(one file of each kind per month; a zip when there are several)").pack(side="left", padx=8)
    kind_var = tk.StringVar(value="all")
    for value, text in EXPORT_KINDS:
        tk.Radiobutton(row2, text=text, variable=kind_var, value=value).pack(side="left", padx=(6, 0))

    def save_gstr1():
        if state["portal"] is None:
            messagebox.showinfo(APP, "Connect to the portal first.")
            return
        try:
            name, data = state["portal"].gstr1(from_var.get().strip(), to_var.get().strip(), kind_var.get())
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP, str(exc))
            return
        ext = os.path.splitext(name)[1]
        path = filedialog.asksaveasfilename(initialfile=name, defaultextension=ext,
                                            filetypes=[("Export file", f"*{ext}"), ("All files", "*.*")])
        if path:
            with open(path, "wb") as fh:
                fh.write(data)
            say(f"Saved {path}" + (" (the month's files inside)" if ext == ".zip" else ""))

    tk.Button(row1, text="Save export...", command=save_gstr1).pack(side="right", padx=6)

    actions = tk.Frame(root)
    actions.pack(fill="x", padx=12, pady=(0, 4))
    tk.Button(actions, text="Connect", command=connect).pack(side="left")
    tk.Button(actions, text="Refresh", command=refresh).pack(side="left", padx=6)
    tk.Button(actions, text="Preview selected", command=preview).pack(side="left")
    tk.Button(actions, text="Sign selected with DSC", bg=navy, fg="white", command=sign).pack(side="right")
    log.pack(fill="x", padx=12, pady=(0, 10))
    root.after(100, run_detect)
    if cfg.get("token"):
        root.after(300, connect)
    root.mainloop()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=APP)
    ap.add_argument("--version", action="version", version=f"{APP} {VERSION}")
    ap.add_argument("--cli", action="store_true", help="command line instead of the window")
    ap.add_argument("--detect", action="store_true", help="list token drivers and plugged-in tokens")
    ap.add_argument("--self-test", metavar="REPORT_FILE", help="check the bundled pieces load; write a report")
    ap.add_argument("--portal", default="https://equip.iitr.ac.in")
    ap.add_argument("--token", help="signing utility token (prefer --token-file or the prompt)")
    ap.add_argument("--token-file")
    ap.add_argument("--pkcs11-lib", help="the DSC token's PKCS#11 driver (default: detected)")
    ap.add_argument("--token-label", help="token label, when several tokens are plugged in")
    ap.add_argument("--pkcs12", help="TEST DSC file, sandbox demonstration only")
    ap.add_argument("--all", action="store_true", help="sign every invoice waiting")
    ap.add_argument("--gstr1", nargs=2, metavar=("FROM", "TO"),
                    help="save the monthly B2C export (GSTR-1 JSON and registers), dates YYYY-MM-DD")
    ap.add_argument("--gstr1-format", choices=[k for k, _t in EXPORT_KINDS] + ["xlsx"], default="json",
                    help="json: GSTR-1 JSON; invoice_register / credit_note_register: Excel registers; all: the "
                         "three in one zip; xlsx: the offline tool's Excel template")
    ap.add_argument("--out", default=".", help="folder for the exported file")
    ap.add_argument("--invoice", action="append", help="sign this invoice number (repeatable)")
    args = ap.parse_args(argv)
    if args.self_test:
        try:
            report, code = self_test(), 0
        except Exception as exc:  # noqa: BLE001
            report, code = f"SELF-TEST FAILED: {type(exc).__name__}: {exc}", 3
        with open(args.self_test, "w", encoding="utf-8") as fh:
            fh.write(report + "\n")
        return code
    try:
        if args.detect:
            report = detect_report(args.pkcs11_lib or "")
            if sys.stdout is None:  # the windowed exe has no console: show the report instead
                import tkinter as tk
                from tkinter import messagebox

                tk.Tk().withdraw()
                messagebox.showinfo(APP, report)
            else:
                print(report)
            return 0
        if args.gstr1:
            return gstr1_cli(args)
        return cli(args) if args.cli else gui()
    except SignerError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

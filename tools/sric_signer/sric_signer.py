"""SRIC Invoice Signer: signs the portal's tax invoices (buyers without a GSTIN) with the signatory's DSC, on the
signatory's own computer. Dean SRIC proposal, section 4 and Figure 3.

* The utility lists the invoices waiting for signature (portal API, device token linked in the portal under
  GST invoices > Signing). The signatory ticks them, previews each PDF, inserts the DSC token and enters its PIN here.
* The PIN goes only to the token (PKCS#11); the private key never leaves the token. Nothing but the signed PDFs is
  uploaded, and the portal accepts a signed PDF only if it verifies with the signatory's registered certificate and
  matches the prepared invoice byte for byte.
* Before signing, the downloaded PDF's SHA-256 is compared with the one the portal listed, so the signatory signs
  exactly what was prepared.

Key sources:
    --pkcs11-lib  path to the token's PKCS#11 library (e.g. eToken: eTPKCS11.dll, ePass2003: eps2003csp11.dll,
                  ProxKey: SignatureP11.dll). The real DSC.
    --pkcs12      a .p12/.pfx file. ONLY for the sandbox demonstration with a TEST DSC (make_test_dsc.py).

Run without arguments for the window; with --cli for the command line (demonstration / scripted use):

    python sric_signer.py
    python sric_signer.py --cli --portal https://equip.iitr.ac.in --token-file token.txt --pkcs11-lib eTPKCS11.dll --all
    python sric_signer.py --cli --portal http://localhost:8000 --token-file token.txt --pkcs12 test-dsc.p12 --all
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
from urllib.parse import urlparse

import requests

APP = "SRIC Invoice Signer"
VERSION = "1.0.0"
CONFIG = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "SRIC Invoice Signer", "config.json")
TIMEOUT = (10, 60)


class SignerError(Exception):
    pass


# --- portal ------------------------------------------------------------------------------------------------------------

class Portal:
    def __init__(self, base: str, token: str):
        parsed = urlparse(base)
        local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
            raise SignerError("The portal address must start with https:// (http:// only for localhost).")
        self.base = base.rstrip("/")
        self.http = requests.Session()
        self.http.headers.update({"Authorization": f"SricSigner {token.strip()}", "User-Agent": f"{APP}/{VERSION}"})

    def _get(self, path: str):
        resp = self.http.get(self.base + path, timeout=TIMEOUT)
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

    def upload(self, item: dict, signed: bytes) -> dict:
        name = f"{item['invoice_number'].replace('/', '-')}-signed.pdf"
        resp = self.http.post(self.base + item["upload_url"], files={"file": (name, signed, "application/pdf")},
                              timeout=TIMEOUT)
        if resp.status_code >= 400:
            raise SignerError(f"Invoice {item['invoice_number']}: {self._message(resp)}")
        return resp.json()


# --- signing ------------------------------------------------------------------------------------------------------------

def open_signer(*, pkcs11_lib: str = "", token_label: str = "", pkcs12: str = "", pin: str):
    """A pyHanko signer. PKCS#11: the PIN logs in to the token and the token signs. PKCS#12: test DSC only."""
    from pyhanko.sign import signers

    if pkcs11_lib:
        from pyhanko.sign import pkcs11

        session = pkcs11.open_pkcs11_session(pkcs11_lib, token_label=token_label or None, user_pin=pin)
        return pkcs11.PKCS11Signer(session, prefer_pss=False), session
    if pkcs12:
        signer = signers.SimpleSigner.load_pkcs12(pfx_file=pkcs12, passphrase=pin.encode() if pin else None)
        if signer is None:
            raise SignerError("The test DSC file could not be opened (wrong PIN or not a .p12 file).")
        return signer, None
    raise SignerError("Choose the DSC token (PKCS#11 library) or, for the sandbox only, a test DSC file.")


def sign_pdf(data: bytes, signer, *, box, field_name: str, signer_name: str) -> bytes:
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
    signers.PdfSigner(meta, signer=signer, stamp_style=style).sign_pdf(writer, output=out)
    return out.getvalue()


def sign_items(portal: Portal, items: list[dict], signer, *, box, field_name: str, log=print) -> tuple[int, int]:
    done = failed = 0
    me = portal.whoami().get("user", "")
    for item in items:
        try:
            data = portal.prepared_pdf(item)
            signed = sign_pdf(data, signer, box=box, field_name=field_name, signer_name=me)
            result = portal.upload(item, signed)
            log(f"Signed and uploaded {item['invoice_number']} ({item['buyer']}, Rs. {item['total']}): {result['status']}")
            done += 1
        except Exception as exc:  # noqa: BLE001 - report each invoice and carry on with the rest
            log(f"NOT signed {item.get('invoice_number')}: {exc}")
            failed += 1
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
    pin = os.environ.get("SRIC_SIGNER_TEST_PIN", "") if args.pkcs12 else ""
    pin = pin or getpass.getpass("DSC PIN (goes only to the token): ")
    signer, session = open_signer(pkcs11_lib=args.pkcs11_lib or "", token_label=args.token_label or "",
                                  pkcs12=args.pkcs12 or "", pin=pin)
    try:
        done, failed = sign_items(portal, items, signer, box=queue["signature_box"], field_name=queue["signature_field"])
    finally:
        if session is not None:
            session.close()
    print(f"Done: {done} signed, {failed} not signed.")
    return 1 if failed else 0


# --- window ---------------------------------------------------------------------------------------------------------------

def gui() -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    cfg = load_config()
    root = tk.Tk()
    root.title(f"{APP} {VERSION}")
    root.geometry("900x600")
    navy = "#153F79"
    state = {"portal": None, "queue": {}, "items": []}

    top = tk.Frame(root, bg=navy)
    top.pack(fill="x")
    tk.Label(top, text="SRIC Invoice Signer", fg="white", bg=navy, font=("Segoe UI", 14, "bold")).pack(side="left", padx=12, pady=8)
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
    tree = ttk.Treeview(root, columns=cols, show="headings", selectmode="extended", height=12)
    for col, label, width in zip(cols, ("Invoice No.", "Date", "Buyer", "Taxable", "GST", "Total", "Receipt"),
                                 (130, 85, 250, 85, 75, 85, 150)):
        tree.heading(col, text=label)
        tree.column(col, width=width, anchor="w")
    tree.pack(fill="both", expand=True, padx=12)

    log = tk.Text(root, height=7, font=("Consolas", 9))
    log.pack(fill="x", padx=12, pady=(6, 0))

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
        state["queue"] = state["portal"].queue()
        state["items"] = state["queue"]["results"]
        tree.delete(*tree.get_children())
        for i, item in enumerate(state["items"]):
            tree.insert("", "end", iid=str(i), values=(item["invoice_number"], item["invoice_date"], item["buyer"],
                                                         item["taxable_value"], item["tax"], item["total"],
                                                         item["receipt_number"] or "-"))
        say(f"{len(state['items'])} invoice(s) waiting for your signature.")

    def chosen():
        return [state["items"][int(i)] for i in tree.selection()]

    def preview():
        for item in chosen()[:3]:
            try:
                data = state["portal"].prepared_pdf(item)
                path = os.path.join(tempfile.gettempdir(), f"preview-{item['invoice_number'].replace('/', '-')}.pdf")
                with open(path, "wb") as fh:
                    fh.write(data)
                os.startfile(path) if hasattr(os, "startfile") else None  # noqa: B018
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror(APP, str(exc))

    keys = tk.LabelFrame(root, text="DSC")
    keys.pack(fill="x", padx=12, pady=6)
    source = tk.StringVar(value=cfg.get("source", "pkcs11"))
    tk.Radiobutton(keys, text="DSC token (PKCS#11 library)", variable=source, value="pkcs11").grid(row=0, column=0, sticky="w")
    lib_var = tk.StringVar(value=cfg.get("pkcs11_lib", ""))
    tk.Entry(keys, textvariable=lib_var, width=48).grid(row=0, column=1, sticky="w")
    tk.Button(keys, text="Browse", command=lambda: lib_var.set(filedialog.askopenfilename(
        filetypes=[("PKCS#11 library", "*.dll *.so *.dylib")]) or lib_var.get())).grid(row=0, column=2)
    tk.Radiobutton(keys, text="Test DSC file (.p12, sandbox only)", variable=source, value="pkcs12").grid(row=1, column=0, sticky="w")
    p12_var = tk.StringVar(value=cfg.get("pkcs12", ""))
    tk.Entry(keys, textvariable=p12_var, width=48).grid(row=1, column=1, sticky="w")
    tk.Button(keys, text="Browse", command=lambda: p12_var.set(filedialog.askopenfilename(
        filetypes=[("PKCS#12", "*.p12 *.pfx")]) or p12_var.get())).grid(row=1, column=2)
    tk.Label(keys, text="PIN").grid(row=0, column=3, padx=(16, 4))
    pin_var = tk.StringVar()
    tk.Entry(keys, textvariable=pin_var, show="•", width=14).grid(row=0, column=4)

    def sign():
        items = chosen()
        if not items:
            messagebox.showinfo(APP, "Tick (select) the invoices to sign.")
            return
        total = sum(float(i["total"]) for i in items)
        if not messagebox.askyesno(APP, f"Sign {len(items)} invoice(s), total Rs. {total:,.2f}, with your DSC?"):
            return
        cfg.update({"source": source.get(), "pkcs11_lib": lib_var.get(), "pkcs12": p12_var.get()})
        save_config(cfg)
        session = None
        try:
            signer, session = open_signer(pkcs11_lib=lib_var.get() if source.get() == "pkcs11" else "",
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

    bar = tk.Frame(root)
    bar.pack(fill="x", padx=12, pady=(0, 10))
    tk.Button(bar, text="Connect", command=connect).pack(side="left")
    tk.Button(bar, text="Refresh", command=refresh).pack(side="left", padx=6)
    tk.Button(bar, text="Preview selected", command=preview).pack(side="left")
    tk.Button(bar, text="Sign selected with DSC", bg=navy, fg="white", command=sign).pack(side="right")
    if cfg.get("token"):
        root.after(200, connect)
    root.mainloop()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=APP)
    ap.add_argument("--cli", action="store_true", help="command line instead of the window")
    ap.add_argument("--portal", default="https://equip.iitr.ac.in")
    ap.add_argument("--token", help="signing utility token (prefer --token-file or the prompt)")
    ap.add_argument("--token-file")
    ap.add_argument("--pkcs11-lib", help="the DSC token's PKCS#11 library")
    ap.add_argument("--token-label", help="token label, when several tokens are inserted")
    ap.add_argument("--pkcs12", help="TEST DSC file, sandbox demonstration only")
    ap.add_argument("--all", action="store_true", help="sign every invoice waiting")
    ap.add_argument("--invoice", action="append", help="sign this invoice number (repeatable)")
    args = ap.parse_args(argv)
    try:
        return cli(args) if args.cli else gui()
    except SignerError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

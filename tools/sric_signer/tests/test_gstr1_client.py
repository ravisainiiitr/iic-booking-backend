"""The utility only asks the portal for the GSTR-1 file and saves it under the portal's file name."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sric_signer  # noqa: E402


class Resp:
    status_code = 200
    content = b'{"gstin": "05AAALI0033R1Z5", "fp": "112026"}'
    headers = {"Content-Disposition": 'attachment; filename="GSTR1-B2C-05AAALI0033R1Z5-112026-sandbox.json"'}


def test_gstr1_request_and_file_name(monkeypatch):
    portal = sric_signer.Portal("http://localhost:8000", "sricsign_x")
    seen = {}
    monkeypatch.setattr(portal.http, "get", lambda url, timeout=None: seen.setdefault("url", url) and Resp())
    name, data = portal.gstr1("2026-11-01", "2026-11-30", "json")
    assert seen["url"].endswith("/api/v1/gst-einvoice/signer/api/gstr1/?from=2026-11-01&to=2026-11-30&kind=json")
    assert name == "GSTR1-B2C-05AAALI0033R1Z5-112026-sandbox.json" and data.startswith(b"{")


def test_all_three_files_and_the_registers_are_offered(monkeypatch, tmp_path):
    """The window and the command line offer GSTR-1 JSON, Invoice Register, Credit Note Register and all three."""
    kinds = [k for k, _t in sric_signer.EXPORT_KINDS]
    assert kinds == ["all", "json", "invoice_register", "credit_note_register", "b2b_register"]

    class Zip(Resp):
        content = b"PK\x03\x04"
        headers = {"Content-Disposition":
                   'attachment; filename="GSTR1-and-registers-B2C-05AAALI0033R1Z5-112026-sandbox.zip"'}

    seen = []
    monkeypatch.setattr(sric_signer.requests.Session, "get",
                        lambda self, url, timeout=None: seen.append(url) or Zip())
    rc = sric_signer.main(["--portal", "http://localhost:8000", "--token", "sricsign_x", "--gstr1", "2026-11-01",
                           "2026-11-30", "--gstr1-format", "all", "--out", str(tmp_path)])
    assert rc == 0 and seen[-1].endswith("&kind=all")
    assert (tmp_path / "GSTR1-and-registers-B2C-05AAALI0033R1Z5-112026-sandbox.zip").read_bytes() == b"PK\x03\x04"

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

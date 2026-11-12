"""API access: who sees what, the signing utility's token-only API, and the buyer's view."""

import pytest

from iic_booking.gst_einvoice import service
from iic_booking.gst_einvoice.models import InvoiceStatus

from . import signing
from .conftest import client_for, completed_request, main_admin, make_user

pytestmark = pytest.mark.django_db
API = "/api/v1/gst-einvoice/"


def test_status_shows_sandbox_and_roles(db):
    body = client_for(make_user()).get(f"{API}status/").json()
    assert body["mode"] == "sandbox" and body["production_available"] is False
    assert body["can_review"] is False and "settings" not in body
    body = client_for(make_user(user_type="finance")).get(f"{API}status/").json()
    assert body["can_review"] is True and body["tax_rules"][0]["sac_code"] == "998397"


def test_only_reviewers_see_invoices(factory, ext_on):
    req = completed_request(factory)
    inv = service.prepare_from_request(req)
    assert client_for(make_user()).get(f"{API}invoices/").status_code == 403
    assert client_for(make_user(user_type="operator")).get(f"{API}invoices/{inv.pk}/").status_code == 403
    r = client_for(make_user(user_type="finance")).get(f"{API}invoices/?supply=B2B")
    assert r.status_code == 200 and r.json()["results"][0]["irn"]
    detail = client_for(main_admin()).get(f"{API}invoices/{inv.pk}/").json()
    assert detail["lines"][0]["sac_code"] == "998397" and detail["events"]


def test_buyer_sees_own_invoice_only(factory, ext_on):
    req = completed_request(factory)
    inv = service.prepare_from_request(req)
    owner = req.booking.user
    body = client_for(owner).get(f"{API}my/bookings/{req.booking_id}/").json()
    assert body["invoice"]["number"] == inv.invoice_number and body["invoice"]["download_url"]
    assert client_for(make_user()).get(f"{API}my/bookings/{req.booking_id}/").json()["invoice"] is None
    assert client_for(owner).get(f"{API}invoices/{inv.pk}/pdf/").status_code == 200
    assert client_for(make_user()).get(f"{API}invoices/{inv.pk}/pdf/").status_code == 404


def test_demo_invoice_and_register_csv(db):
    admin = client_for(main_admin())
    r = admin.post(f"{API}invoices/demo/", {"legal_name": "Demo Analytics Pvt Ltd", "gstin": "07AAACX0000X1Z2",
                                            "address1": "1 Demo Street", "location": "New Delhi", "pincode": "110001",
                                            "amount": "9000"}, format="json")
    assert r.status_code == 201 and r.json()["status"] == InvoiceStatus.IRN_GENERATED
    csv = admin.get(f"{API}register/?export=csv")
    assert csv.status_code == 200 and b"SANDBOX" in csv.content and r.json()["invoice_number"].encode() in csv.content
    assert admin.get(f"{API}register/?month=2026-13").status_code == 400


def test_certificate_device_and_signing_api(factory, ext_on):
    req = completed_request(factory, gstin="", billing_state="Uttarakhand", billing_pincode="247667")
    inv = service.prepare_from_request(req)
    signatory = make_user(user_type="finance")
    key, cert = signing.test_identity()
    from django.core.files.uploadedfile import SimpleUploadedFile

    upload = SimpleUploadedFile("dsc.cer", signing.cert_pem(cert))
    assert client_for(make_user(user_type="finance")).post(
        f"{API}certificates/", {"user_email": signatory.email, "file": upload}).status_code == 403
    upload.seek(0)
    r = client_for(main_admin()).post(f"{API}certificates/", {"user_email": signatory.email, "file": upload,
                                                               "is_test": "true"})
    assert r.status_code == 201 and r.json()["is_test"]
    r = client_for(signatory).post(f"{API}signer/devices/", {"name": "SRIC PC"}, format="json")
    assert r.status_code == 201
    token = r.json()["token"]
    assert client_for(signatory).get(f"{API}signer/devices/").json()["results"][0].get("token") is None

    tool = client_for()
    assert tool.get(f"{API}signer/api/queue/").status_code == 401
    assert tool.get(f"{API}signer/api/queue/", HTTP_AUTHORIZATION="SricSigner nope").status_code == 401
    auth = {"HTTP_AUTHORIZATION": f"SricSigner {token}"}
    queue = tool.get(f"{API}signer/api/queue/", **auth).json()
    item = queue["results"][0]
    assert item["invoice_number"] == inv.invoice_number and queue["signature_field"] == "SRIC_Signatory"
    pdf = b"".join(tool.get(item["pdf_url"], **auth).streaming_content) if hasattr(
        tool.get(item["pdf_url"], **auth), "streaming_content") else tool.get(item["pdf_url"], **auth).content
    signed = signing.sign(pdf, key, cert)
    r = tool.post(item["upload_url"], {"file": SimpleUploadedFile("s.pdf", signed, "application/pdf")}, **auth)
    assert r.status_code == 200 and r.json()["status"] == InvoiceStatus.SIGNED
    # A session-only signatory endpoint is not reachable with the device token, and vice versa.
    assert tool.get(f"{API}invoices/", **auth).status_code in (401, 403)
    # Revoked: the token stops working.
    device_id = client_for(signatory).get(f"{API}signer/devices/").json()["results"][0]["id"]
    client_for(signatory).post(f"{API}signer/devices/{device_id}/revoke/")
    assert tool.get(f"{API}signer/api/queue/", **auth).status_code == 401


def test_switched_off_module_answers_nothing(db, settings):
    settings.GST_EINVOICE_ENABLED = False
    assert client_for(main_admin()).get(f"{API}invoices/").status_code == 403
    assert client_for(make_user()).get(f"{API}my/bookings/1/").json() == {"enabled": False, "invoice": None}

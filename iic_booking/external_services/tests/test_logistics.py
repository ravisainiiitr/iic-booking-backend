"""Saved addresses, printed invoice copies, 10 x 4.5 in labels and QR sample receipt."""

import re
from datetime import timedelta

import pytest
from django.utils import timezone

from iic_booking.equipment.models import BookingSampleTrace, EquipmentOperator
from iic_booking.external_services import logistics
from iic_booking.external_services.models import (
    CopyRequestStatus,
    ExternalServicesSettings,
    InvoiceRequestStatus,
    PhysicalCopyRequest,
    SampleReceiptScan,
    SampleShipmentLabel,
    SavedPostalAddress,
    SricInvoiceRequest,
)
from iic_booking.users.models.user_type import UserType

from .conftest import client_for, make_user

pytestmark = pytest.mark.django_db
API = "/api/v1/external-booking/"
ADDRESS = {"recipient_name": "Dr Asha Verma", "organisation": "Asha Labs", "line1": "12 Station Road",
           "city": "Roorkee", "state": "Uttarakhand", "postal_code": "247667", "country": "India",
           "phone": "+91 98765 43210"}
IIC_ADDRESS = "The Head\nInstitute Instrumentation Centre\nIndian Institute of Technology Roorkee\nRoorkee 247667"


@pytest.fixture
def on():
    s = ExternalServicesSettings.load()
    s.invoice_requests_enabled = s.physical_copies_enabled = s.sample_labels_enabled = True
    s.iic_postal_address = IIC_ADDRESS
    s.save()
    return s


@pytest.fixture
def booking(factory):
    user = make_user()
    from iic_booking.users.models import ExternalBillingProfile

    ExternalBillingProfile.objects.create(user=user, billing_name="Asha Labs", billing_address_line1="12 Station Road",
                                          billing_city="Roorkee", billing_state="Uttarakhand", billing_pincode="247667")
    return factory.booking(user, factory.equipment(), factory.at(timezone.localdate() + timedelta(days=6)),
                           payment_settled_at=timezone.now())


# --- addresses ------------------------------------------------------------------------------------------------------

def test_address_book_is_private_and_validated(on):
    user, other = make_user(), make_user()
    r = client_for(user).post(f"{API}addresses/", ADDRESS | {"is_default": True}, format="json")
    assert r.status_code == 201, r.content
    aid = r.json()["id"]
    assert client_for(other).patch(f"{API}addresses/{aid}/", {"city": "X"}, format="json").status_code == 404
    assert client_for(other).delete(f"{API}addresses/{aid}/").status_code == 404
    assert client_for(other).get(f"{API}addresses/").json()["addresses"] == []
    for field, bad in (("postal_code", "047667"), ("postal_code", "24766"), ("recipient_name", ""),
                       ("phone", "call me")):
        r = client_for(user).post(f"{API}addresses/", ADDRESS | {field: bad}, format="json")
        assert r.status_code == 400 and r.json()["field"] == field
    foreign = client_for(user).post(f"{API}addresses/", ADDRESS | {"country": "Nepal", "postal_code": "44600"},
                                    format="json")
    assert foreign.status_code == 201  # PIN format applies to Indian addresses only


def test_one_default_address(on):
    user = make_user()
    a = client_for(user).post(f"{API}addresses/", ADDRESS | {"is_default": True}, format="json").json()["id"]
    b = client_for(user).post(f"{API}addresses/", ADDRESS | {"label": "Lab", "is_default": True},
                              format="json").json()["id"]
    defaults = SavedPostalAddress.objects.filter(user=user, is_default=True).values_list("pk", flat=True)
    assert list(defaults) == [b] and a != b


# --- printed copies --------------------------------------------------------------------------------------------------

def _invoice_request(booking, status=InvoiceRequestStatus.SENT):
    return SricInvoiceRequest.objects.create(
        booking=booking, trigger="completion", status=status,
        snapshot={"booking_reference": booking.virtual_booking_id, "fbr_number": "FBR-77"},
    )


def test_copy_needs_an_invoice_request_and_waits_for_the_invoice(on, booking):
    user = booking.user
    r = client_for(user).post(f"{API}bookings/{booking.pk}/invoice/copy/", {"address": ADDRESS}, format="json")
    assert r.status_code == 400 and r.json()["code"] == "no_invoice_request"
    req = _invoice_request(booking)
    r = client_for(user).post(f"{API}bookings/{booking.pk}/invoice/copy/", {"address": ADDRESS}, format="json")
    assert r.status_code == 201 and r.json()["status"] == CopyRequestStatus.WAITING_FOR_INVOICE
    r = client_for(user).post(f"{API}bookings/{booking.pk}/invoice/copy/", {"address": ADDRESS}, format="json")
    assert r.status_code == 400 and r.json()["code"] == "already_requested"
    logistics.release_waiting_copies(req)
    assert PhysicalCopyRequest.objects.get(invoice_request=req).status == CopyRequestStatus.REQUESTED


def test_address_snapshot_is_immutable(on, booking):
    user = booking.user
    _invoice_request(booking)
    aid = client_for(user).post(f"{API}addresses/", ADDRESS, format="json").json()["id"]
    client_for(user).post(f"{API}bookings/{booking.pk}/invoice/copy/", {"address_id": aid}, format="json")
    client_for(user).patch(f"{API}addresses/{aid}/", ADDRESS | {"line1": "New Street 9"}, format="json")
    copy = PhysicalCopyRequest.objects.get(requested_by=user)
    assert copy.address_snapshot["line1"] == "12 Station Road"
    assert client_for(user).delete(f"{API}addresses/{aid}/").json()["result"] == "archived"  # used: kept


def test_dispatch_workflow_is_staff_only_and_audited(on, booking):
    user = booking.user
    _invoice_request(booking)

    copy_id = client_for(user).post(f"{API}bookings/{booking.pk}/invoice/copy/", {"address": ADDRESS},
                                    format="json").json()["id"]
    PhysicalCopyRequest.objects.filter(pk=copy_id).update(status=CopyRequestStatus.REQUESTED)
    finance = make_user(UserType.FINANCE, department=booking.equipment.internal_department, is_staff=True)
    assert client_for(user).post(f"{API}staff/invoice-copies/{copy_id}/prepared/").status_code in (403, 404)
    r = client_for(finance).get(f"{API}staff/invoice-copies/{copy_id}/envelope/")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert client_for(finance).post(f"{API}staff/invoice-copies/{copy_id}/prepared/").status_code == 200
    future = (timezone.localdate() + timedelta(days=2)).isoformat()
    r = client_for(finance).post(f"{API}staff/invoice-copies/{copy_id}/dispatched/", {"dispatch_date": future},
                                 format="json")
    assert r.status_code == 400
    r = client_for(finance).post(f"{API}staff/invoice-copies/{copy_id}/dispatched/",
                                 {"dispatch_date": timezone.localdate().isoformat(),
                                  "speed_post_reference": "EE123456789IN"}, format="json")
    assert r.status_code == 200 and r.json()["status"] == "dispatched"
    actions = [e["action"] for e in r.json()["events"]]
    assert actions == ["requested", "prepared", "dispatched"]
    assert client_for(user).post(f"{API}invoice-copies/{copy_id}/cancel/").status_code == 400


# --- labels ---------------------------------------------------------------------------------------------------------

def _mediabox(pdf: bytes):
    m = re.search(rb"/MediaBox\s*\[\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*\]", pdf)
    return tuple(float(x) for x in m.groups()) if m else None


def test_envelope_label_is_exactly_10_by_4_5_inches(on):
    from iic_booking.external_services.documents import envelope_label_pdf

    long_lines = ["Dr " + "Very Long Name " * 8, "Department of " + "Advanced Materials " * 6, "Roorkee 247667"]
    pdf = envelope_label_pdf(recipient_lines=long_lines, reference="IIC123 · FBR-77")
    assert _mediabox(pdf) == (0.0, 0.0, 720.0, 324.0)


def test_labels_refuse_without_a_configured_iic_address(on, booking):
    on.iic_postal_address = ""
    on.save()
    from iic_booking.external_services.documents import DocumentError, envelope_label_pdf

    with pytest.raises(DocumentError):
        envelope_label_pdf(recipient_lines=["A"], reference="R")


def test_sample_label_qr_holds_only_a_random_token(on, booking):
    user = booking.user
    r = client_for(user).post(f"{API}bookings/{booking.pk}/sample-label/",
                              {"sample_count": 3, "sender": ADDRESS, "description": "Films"}, format="json")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert _mediabox(r.content) == (0.0, 0.0, 720.0, 324.0)
    label = SampleShipmentLabel.objects.get(booking=booking)
    assert len(label.token_sha256) == 64 and re.fullmatch(r"[A-Z2-9]{8}", label.reference)
    assert user.email.encode() not in r.content  # no personal contact data on the label
    second = client_for(user).post(f"{API}bookings/{booking.pk}/sample-label/", {"sample_count": 3, "sender": ADDRESS},
                                   format="json")
    assert second.status_code == 200
    label.refresh_from_db()
    assert label.revoked_at is not None  # a new label revokes the old one
    assert client_for(make_user()).post(f"{API}bookings/{booking.pk}/sample-label/", {"sample_count": 1,
                                        "sender": ADDRESS}, format="json").status_code == 404


def test_sample_label_prints_booking_id_and_both_mobiles(on, booking, monkeypatch):
    from iic_booking.external_services import documents

    on.iic_postal_address = IIC_ADDRESS + "\nMobile: +91 01332 000000"
    on.save()
    seen = {}
    real = documents.sample_label_pdf
    monkeypatch.setattr(documents, "sample_label_pdf", lambda **kw: seen.update(kw) or real(**kw))
    r = client_for(booking.user).post(f"{API}bookings/{booking.pk}/sample-label/",
                                      {"sample_count": 2, "sender": ADDRESS}, format="json")
    assert r.status_code == 200 and _mediabox(r.content) == (0.0, 0.0, 720.0, 324.0)
    assert seen["sender_mobile"] == ADDRESS["phone"]
    assert not any("Phone" in line for line in seen["sender_lines"])  # printed once, as the FROM mobile
    assert seen["booking_reference"]


def test_iic_mobile_is_taken_out_of_the_address_lines():
    from iic_booking.external_services.documents import split_phone_lines

    lines, mobile = split_phone_lines(["The Head", "IIT Roorkee", "Mobile No: +91 01332 000000", "Phone: 2"])
    assert lines == ["The Head", "IIT Roorkee"] and mobile == "+91 01332 000000"
    assert split_phone_lines(["The Head", "Roorkee 247667"]) == (["The Head", "Roorkee 247667"], "")


# --- QR receipt -----------------------------------------------------------------------------------------------------

def _label(booking):
    token = "tok-" + "x" * 20
    label = SampleShipmentLabel.objects.create(booking=booking, token_sha256=logistics._hash(token),
                                               reference="ABCD2345", sample_count=2)
    return label, token


def test_lookup_needs_authorised_staff_and_changes_nothing(on, booking):
    label, token = _label(booking)
    finance = make_user(UserType.FINANCE, department=booking.equipment.internal_department, is_staff=True)
    assert client_for(booking.user).post(f"{API}staff/sample-receipt/lookup/", {"code": token},
                                         format="json").status_code == 404
    r = client_for(finance).post(f"{API}staff/sample-receipt/lookup/", {"code": token}, format="json")
    assert r.status_code == 200, r.content
    assert r.json()["booking_id"] == booking.pk and r.json()["already_received"] is False
    assert not BookingSampleTrace.objects.filter(booking=booking).exists()  # scanning never marks receipt
    assert SampleReceiptScan.objects.filter(label=label, action="looked_up").count() == 1


def test_tampered_or_revoked_codes_are_not_found(on, booking):
    label, token = _label(booking)
    finance = make_user(UserType.FINANCE, department=booking.equipment.internal_department, is_staff=True)
    assert client_for(finance).post(f"{API}staff/sample-receipt/lookup/", {"code": token + "a"},
                                    format="json").status_code == 404
    label.revoked_at = timezone.now()
    label.save(update_fields=["revoked_at"])
    assert client_for(finance).post(f"{API}staff/sample-receipt/lookup/", {"code": token},
                                    format="json").status_code == 404


def test_confirm_records_receipt_once(on, booking):
    label, token = _label(booking)
    finance = make_user(UserType.FINANCE, department=booking.equipment.internal_department, is_staff=True)
    url = f"{API}staff/sample-receipt/confirm/"
    r = client_for(finance).post(url, {"code": token, "outcome": "held_at_office"}, format="json")
    assert r.status_code == 400 and r.json()["field"] == "notes"  # a reason is required, as for the status endpoint
    r = client_for(finance).post(url, {"code": token, "outcome": "forwarded_to_lab"}, format="json")
    assert r.status_code == 200 and r.json()["recorded"] is True
    trace = BookingSampleTrace.objects.get(booking=booking)
    assert trace.status == "FORWARDED_TO_LAB" and trace.created_by == finance
    r = client_for(finance).post(url, {"code": token, "outcome": "forwarded_to_lab"}, format="json")
    assert r.status_code == 200 and r.json()["recorded"] is False
    assert BookingSampleTrace.objects.filter(booking=booking).count() == 1
    assert list(SampleReceiptScan.objects.filter(label=label).values_list("action", flat=True)) == [
        "confirmed", "duplicate"]


def test_operator_of_another_equipment_cannot_receive(on, booking, factory):
    label, token = _label(booking)
    operator = make_user(UserType.OPERATOR, department=factory.department, is_staff=True)
    EquipmentOperator.objects.create(equipment=factory.equipment(), operator=operator)
    r = client_for(operator).post(f"{API}staff/sample-receipt/confirm/",
                                  {"code": token, "outcome": "forwarded_to_lab"}, format="json")
    assert r.status_code == 404
    assert not BookingSampleTrace.objects.filter(booking=booking).exists()

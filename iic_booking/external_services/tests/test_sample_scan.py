"""Scanning a sample label: the Accounts In Charge of the booking's department receives it at the office, then the
equipment's Officer In Charge or lab in-charge receives it at the lab; later scans only show the booking. Nobody types
a code: opening the QR link (``scan``) is the action, decided by the server for the signed-in user."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from iic_booking.equipment.models import BookingSampleTrace, EquipmentManager, EquipmentOperator
from iic_booking.external_services import logistics
from iic_booking.external_services.models import ExternalNotification, SampleReceiptScan, SampleShipmentLabel
from iic_booking.users.models import Department
from iic_booking.users.models.user_type import UserType

from iic_booking.external_services.models import ExternalServicesSettings

from .conftest import client_for, make_user
from .test_logistics import IIC_ADDRESS

pytestmark = pytest.mark.django_db
SCAN = "/api/v1/external-booking/staff/sample-receipt/scan/"


@pytest.fixture
def on():
    s = ExternalServicesSettings.load()
    s.sample_labels_enabled = True
    s.iic_postal_address = IIC_ADDRESS
    s.save()
    return s


@pytest.fixture
def booking(factory):
    """A paid external booking awaiting its sample."""
    return factory.booking(make_user(), factory.equipment(), factory.at(timezone.localdate() + timedelta(days=6)),
                           payment_settled_at=timezone.now())


@pytest.fixture(autouse=True)
def no_results_folder(monkeypatch):
    """Sample acceptance resolves the results folder in storage; not under test here."""
    from iic_booking.equipment import api_views

    monkeypatch.setattr(api_views, "_resolve_in_analysis_results_folder_spec", lambda booking: None)


def _label(booking):
    token = "scan-" + "y" * 22
    SampleShipmentLabel.objects.create(booking=booking, token_sha256=logistics._hash(token), reference="SCAN2345",
                                       sample_count=2)
    return token


def _people(booking):
    eq = booking.equipment
    office = make_user(UserType.FINANCE, department=eq.internal_department, is_staff=True)
    oic = make_user(UserType.MANAGER, department=eq.internal_department, is_staff=True)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    lab = make_user(UserType.OPERATOR, department=eq.internal_department, is_staff=True)
    EquipmentOperator.objects.create(equipment=eq, operator=lab)
    return office, oic, lab


def _scan(user, token):
    return client_for(user).post(SCAN, {"code": token}, format="json")


def _statuses(booking):
    return list(BookingSampleTrace.objects.filter(booking=booking).order_by("id").values_list("status", flat=True))


def test_office_then_lab_then_details(on, booking):
    token = _label(booking)
    office, oic, _lab = _people(booking)

    r = _scan(office, token)
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["outcome"] == "received_at_office" and body["role"] == "office"
    assert _statuses(booking) == ["FORWARDED_TO_LAB"]
    assert BookingSampleTrace.objects.get(booking=booking).created_by == office
    assert body["lab"] == booking.equipment.internal_department.name and body["received_at_office"] is True
    assert ExternalNotification.objects.filter(recipient=booking.user, title__icontains="Sample received").exists()

    r = _scan(oic, token)
    assert r.status_code == 200 and r.json()["outcome"] == "received_at_lab", r.content
    assert _statuses(booking) == ["FORWARDED_TO_LAB", "SAMPLE_ACCEPTED"]

    for person in (oic, office):
        r = _scan(person, token)
        assert r.status_code == 200 and r.json()["outcome"] == "details"
    assert _statuses(booking) == ["FORWARDED_TO_LAB", "SAMPLE_ACCEPTED"]  # later scans record nothing
    body = r.json()
    assert [e["status"] for e in body["timeline"]] == ["FORWARDED_TO_LAB", "SAMPLE_ACCEPTED"]
    assert body["received_at_lab"] is True and "inputs" in body and body["slot_ranges"]
    assert list(SampleReceiptScan.objects.order_by("id").values_list("action", flat=True)) == [
        "office", "lab", "looked_up", "looked_up"]


def test_lab_in_charge_receives_at_the_lab(on, booking):
    token = _label(booking)
    office, _oic, lab = _people(booking)
    assert _scan(office, token).json()["outcome"] == "received_at_office"
    assert _scan(lab, token).json()["outcome"] == "received_at_lab"


def test_lab_scan_before_the_office_records_nothing(on, booking):
    token = _label(booking)
    _office, oic, _lab = _people(booking)
    r = _scan(oic, token)
    assert r.status_code == 200 and r.json()["outcome"] == "awaiting_office"
    assert _statuses(booking) == []


def test_pending_payment_is_not_forwarded(on, booking):
    token = _label(booking)
    booking.status, booking.amount_due, booking.payment_settled_at = "PENDING_PAYMENT", Decimal("590.00"), None
    booking.save(update_fields=["status", "amount_due", "payment_settled_at"])
    office, _oic, _lab = _people(booking)
    r = _scan(office, token)
    assert r.status_code == 200 and r.json()["outcome"] == "payment_pending"
    assert "590.00" in r.json()["message"]
    assert _statuses(booking) == []


def test_accounts_in_charge_of_another_department_cannot_receive(on, booking):
    token = _label(booking)
    other_dept = Department.objects.create(name="XS Other Dept", code="XSOTH", equipment_booking_enabled=True)
    stranger = make_user(UserType.FINANCE, department=other_dept, is_staff=True)
    assert _scan(stranger, token).status_code == 404
    assert client_for(stranger).post("/api/v1/external-booking/staff/sample-receipt/lookup/", {"code": token},
                                     format="json").status_code == 404  # manual look-up has the same rule
    assert _statuses(booking) == []


def test_unrelated_people_get_not_found(on, booking, factory):
    token = _label(booking)
    other_operator = make_user(UserType.OPERATOR, is_staff=True)
    EquipmentOperator.objects.create(equipment=factory.equipment(), operator=other_operator)
    for person in (booking.user, other_operator, make_user()):
        assert _scan(person, token).status_code == 404
    assert _scan(make_user(UserType.FINANCE, department=booking.equipment.internal_department, is_staff=True),
                 token + "x").status_code == 404  # tampered code
    assert _statuses(booking) == []


def test_printed_code_works_when_the_qr_is_damaged(on, booking):
    _label(booking)
    office, _oic, _lab = _people(booking)
    r = _scan(office, "scan2345")  # the 8-letter reference printed under the QR, any case
    assert r.status_code == 200 and r.json()["outcome"] == "received_at_office", r.content
    assert _scan(make_user(UserType.FINANCE, is_staff=True), "SCAN2345").status_code == 404  # same role rules


def test_main_administrator_sees_details_only(on, booking):
    token = _label(booking)
    admin = make_user(UserType.ADMIN, is_staff=True)
    r = _scan(admin, token)
    assert r.status_code == 200 and r.json()["outcome"] == "details"
    assert _statuses(booking) == []


def test_repeated_office_scans_record_once(on, booking):
    token = _label(booking)
    office, _oic, _lab = _people(booking)
    outcomes = [_scan(office, token).json()["outcome"] for _ in range(3)]
    assert outcomes == ["received_at_office", "details", "details"]
    assert _statuses(booking) == ["FORWARDED_TO_LAB"]


def test_label_shows_the_lab_and_the_addressed_people(on, booking):
    _office, oic, lab = _people(booking)
    from iic_booking.users.display import get_user_display_name

    r = client_for(booking.user).post(
        f"/api/v1/external-booking/bookings/{booking.pk}/sample-label/",
        {"sample_count": 2, "sender": {"recipient_name": "Dr Asha Verma", "line1": "12 Station Road",
                                       "city": "Roorkee", "state": "Uttarakhand", "postal_code": "247667"}},
        format="json",
    )
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    import io

    from pypdf import PdfReader

    text = PdfReader(io.BytesIO(r.content)).pages[0].extract_text()
    assert booking.equipment.internal_department.name in text
    assert f"Attn: {get_user_display_name(oic)} (Officer In Charge)" in text
    assert f"Lab in-charge: {get_user_display_name(lab)}" in text
    assert booking.virtual_booking_id in text
    assert IIC_ADDRESS.splitlines()[1] in text
    assert timezone.now() - SampleShipmentLabel.objects.get(booking=booking).created_at < timedelta(minutes=1)

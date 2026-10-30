"""First scan of a sample label: whoever holds the parcel (normally the sender, from a phone camera, often not signed in)
gives the courier and tracking number once. Afterwards, or when the portal already holds courier details, the label
only shows them; changes go through the portal. Receiving staff still get the office / lab steps."""

import pytest
from django.core.cache import cache

from iic_booking.courier_tracking import eligibility as courier_eligibility
from iic_booking.courier_tracking.models import Courier, SampleArrival
from iic_booking.equipment.models import BookingSampleTrace
from iic_booking.external_services.models import ExternalNotification, SampleReceiptScan

from .conftest import client_for
from .test_sample_scan import _label, _people, _scan, _statuses, booking, no_results_folder, on  # noqa: F401

pytestmark = pytest.mark.django_db
STATUS = "/api/v1/external-booking/sample-label/courier/status/"
SUBMIT = "/api/v1/external-booking/sample-label/courier/"


@pytest.fixture(autouse=True)
def fresh_throttle():
    cache.clear()


@pytest.fixture
def tracked(monkeypatch):
    """Courier tracking switched on for the booking's department, with one courier in the list."""
    monkeypatch.setattr(courier_eligibility, "department_tracking_enabled", lambda booking: True)
    monkeypatch.setattr("iic_booking.courier_tracking.services._dispatch_lookup", lambda *a, **k: None)
    Courier.objects.create(slug="bluedart", name="Blue Dart")


def _post(url, body, user=None):
    return client_for(user).post(url, body, format="json")


def test_anonymous_first_scan_records_the_courier_once(on, booking, tracked):
    token = _label(booking)
    r = _post(STATUS, {"code": token})
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["can_submit"] is True and body["courier_list"] is True
    assert body["couriers"] == [{"slug": "bluedart", "name": "Blue Dart"}]
    assert body["booking_reference"] and "user_name" not in body and "email" not in str(body).lower()

    r = _post(SUBMIT, {"code": token, "courier_slug": "bluedart", "tracking_number": "ab 12345678"})
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["can_submit"] is False and body["recorded"] == {"courier_name": "Blue Dart", "tracking_tail": "…5678"}
    arrival = SampleArrival.objects.get(booking=booking, is_active=True)
    assert arrival.user == booking.user and arrival.tracking_number == "AB12345678" and arrival.email_updates
    assert _statuses(booking) == ["SAMPLE_SENT"]
    assert SampleReceiptScan.objects.get(action="courier").staff is None
    note = ExternalNotification.objects.filter(recipient=booking.user, title__icontains="Courier details recorded")
    assert note.filter(channel="email").exists()

    r = _post(SUBMIT, {"code": token, "courier_slug": "bluedart", "tracking_number": "ZZ99999999"})
    assert r.status_code == 409 and r.json()["code"] == "courier_locked"
    assert SampleArrival.objects.get(booking=booking, is_active=True).tracking_number == "AB12345678"


def test_courier_typed_when_department_tracking_is_off(on, booking):
    token = _label(booking)
    assert _post(STATUS, {"code": token}).json()["courier_list"] is False
    r = _post(SUBMIT, {"code": token, "courier_name": "DTDC", "tracking_number": "D1234567"})
    assert r.status_code == 200, r.content
    trace = BookingSampleTrace.objects.get(booking=booking)
    assert trace.status == "SAMPLE_SENT" and trace.tracking_id == "DTDC: D1234567"
    assert not SampleArrival.objects.filter(booking=booking).exists()


def test_invalid_input_is_refused_and_nothing_is_locked(on, booking):
    token = _label(booking)
    r = _post(SUBMIT, {"code": token, "courier_name": "", "tracking_number": "D1234567"})
    assert r.status_code == 400 and r.json()["field"] == "courier_name"
    r = _post(SUBMIT, {"code": token, "courier_name": "DTDC", "tracking_number": "x"})
    assert r.status_code == 400 and r.json()["field"] == "tracking_number"
    assert _post(STATUS, {"code": token}).json()["can_submit"] is True


def test_portal_courier_details_lock_the_label(on, booking):
    token = _label(booking)
    BookingSampleTrace.objects.create(booking=booking, status="SAMPLE_SENT", tracking_id="India Post: EE123456789IN",
                                      created_by=booking.user)
    body = _post(STATUS, {"code": token}).json()
    assert body["can_submit"] is False and "portal" in body["reason"]
    assert body["recorded"] == {"courier_name": "India Post", "tracking_tail": "…89IN"}
    r = _post(SUBMIT, {"code": token, "courier_name": "DTDC", "tracking_number": "D1234567"})
    assert r.status_code == 409


def test_received_sample_cannot_take_courier_details(on, booking):
    token = _label(booking)
    office, _oic, _lab = _people(booking)
    assert _scan(office, token).json()["outcome"] == "received_at_office"
    body = _post(STATUS, {"code": token}).json()
    assert body["can_submit"] is False and "already received" in body["reason"]


def test_staff_scan_after_the_courier_step_still_receives(on, booking):
    token = _label(booking)
    assert _post(SUBMIT, {"code": token, "courier_name": "DTDC", "tracking_number": "D1234567"}).status_code == 200
    office, oic, _lab = _people(booking)
    assert _scan(office, token).json()["outcome"] == "received_at_office"
    assert _scan(oic, token).json()["outcome"] == "received_at_lab"
    assert _statuses(booking) == ["SAMPLE_SENT", "FORWARDED_TO_LAB", "SAMPLE_ACCEPTED"]


def test_unknown_or_replaced_label_is_not_found(on, booking):
    token = _label(booking)
    assert _post(STATUS, {"code": "nope"}).status_code == 404
    booking.external_sample_labels.update(revoked_at="2026-10-30T10:00:00+05:30")
    assert _post(STATUS, {"code": token}).status_code == 404
    assert _post(SUBMIT, {"code": token, "courier_name": "DTDC", "tracking_number": "D1234567"}).status_code == 404


def test_signed_in_owner_may_use_the_label_too(on, booking):
    token = _label(booking)
    r = _post(SUBMIT, {"code": token, "courier_name": "DTDC", "tracking_number": "D1234567"}, user=booking.user)
    assert r.status_code == 200, r.content

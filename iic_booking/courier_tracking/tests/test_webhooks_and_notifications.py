from unittest.mock import patch

import pytest

from iic_booking.courier_tracking import notifications, services, webhooks
from iic_booking.courier_tracking.constants import LookupTrigger, ShipmentStatus
from iic_booking.courier_tracking.models import (
    CourierTrackingAlert,
    DeliveryStatus,
    SampleArrival,
    ShipmentNotification,
    TrackingWebhookEvent,
    WebhookEventStatus,
)
from iic_booking.equipment.models import BookingSampleTrace, EquipmentManager, EquipmentOperator, SampleTraceStatus
from iic_booking.users.models.user_type import UserType

from .conftest import FakeResponse, client_for, make_user, signed, track_payload

pytestmark = pytest.mark.django_db
URL = "/api/v1/courier-tracking/webhooks/trackcourier/"


def _post(raw: bytes, signature: str, mode: str = "live"):
    return client_for().post(URL, raw, content_type="application/json", HTTP_X_TRACKCOURIER_SIGNATURE=signature,
                             HTTP_X_WEBHOOK_MODE=mode, HTTP_X_WEBHOOK_DELIVERY="wh_1-1")


def _event(status="delivered", event="tracking.delivered", checkpoints="default", number="D3010377227", courier="dtdc"):
    if checkpoints == "default":
        checkpoints = [
            {"activity": "Delivered", "checkpoint_state": "delivered", "courier_name": "DTDC", "date": "18-Sep-2026",
             "time": "17:08", "location": "Delhi"},
            {"activity": "On the way", "checkpoint_state": "intransit", "courier_name": "DTDC", "date": "17-Sep-2026",
             "time": "22:15", "location": "Jaipur to Delhi"},
        ]
    return {"event": event, "enqueued_at": "2026-09-18T09:14:22Z",
            "data": {"tracking_number": number, "courier": courier, "status": status,
                     "most_recent_status": status.title(), "checkpoints": checkpoints}}


@pytest.fixture
def staff(iic_equipment):
    oic = make_user(UserType.MANAGER)
    operator = make_user(UserType.OPERATOR)
    finance = make_user(UserType.FINANCE, department=iic_equipment.internal_department)
    EquipmentManager.objects.create(equipment=iic_equipment, manager=oic)
    EquipmentOperator.objects.create(equipment=iic_equipment, operator=operator)
    return {"oic": oic, "operator": operator, "finance": finance}


@pytest.fixture
def shipment(catalog, external_booking, external_user, no_queue):
    return services.register_arrival(external_user, external_booking, services.ArrivalInput(
        arrival_mode="COURIER", courier_slug="dtdc", tracking_number="D3010377227", email_updates=True))


def _process_all(order="received_at"):
    for pk in TrackingWebhookEvent.objects.order_by(order).values_list("pk", flat=True):
        webhooks.process(pk)


# --- signature and storage --------------------------------------------------------------------------------------------

def test_without_secret_every_delivery_is_refused(shipment):
    raw, sig = signed(_event())
    assert _post(raw, sig).status_code == 503
    assert not TrackingWebhookEvent.objects.exists()


@pytest.mark.parametrize("signature", ["", "sha256=" + "0" * 64, "sha256=ünicode", "md5=abc"])
def test_invalid_signatures_are_rejected(webhook_secret, shipment, signature):
    raw, _ = signed(_event())
    assert _post(raw, signature).status_code == 401
    assert not TrackingWebhookEvent.objects.exists()


def test_signature_covers_exact_raw_body(webhook_secret, shipment):
    raw, sig = signed(_event())
    reformatted = raw.replace(b'", "', b'","')
    assert _post(reformatted, sig).status_code == 401


def test_previous_secret_accepted_during_rotation(settings, webhook_secret, shipment):
    settings.TRACKCOURIER_WEBHOOK_PREVIOUS_SECRET = "old-secret-still-valid"
    raw, sig = signed(_event(), secret="old-secret-still-valid")
    assert _post(raw, sig).status_code == 200


def test_malformed_json_with_valid_signature(webhook_secret, shipment):
    raw, sig = signed(b"{not json")
    assert _post(raw, sig).status_code == 400


def test_valid_delivery_is_stored_before_acknowledging(webhook_secret, shipment):
    raw, sig = signed(_event())
    response = _post(raw, sig)
    assert response.status_code == 200 and response.data == {"received": True}
    event = TrackingWebhookEvent.objects.get()
    assert event.status == WebhookEventStatus.RECEIVED and event.mode == "live"
    assert webhooks.process(event.pk) == WebhookEventStatus.PROCESSED


# --- processing -------------------------------------------------------------------------------------------------------

def test_duplicate_delivery_changes_nothing_twice(webhook_secret, shipment, staff):
    raw, sig = signed(_event())
    _post(raw, sig)
    _post(raw, sig)  # retry of the same event (X-Webhook-Delivery would differ; not used)
    assert TrackingWebhookEvent.objects.get().deliveries == 2
    _process_all()
    _process_all()
    shipment.refresh_from_db()
    assert shipment.status == ShipmentStatus.DELIVERY_REPORTED and len(shipment.checkpoints) == 2
    staff_rows = ShipmentNotification.objects.filter(kind=notifications.KIND_STAFF_DELIVERED, channel="EMAIL")
    assert staff_rows.count() == 3  # OIC, Lab Operator, Accounts In Charge - once each


def test_checkpoints_null_preserves_history(webhook_secret, shipment):
    raw, sig = signed(_event(status="in_transit", event="tracking.in_transit"))
    _post(raw, sig)
    raw, sig = signed(_event(status="out_for_delivery", event="tracking.out_for_delivery", checkpoints=None)
                      | {"enqueued_at": "2026-09-18T10:00:00Z"})
    _post(raw, sig)
    _process_all()
    shipment.refresh_from_db()
    assert shipment.status == ShipmentStatus.OUT_FOR_DELIVERY and len(shipment.checkpoints) == 2


def test_synthetic_test_deliveries_are_ignored(webhook_secret, shipment):
    raw, sig = signed(_event(courier="synthetic-test-dtdc"))
    _post(raw, sig, mode="test")
    _process_all()
    assert TrackingWebhookEvent.objects.get().status == WebhookEventStatus.IGNORED
    shipment.refresh_from_db()
    assert shipment.status == ShipmentStatus.REGISTERED


def test_unknown_number_is_recorded_unmatched(webhook_secret, shipment):
    raw, sig = signed(_event(number="ZZ999999999"))
    _post(raw, sig)
    _process_all()
    assert TrackingWebhookEvent.objects.get().status == WebhookEventStatus.UNMATCHED


def test_late_non_terminal_event_cannot_reopen_a_delivery(webhook_secret, shipment):
    for status, event in (("delivered", "tracking.delivered"), ("in_transit", "tracking.in_transit")):
        raw, sig = signed(_event(status=status, event=event))
        _post(raw, sig)
    _process_all()
    shipment.refresh_from_db()
    assert shipment.status == ShipmentStatus.DELIVERY_REPORTED


def test_reported_delivery_never_marks_sample_received(webhook_secret, shipment, staff):
    raw, sig = signed(_event())
    _post(raw, sig)
    _process_all()
    statuses = set(BookingSampleTrace.objects.filter(booking=shipment.booking).values_list("status", flat=True))
    assert statuses == {SampleTraceStatus.SAMPLE_SENT}


# --- notifications ----------------------------------------------------------------------------------------------------

def test_user_opt_out_suppresses_user_email_but_not_staff_email(provider, catalog, external_user, iic_equipment, staff, no_queue):
    from .conftest import make_booking

    arrival = services.register_arrival(external_user, make_booking(external_user, iic_equipment), services.ArrivalInput(
        arrival_mode="COURIER", courier_slug="dtdc", tracking_number="D3010377227", email_updates=False))
    provider.queue(FakeResponse(200, track_payload("delivered", "Delivered")))
    services.lookup(arrival.pk, LookupTrigger.INITIAL)
    user_rows = ShipmentNotification.objects.filter(recipient=external_user)
    assert not user_rows.filter(channel="EMAIL").exists() and user_rows.filter(channel="IN_APP").exists()
    staff_emails = set(ShipmentNotification.objects.filter(kind=notifications.KIND_STAFF_DELIVERED, channel="EMAIL")
                       .values_list("recipient_id", flat=True))
    assert staff_emails == {staff["oic"].pk, staff["operator"].pk, staff["finance"].pk}


def test_user_opt_in_gets_email_once_per_status(provider, shipment):
    provider.queue(FakeResponse(200, track_payload("intransit")))
    services.lookup(shipment.pk, LookupTrigger.INITIAL)
    SampleArrival.objects.filter(pk=shipment.pk).update(last_lookup_attempt_at=None)
    provider.queue(FakeResponse(200, track_payload("intransit")))
    services.lookup(shipment.pk, LookupTrigger.STAFF)
    emails = ShipmentNotification.objects.filter(recipient=shipment.booking.user, channel="EMAIL")
    assert sorted(emails.values_list("state_key", flat=True)) == ["IN_TRANSIT", "registered"]


def test_staff_holding_several_roles_get_one_message(catalog, iic_equipment, external_booking, external_user, no_queue):
    both = make_user(UserType.MANAGER)
    EquipmentManager.objects.create(equipment=iic_equipment, manager=both)
    finance = make_user(UserType.FINANCE, department=iic_equipment.internal_department)
    users, missing = notifications.staff_recipients(SampleArrival(equipment=iic_equipment,
                                                                  department=iic_equipment.internal_department))
    assert [u.pk for u in users].count(both.pk) == 1 and finance in users
    assert missing == ["Lab in-charge (Lab Operator)"]


def test_missing_required_recipient_raises_admin_warning(webhook_secret, shipment):
    raw, sig = signed(_event())
    _post(raw, sig)
    _process_all()
    alert = CourierTrackingAlert.objects.get(kind="RECIPIENT_MISSING")
    assert "Officer In Charge" in alert.message and "Lab in-charge" in alert.message


def test_failed_send_is_retried_then_gives_up(webhook_secret, shipment, staff):
    raw, sig = signed(_event())
    _post(raw, sig)
    _process_all()
    row = ShipmentNotification.objects.filter(kind=notifications.KIND_STAFF_DELIVERED, channel="EMAIL").first()
    with patch("iic_booking.communication.service.CommunicationService.send_email", side_effect=RuntimeError("smtp down")):
        assert notifications.send_one(row.pk) == "failed"
    row.refresh_from_db()
    assert row.status == DeliveryStatus.FAILED and row.attempts == 1
    assert notifications.send_one(row.pk) == "sent"
    assert notifications.send_one(row.pk) == "skipped"  # never sent twice
    other = ShipmentNotification.objects.filter(kind=notifications.KIND_STAFF_DELIVERED, channel="EMAIL").exclude(pk=row.pk).first()
    with patch("iic_booking.communication.service.CommunicationService.send_email", side_effect=RuntimeError("smtp down")):
        for _ in range(notifications.MAX_ATTEMPTS):
            notifications.send_one(other.pk)
    other.refresh_from_db()
    assert other.status == DeliveryStatus.GAVE_UP
    assert CourierTrackingAlert.objects.filter(kind="NOTIFICATION_FAILED").exists()


def test_manual_arrival_gets_no_courier_messages(catalog, internal_user, iic_equipment, no_queue):
    from .conftest import make_booking

    services.register_arrival(internal_user, make_booking(internal_user, iic_equipment),
                              services.ArrivalInput(arrival_mode="MANUAL"))
    assert not ShipmentNotification.objects.exists()



def test_older_event_processed_late_cannot_move_status_back(webhook_secret, shipment):
    newer = _event(status="out_for_delivery", event="tracking.out_for_delivery") | {"enqueued_at": "2026-09-18T12:00:00Z"}
    older = _event(status="in_transit", event="tracking.in_transit") | {"enqueued_at": "2026-09-18T08:00:00Z"}
    for body in (newer, older):
        raw, sig = signed(body)
        _post(raw, sig)
    _process_all()  # newer first, then the older one arrives
    shipment.refresh_from_db()
    assert shipment.status == ShipmentStatus.OUT_FOR_DELIVERY
    assert TrackingWebhookEvent.objects.filter(outcome__icontains="older").count() == 1

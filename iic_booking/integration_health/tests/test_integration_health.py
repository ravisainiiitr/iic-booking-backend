"""Integration health: each check finds the failure it is for, the safe fixes repeat only what cannot duplicate, the
Main Administrators are emailed once per new problem, and the nightly report follows each booking step by step."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.integration_health import checks, heal, report
from iic_booking.istem.choices import AttemptOutcome, JobState, Operation, SyncState
from iic_booking.istem.models import IstemApiAttempt, IstemBookingLink, IstemOutboxJob
from iic_booking.istem.tests.support import apply_live, make_booking, ready_booking
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory
from iic_booking.whatsapp.models import WhatsAppMessage

pytestmark = pytest.mark.django_db

REFUSED_131037 = ("Meta refused the message (131037): (#131037) WhatsApp provided number needs display name approval "
                  "before message can be sent.")


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def istem_on(settings):
    apply_live(settings, ISTEM_AUTOMATIC_FBR=True)
    return settings


@pytest.fixture
def whatsapp_on(settings):
    settings.WHATSAPP_ENABLED = True
    settings.WHATSAPP_PHONE_NUMBER_ID = "1234567890"
    settings.WHATSAPP_ACCESS_TOKEN = "unit-test-token-not-real"
    settings.WHATSAPP_APP_SECRET = "s"
    settings.WHATSAPP_VERIFY_TOKEN = "v"
    settings.WHATSAPP_PILOT_NUMBERS = []
    settings.WHATSAPP_ALLOW_ALL = True
    return settings


def _by_key(found):
    return {c.key: c for c in found}


def _age(model, pk, **fields):
    model.objects.filter(pk=pk).update(**fields)


# --- I-STEM ----------------------------------------------------------------------------------------------------------

def test_a_paid_booking_never_sent_to_istem_is_reported(istem_on):
    booking = ready_booking()
    _age(type(booking), booking.pk, created_at=timezone.now() - timedelta(hours=1))
    found = _by_key(checks.istem_checks())
    assert found["istem_registered"].status == checks.FAIL
    assert any("paid but not sent to I-STEM" in i for i in found["istem_registered"].items)


def test_a_completed_booking_without_fbr_execute_is_reported(istem_on):
    booking = ready_booking()
    IstemBookingLink.objects.create(booking=booking, sync_state=SyncState.CONFIRMED, request_id="95589",
                                    fbr_no="1976095589", confirmation_id="72003")
    type(booking).objects.filter(pk=booking.pk).update(status="COMPLETED", completed_at=timezone.now() - timedelta(hours=3))
    found = _by_key(checks.istem_checks())
    assert found["istem_executed"].status == checks.FAIL
    assert found["istem_registered"].status == checks.OK


def test_a_held_record_and_an_overdue_queue_are_reported(istem_on):
    booking = ready_booking()
    link = IstemBookingLink.objects.create(booking=booking, sync_state=SyncState.CREATE_FAILED, needs_review=True,
                                           review_reason="I-STEM refused the booking: This slot is already booked.")
    IstemOutboxJob.objects.create(operation=Operation.CREATE_BOOKING, dedupe_key="CREATE_BOOKING:x", trigger="t",
                                  booking_link=link, state=JobState.RETRY_WAIT,
                                  next_attempt_at=timezone.now() - timedelta(hours=1))
    found = _by_key(checks.istem_checks())
    assert found["istem_held"].status == checks.FAIL and "already booked" in found["istem_held"].items[0]
    assert found["istem_queue"].status == checks.FAIL


def test_a_create_refused_for_a_taken_hour_is_retried_once_and_only_then(istem_on):
    booking = ready_booking()
    link = IstemBookingLink.objects.create(booking=booking, sync_state=SyncState.CREATE_FAILED, needs_review=True,
                                           review_reason="I-STEM refused the booking: This slot is already booked.")
    IstemApiAttempt.objects.create(booking_link=link, operation=Operation.CREATE_BOOKING,
                                   outcome=AttemptOutcome.DEFINITE_FAILURE)
    other = make_booking()
    other_link = IstemBookingLink.objects.create(booking=other, sync_state=SyncState.CREATE_FAILED, needs_review=True,
                                                 review_reason="I-STEM refused the booking: Invalid equipment.")
    IstemApiAttempt.objects.create(booking_link=other_link, operation=Operation.CREATE_BOOKING,
                                   outcome=AttemptOutcome.DEFINITE_FAILURE)
    assert heal.retry_slot_refused_creates() == 1
    link.refresh_from_db()
    other_link.refresh_from_db()
    assert (link.sync_state, link.needs_review) == (SyncState.CREATE_READY, False)
    assert IstemOutboxJob.objects.filter(booking_link=link, operation=Operation.CREATE_BOOKING, state=JobState.PENDING).exists()
    assert other_link.sync_state == SyncState.CREATE_FAILED  # any other refusal stays with the team
    assert heal.retry_slot_refused_creates() == 0


def test_istem_off_is_reported_as_off(settings):
    apply_live(settings, ISTEM_AUTOMATIC_FBR=False)
    assert [c.status for c in checks.istem_checks()] == [checks.OFF]


# --- WhatsApp --------------------------------------------------------------------------------------------------------

def _message(user, status, error="", key="booking:1:booking_confirmed:payment", age=timedelta(minutes=5)):
    msg = WhatsAppMessage.objects.create(user=user, to_e164="+919876543210", event="booking_confirmed",
                                         template="iic_booking_confirmed", params=[], event_key=key, status=status, error=error)
    WhatsAppMessage.objects.filter(pk=msg.pk).update(created_at=timezone.now() - age)
    return msg


def test_refused_whatsapp_messages_are_reported_with_what_to_do(whatsapp_on):
    user = UserFactory()
    _message(user, "read", key="k1", age=timedelta(hours=3))
    _message(user, "failed", REFUSED_131037, key="k2")
    _message(user, "failed", REFUSED_131037, key="k3")
    check = _by_key(checks.whatsapp_checks())["whatsapp_delivery"]
    assert check.status == checks.FAIL  # nothing has gone out since the last refusal
    assert check.items == [f"Meta 131037: 2 message(s) - {checks.META_HINTS['131037']}"]
    assert "display name" in check.action


def test_refused_messages_are_resent_once_meta_accepts_again(whatsapp_on, monkeypatch, django_capture_on_commit_callbacks):
    sent = []
    monkeypatch.setattr("iic_booking.whatsapp.service._enqueue", lambda pk: sent.append(pk))
    user = UserFactory()
    refused = _message(user, "failed", REFUSED_131037, key="k1")
    wrong_number = _message(user, "failed", "Meta refused the message (131026): not on WhatsApp", key="k2")
    too_old = _message(user, "failed", REFUSED_131037, key="k3", age=timedelta(days=3))

    assert heal.heal_whatsapp({"can_send": "LIMITED"})["resent"] == 0  # Meta still refuses: nothing is sent
    with django_capture_on_commit_callbacks(execute=True):
        assert heal.heal_whatsapp({"can_send": "AVAILABLE"})["resent"] == 1
    assert sent == [refused.pk]
    refused.refresh_from_db()
    assert refused.status == "queued" and refused.error.startswith("resent after:")
    for msg in (wrong_number, too_old):
        msg.refresh_from_db()
        assert msg.status == "failed"
    # Refused again: never resent a second time by this route.
    WhatsAppMessage.objects.filter(pk=refused.pk).update(status="failed", error=REFUSED_131037)
    assert heal.heal_whatsapp({"can_send": "AVAILABLE"})["resent"] == 0


# --- alerts, API, nightly --------------------------------------------------------------------------------------------

def test_admins_are_emailed_once_per_new_problem_and_when_it_clears(mailoutbox):
    admin = UserFactory(user_type=UserType.ADMIN, email="chief@iitr.ac.in")
    type(admin).objects.filter(pk=admin.pk).update(is_active=True, is_test_account=False)
    failing = [checks.Check("istem_queue", "istem", "I-STEM queue is moving", status=checks.FAIL, summary="3 overdue")]
    report._alert_on_change(failing)
    report._alert_on_change(failing)
    assert len(mailoutbox) == 1 and "1 problem" in mailoutbox[0].subject
    report._alert_on_change([checks.Check("istem_queue", "istem", "I-STEM queue is moving", status=checks.OK, summary="ok")])
    assert len(mailoutbox) == 2 and "resolved" in mailoutbox[1].subject


def test_only_the_main_administrator_sees_integration_health(istem_on):
    client = APIClient()
    client.force_authenticate(UserFactory(user_type=UserType.STUDENT))
    assert client.get("/api/admin/integration-health/").status_code == 403
    client.force_authenticate(UserFactory(user_type=UserType.ADMIN))
    res = client.get("/api/admin/integration-health/")
    assert res.status_code == 200
    assert res.data["overall"] in ("ok", "warn", "fail") and {c["key"] for c in res.data["checks"]} >= {"istem_queue"}


def test_the_nightly_report_follows_each_booking(istem_on, whatsapp_on):
    booking = ready_booking()
    IstemBookingLink.objects.create(booking=booking, sync_state=SyncState.CONFIRMED, request_id="95590",
                                    fbr_no="1976095590", confirmation_id="72004")
    type(booking).objects.filter(pk=booking.pk).update(status="COMPLETED", completed_at=timezone.now() - timedelta(hours=3))
    _message(booking.user, "failed", REFUSED_131037, key=f"booking:{booking.pk}:booking_completed:1")
    journey = report.booking_journeys()
    assert journey["totals"]["istem_registered"] == 1 and journey["totals"]["istem_executed"] == 0
    problem = next(p for p in journey["problems"] if p.startswith(booking.virtual_booking_id or f"#{booking.pk}"))
    assert "FBR not executed" in problem and "1 WhatsApp message(s) refused" in problem
    assert "FBR executed: 0 of 1 completed" in report.journey_text(journey)

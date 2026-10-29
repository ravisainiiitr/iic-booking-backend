"""External special requests: submission, isolation from internal urgent requests, staff decisions, allocation."""

from datetime import timedelta

import pytest
from django.utils import timezone

from iic_booking.equipment.models import (
    Booking,
    DailySlot,
    EquipmentManager,
    UrgentBookingRequest,
    UrgentBookingRequestStatus,
    UrgentBookingRequestType,
)
from iic_booking.external_services import special_requests as svc
from iic_booking.external_services import special_staff
from iic_booking.external_services.models import (
    AllocationState,
    ExternalNotification,
    ExternalServicesSettings,
    PolicyText,
    SpecialAllocation,
    SpecialRequestEvent,
)
from iic_booking.users.models.user_type import UserType

from .conftest import client_for, main_admin, make_user

pytestmark = pytest.mark.django_db
API = "/api/v1/external-booking/special-requests/"
STAFF = "/api/v1/external-booking/staff/special-requests/"


@pytest.fixture(autouse=True)
def open_portal(monkeypatch):
    from iic_booking.users.legacy_ledger import booking_lock

    monkeypatch.setattr(booking_lock, "booking_is_locked", lambda user: (False, ""))
    monkeypatch.setattr(booking_lock, "department_equipment_booking_blocked", lambda equipment, user: (False, ""))


@pytest.fixture
def enabled():
    s = ExternalServicesSettings.load()
    s.special_requests_enabled = True
    s.save()
    return s


@pytest.fixture
def eq(factory):
    return factory.equipment()


@pytest.fixture
def oic(eq):
    user = make_user(UserType.MANAGER, is_staff=True, department=eq.internal_department)
    EquipmentManager.objects.create(equipment=eq, manager=user)

    return user


def _payload(eq, days=20, **extra):
    start = timezone.localtime() + timedelta(days=days)
    start = start.replace(hour=10, minute=0, second=0, microsecond=0)
    body = {
        "equipment_id": eq.pk,
        "input_values": {"A": 2},
        "number_of_samples": 2,
        "preferred_start": start.isoformat(),
        "preferred_end": (start + timedelta(hours=2)).isoformat(),
        "alternatives": [{"start": (start + timedelta(days=1)).isoformat(),
                          "end": (start + timedelta(days=1, hours=2)).isoformat()}],
        "sample_description": "Two polymer films, 1 cm x 1 cm",
        "remarks": "Reviewer asked for XRD",
    }
    body.update(extra)
    return body


def _submit(user, eq, **extra):
    return client_for(user).post(API, _payload(eq, **extra), format="json")


# --- availability and eligibility -----------------------------------------------------------------------------------

def test_disabled_by_default(eq):
    r = _submit(make_user(), eq)
    assert r.status_code == 403 and r.json()["code"] == "special_requests_disabled"


def test_internal_users_cannot_submit(enabled, eq):
    r = _submit(make_user(UserType.STUDENT), eq)
    assert r.status_code == 403 and r.json()["code"] == "not_external"


def test_submission_reserves_nothing_and_is_invisible_to_internal_urgent_code(enabled, eq, oic):
    user = make_user()
    r = _submit(user, eq)
    assert r.status_code == 201, r.content
    body = r.json()
    assert body["status"] == "PENDING" and body["estimated_total"] is not None
    urg = UrgentBookingRequest.all_types.get(pk=body["id"])
    assert urg.request_type == UrgentBookingRequestType.EXTERNAL_SPECIAL
    # The default manager (used by every existing urgent-request screen, cap, report and expiry) never sees it.
    assert not UrgentBookingRequest.objects.filter(pk=urg.pk).exists()
    assert not user.urgent_booking_requests.exists()
    assert not DailySlot.objects.filter(booking__user=user).exists()
    assert not Booking.objects.filter(user=user).exists()
    assert SpecialRequestEvent.objects.filter(request=urg, action="submitted").exists()
    keys = set(ExternalNotification.objects.values_list("key", "recipient_id"))
    assert (f"special:{urg.pk}:submitted", user.pk) in keys
    assert (f"special:{urg.pk}:submitted:staff", oic.pk) in keys


def test_internal_urgent_endpoints_cannot_act_on_external_requests(enabled, eq, oic):
    urg_id = _submit(make_user(), eq).json()["id"]
    admin = main_admin()
    listing = client_for(admin).get("/api/urgent-booking-requests/").json()
    ids = [row.get("id") for row in listing.get("results", listing.get("urgent_requests", []))] if isinstance(
        listing, dict) else [row.get("id") for row in listing]
    assert urg_id not in ids
    r = client_for(admin).patch(f"/api/urgent-booking-requests/{urg_id}/", {"status": "APPROVED"}, format="json")
    assert r.status_code == 404
    assert UrgentBookingRequest.all_types.get(pk=urg_id).status == "PENDING"


def test_client_cannot_set_protected_fields(enabled, eq):
    r = _submit(make_user(), eq, status="APPROVED", estimated_charge="1.00", decided_by=1)
    assert r.status_code == 201
    urg = UrgentBookingRequest.all_types.get(pk=r.json()["id"])
    assert urg.status == "PENDING" and urg.decided_by_id is None and urg.estimated_charge > 1


@pytest.mark.parametrize("field, value, expected_field", [
    ("preferred_start", "2001-01-01T10:00:00+05:30", "preferred_start"),
    ("sample_description", "", "sample_description"),
    ("number_of_samples", 0, "number_of_samples"),
    ("alternatives", [{"start": "x", "end": "y"}] * 4, "alternatives"),
])
def test_validation(enabled, eq, field, value, expected_field):
    r = _submit(make_user(), eq, **{field: value})
    assert r.status_code == 400, r.content
    assert r.json().get("field") == expected_field


def test_end_before_start_and_over_24_hours(enabled, eq):
    start = timezone.localtime() + timedelta(days=20)
    r = _submit(make_user(), eq, preferred_start=start.isoformat(), preferred_end=(start - timedelta(hours=1)).isoformat())
    assert r.status_code == 400
    r = _submit(make_user(), eq, preferred_start=start.isoformat(), preferred_end=(start + timedelta(hours=25)).isoformat())
    assert r.status_code == 400


def test_open_request_limit(enabled, eq):
    enabled.max_open_special_requests = 2
    enabled.save()
    user = make_user()
    assert _submit(user, eq).status_code == 201
    assert _submit(user, eq).status_code == 201
    r = _submit(user, eq)
    assert r.status_code == 429 and r.json()["code"] == "too_many_open_requests"
    first = UrgentBookingRequest.all_types.filter(user=user).order_by("pk").first()
    assert client_for(user).post(f"{API}{first.pk}/withdraw/", {"reason": "not needed"}, format="json").status_code == 200
    assert _submit(user, eq).status_code == 201


# --- short notice ---------------------------------------------------------------------------------------------------

def test_short_notice_boundary():
    now = timezone.now()
    assert svc.is_short_notice(now + timedelta(days=12) - timedelta(seconds=1), at=now, days=12)
    assert not svc.is_short_notice(now + timedelta(days=12), at=now, days=12)  # exactly 12 days is not short notice
    assert not svc.is_short_notice(now + timedelta(days=13), at=now, days=12)


def test_short_notice_needs_acknowledgement_of_the_current_version(enabled, eq):
    user = make_user()
    r = _submit(user, eq, days=5)
    assert r.status_code == 400 and r.json()["code"] == "acknowledgement_required"
    current = PolicyText.objects.filter(key="short_notice").order_by("-version").first()
    assert r.json()["policy"]["version"] == current.version and r.json()["policy"]["approved"] is False
    r = _submit(user, eq, days=5, short_notice_ack_version=current.version + 1)
    assert r.status_code == 400
    r = _submit(user, eq, days=5, short_notice_ack_version=current.version)
    assert r.status_code == 201, r.content
    detail = UrgentBookingRequest.all_types.get(pk=r.json()["id"]).external_special
    assert detail.short_notice and detail.short_notice_policy == current and detail.short_notice_ack_at
    import hashlib

    assert detail.short_notice_text_sha256 == hashlib.sha256(current.text.encode()).hexdigest()


def test_not_short_notice_needs_no_acknowledgement(enabled, eq):
    r = _submit(make_user(), eq, days=20)
    assert r.status_code == 201
    assert r.json()["short_notice"] is False


# --- clarification and staff scope ------------------------------------------------------------------------------------

def test_clarification_cycle_and_scope(enabled, eq, oic, factory):
    user = make_user()
    urg_id = _submit(user, eq).json()["id"]
    other_oic = make_user(UserType.MANAGER, is_staff=True, department=factory.department)
    EquipmentManager.objects.create(equipment=factory.equipment(), manager=other_oic)
    assert client_for(other_oic).get(f"{STAFF}{urg_id}/").status_code == 404
    assert client_for(other_oic).post(f"{STAFF}{urg_id}/reject/", {"reason": "no"}, format="json").status_code == 404
    assert client_for(user).get(STAFF).status_code == 403  # an external user is not staff
    r = client_for(oic).post(f"{STAFF}{urg_id}/clarify/", {"message": "Which 2-theta range?"}, format="json")
    assert r.status_code == 200 and r.json()["status"] == "CLARIFICATION"
    r = client_for(oic).post(f"{STAFF}{urg_id}/allocate/", {"slot_ids": []}, format="json")
    assert r.status_code == 400  # not pending while waiting for the user
    r = client_for(user).post(f"{API}{urg_id}/respond/", {"message": "10 to 80 degrees"}, format="json")
    assert r.status_code == 200 and r.json()["status"] == "PENDING"
    history = client_for(oic).get(f"{STAFF}{urg_id}/").json()["history"]
    assert [h["action"] for h in history] == ["submitted", "clarification_requested", "clarified"]
    assert client_for(make_user()).get(f"{API}{urg_id}/").status_code == 404  # another user cannot read it


def test_reject(enabled, eq, oic):
    urg_id = _submit(make_user(), eq).json()["id"]
    assert client_for(oic).post(f"{STAFF}{urg_id}/reject/", {"reason": ""}, format="json").status_code == 400
    r = client_for(oic).post(f"{STAFF}{urg_id}/reject/", {"reason": "Instrument booked for calibration"},
                             format="json")
    assert r.status_code == 200 and r.json()["status"] == "REJECTED"
    assert client_for(oic).post(f"{STAFF}{urg_id}/reject/", {"reason": "again please"}, format="json").status_code == 400


# --- allocation -------------------------------------------------------------------------------------------------------

def _allocate(oic, urg_id, slots, **extra):
    return client_for(oic).post(f"{STAFF}{urg_id}/allocate/", {"slot_ids": [s.pk for s in slots], **extra},
                                format="json")


def test_allocation_creates_a_pending_payment_booking_with_the_deadline(enabled, eq, oic, factory):
    user = make_user()
    urg_id = _submit(user, eq).json()["id"]
    start = factory.at(timezone.localdate() + timedelta(days=4), 10)
    slot = factory.slot(eq, start)
    r = _allocate(oic, urg_id, [slot])
    assert r.status_code == 200, r.content
    a = SpecialAllocation.objects.get(request_id=urg_id)
    booking = a.booking
    assert booking.status == "PENDING_PAYMENT" and booking.user == user and booking.amount_due == booking.total_charge
    slot.refresh_from_db()
    assert slot.booking_id == booking.pk and slot.status == "BOOKED"
    assert a.previous_slot_statuses == {str(slot.pk): "AVAILABLE"}
    assert a.payment_deadline_at == a.approved_at + timedelta(hours=24)  # slot is days away: 24 h applies
    assert a.state == AllocationState.AWAITING_PAYMENT
    assert UrgentBookingRequest.all_types.get(pk=urg_id).status == UrgentBookingRequestStatus.APPROVED
    assert _allocate(oic, urg_id, [slot]).status_code == 400  # not twice


def test_deadline_is_the_slot_start_when_sooner(enabled, eq, oic, factory):
    urg_id = _submit(make_user(), eq).json()["id"]
    start = (timezone.now() + timedelta(hours=5)).replace(second=0, microsecond=0)
    slot = factory.slot(eq, start)
    assert _allocate(oic, urg_id, [slot]).status_code == 200
    a = SpecialAllocation.objects.get(request_id=urg_id)
    assert a.payment_deadline_at == slot.start_datetime
    assert a.short_notice is True


def test_allocation_refuses_a_stale_amount_and_taken_or_past_slots(enabled, eq, oic, factory):
    urg_id = _submit(make_user(), eq).json()["id"]
    free = factory.slot(eq, factory.at(timezone.localdate() + timedelta(days=4), 10))
    r = _allocate(oic, urg_id, [free], expected_total="1.00")
    assert r.status_code == 400 and r.json()["code"] == "amount_changed"
    taken = factory.booking(make_user(), eq, factory.at(timezone.localdate() + timedelta(days=4), 14))
    r = _allocate(oic, urg_id, list(taken.daily_slots.all()))
    assert r.status_code == 400
    past = factory.slot(eq, timezone.now() - timedelta(hours=3))
    assert _allocate(oic, urg_id, [past]).status_code == 400
    assert not SpecialAllocation.objects.exists()


def test_allocation_respects_the_external_quota(enabled, factory, oic):
    eq0 = factory.equipment(external_slot_quota_percent=0)
    EquipmentManager.objects.create(equipment=eq0, manager=oic)
    urg_id = _submit(make_user(), eq0).json()["id"]
    slot = factory.slot(eq0, factory.at(timezone.localdate() + timedelta(days=4), 10))
    r = _allocate(oic, urg_id, [slot])
    assert r.status_code == 400 and r.json()["code"] == "external_quota"
    slot.refresh_from_db()
    assert slot.booking_id is None


def test_awaiting_payment_allocations_count_toward_the_external_quota(enabled, eq, oic, factory):
    from iic_booking.equipment.external_slot_quota import ExternalSlotQuotaService

    urg_id = _submit(make_user(), eq).json()["id"]
    day = timezone.localdate() + timedelta(days=4)
    slot = factory.slot(eq, factory.at(day, 10))
    week_start, week_end = ExternalSlotQuotaService.week_bounds_for_date(day)
    before = ExternalSlotQuotaService.count_external_usage(eq, week_start, week_end)
    assert _allocate(oic, urg_id, [slot]).status_code == 200
    assert ExternalSlotQuotaService.count_external_usage(eq, week_start, week_end) == before + 1


def test_staff_cannot_allocate_their_own_request(enabled, eq):
    # An admin-type account that is also the requester (not a real flow) must still be refused.
    admin = main_admin()
    urg = UrgentBookingRequest.all_types.create(
        user=admin, equipment=eq, request_type=UrgentBookingRequestType.EXTERNAL_SPECIAL,
        status=UrgentBookingRequestStatus.PENDING, requires_slot_allocation=True,
    )
    with pytest.raises(svc.SpecialRequestError) as exc:
        special_staff.allocate(admin, urg.pk, [])
    assert exc.value.code == "own_request"


def test_staff_actions_authorise_before_validating_input(enabled, eq, oic):
    """A user who may not act on the request gets the same answer whatever they send (no validation leak)."""
    user = make_user()
    r = client_for(user).post(API, _payload(eq), format="json")
    urg_id = r.json()["id"]
    outsider = make_user(UserType.MANAGER, is_staff=True)  # an OIC of other equipment
    for path, body in (("clarify/", {"message": ""}), ("reject/", {"reason": ""}),
                       ("allocate/", {"slot_ids": [], "expected_total": "not-a-number"})):
        for who in (user, outsider):
            r = client_for(who).post(f"{STAFF}{urg_id}/{path}", body, format="json")
            assert r.status_code in (403, 404), (path, who.user_type, r.status_code, r.content)


def test_respond_checks_ownership_before_validating_the_reply(enabled, eq):
    owner = make_user()
    urg_id = client_for(owner).post(API, _payload(eq), format="json").json()["id"]
    r = client_for(make_user()).post(f"{API}{urg_id}/respond/", {"message": ""}, format="json")
    assert r.status_code == 404, r.content


def test_invalid_state_transitions_are_refused_and_change_nothing(enabled, eq, oic, factory):
    user = make_user()
    urg_id = _submit(user, eq).json()["id"]
    # replying without a question, acknowledging without an allocation
    r = client_for(user).post(f"{API}{urg_id}/respond/", {"message": "unasked"}, format="json")
    assert r.status_code == 400 and r.json()["code"] == "invalid_state"
    r = client_for(user).post(f"{API}{urg_id}/acknowledge-short-notice/", {"short_notice_ack_version": 1},
                              format="json")
    assert r.status_code == 400 and r.json()["code"] == "invalid_state"
    slot = factory.slot(eq, factory.at(timezone.localdate() + timedelta(days=14), 10))
    factory.slot(eq, factory.at(timezone.localdate() + timedelta(days=14), 13))  # keeps the weekly quota above zero
    assert _allocate(oic, urg_id, [slot]).status_code == 200
    events_before = SpecialRequestEvent.objects.filter(request_id=urg_id).count()
    other = factory.slot(eq, factory.at(timezone.localdate() + timedelta(days=15), 10))
    for who, path, data in (
        (oic, f"{STAFF}{urg_id}/clarify/", {"message": "too late to ask"}),
        (oic, f"{STAFF}{urg_id}/reject/", {"reason": "too late to reject"}),
        (oic, f"{STAFF}{urg_id}/allocate/", {"slot_ids": [other.pk]}),
        (user, f"{API}{urg_id}/withdraw/", {}),
        (user, f"{API}{urg_id}/respond/", {"message": "late reply"}),
    ):
        r = client_for(who).post(path, data, format="json")
        assert r.status_code == 400 and r.json()["code"] in ("invalid_state", "already_allocated"), (path, r.content)
    assert SpecialRequestEvent.objects.filter(request_id=urg_id).count() == events_before
    assert SpecialAllocation.objects.filter(request_id=urg_id).count() == 1
    other.refresh_from_db()
    assert other.booking_id is None

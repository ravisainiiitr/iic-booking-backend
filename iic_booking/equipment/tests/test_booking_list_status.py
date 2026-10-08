"""Derived Pending / Result Overdue booking statuses, the list_status filter and the default group order."""

import csv
import io
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.utils import timezone

from iic_booking.equipment import booking_list_status as bls
from iic_booking.equipment.models import (
    Booking,
    BookingSampleTrace,
    BookingStatus,
    EquipmentManager,
    SampleTraceStatus,
)
from iic_booking.equipment.results_overdue import overdue_booking_ids, preload
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


def _trace(booking, status, at):
    row = BookingSampleTrace.objects.create(booking=booking, status=status)
    BookingSampleTrace.objects.filter(pk=row.pk).update(created_at=at)
    return booking


def _status(booking, status):
    Booking.objects.filter(pk=booking.pk).update(status=status)
    return booking


def _reload(booking):
    b = Booking.objects.select_related("equipment").get(pk=booking.pk)
    preload([b])
    return b


@pytest.fixture
def lab(egs_factory):
    eq = egs_factory.equipment()
    admin = UserFactory(user_type=UserType.ADMIN, admin_approved=True)
    oic = UserFactory(user_type=UserType.MANAGER, admin_approved=True)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    return SimpleNamespace(f=egs_factory, eq=eq, admin=admin, oic=oic, student=egs_factory.student(), now=timezone.now())


def _derive(booking, staff_view=True, now=None):
    return bls.compute_list_status(_reload(booking), staff_view=staff_view, now=now)


def _annotated(booking_ids, staff_view=True, now=None):
    qs = bls.annotate_list_status(Booking.objects.filter(pk__in=booking_ids), staff_view=staff_view, now=now)
    return dict(qs.values_list("booking_id", "_list_status"))


@pytest.mark.django_db
def test_sample_accepted_moves_to_pending_then_result_overdue_and_completed_overrides(lab):
    now = lab.now
    future = lab.f.booking(lab.student, lab.eq, now + timedelta(days=2))
    accepted_early = _trace(lab.f.booking(lab.student, lab.eq, now + timedelta(days=2)), SampleTraceStatus.SAMPLE_ACCEPTED, now)
    due_later = _trace(lab.f.booking(lab.student, lab.eq, now - timedelta(hours=5)), SampleTraceStatus.SAMPLE_ACCEPTED, now - timedelta(hours=6))
    overdue = _trace(lab.f.booking(lab.student, lab.eq, now - timedelta(hours=40)), SampleTraceStatus.SAMPLE_ACCEPTED, now - timedelta(hours=41))
    completed = _status(
        _trace(lab.f.booking(lab.student, lab.eq, now - timedelta(hours=40)), SampleTraceStatus.SAMPLE_ACCEPTED, now - timedelta(hours=41)),
        BookingStatus.COMPLETED,
    )
    processing = _status(lab.f.booking(lab.student, lab.eq, now - timedelta(hours=3)), BookingStatus.PROCESSING)
    rejected = _trace(
        _trace(lab.f.booking(lab.student, lab.eq, now - timedelta(hours=40)), SampleTraceStatus.SAMPLE_ACCEPTED, now - timedelta(hours=41)),
        SampleTraceStatus.SAMPLE_REJECTED, now - timedelta(hours=39),
    )
    held = _trace(lab.f.booking(lab.student, lab.eq, now - timedelta(hours=40)), SampleTraceStatus.HELD_AT_OFFICE, now - timedelta(hours=41))

    expected = {
        future.pk: BookingStatus.BOOKED,
        accepted_early.pk: bls.RESULTS_PENDING,
        due_later.pk: bls.RESULTS_PENDING,
        overdue.pk: bls.RESULT_OVERDUE,
        completed.pk: BookingStatus.COMPLETED,
        processing.pk: bls.RESULTS_PENDING,
        rejected.pk: BookingStatus.BOOKED,
        held.pk: BookingStatus.BOOKED,
    }
    assert {pk: _derive(Booking(pk=pk)) for pk in expected} == expected
    assert _annotated(list(expected), now=now) == expected
    # Result Overdue is exactly the Results overdue list.
    assert set(overdue_booking_ids(Booking.objects.all(), now)) == {overdue.pk}

    # At the due time it moves from Pending to Result Overdue.
    due_at = now - timedelta(hours=5) + timedelta(hours=1) + timedelta(hours=24)
    assert _derive(due_later, now=due_at - timedelta(minutes=1)) == bls.RESULTS_PENDING
    assert _derive(due_later, now=due_at) == bls.RESULT_OVERDUE
    assert _annotated([due_later.pk], now=due_at) == {due_later.pk: bls.RESULT_OVERDUE}


@pytest.mark.django_db
def test_walk_in_pending_after_slot_end_and_user_visibility(lab):
    now = lab.now
    walk_in = lab.f.equipment(sample_submission_lead_hours=0, sample_collect_deadline_hours=0)
    upcoming = lab.f.booking(lab.student, walk_in, now + timedelta(hours=2))
    ended = lab.f.booking(lab.student, walk_in, now - timedelta(hours=3))
    late = lab.f.booking(lab.student, walk_in, now - timedelta(hours=30))
    expected = {upcoming.pk: BookingStatus.BOOKED, ended.pk: bls.RESULTS_PENDING, late.pk: bls.RESULT_OVERDUE}
    assert {pk: _derive(Booking(pk=pk), now=now) for pk in expected} == expected
    assert _annotated(list(expected), now=now) == expected

    # Users see Result Overdue only when the equipment shows the results countdown.
    assert _derive(late, staff_view=False, now=now) == bls.RESULTS_PENDING
    assert _annotated([late.pk], staff_view=False, now=now) == {late.pk: bls.RESULTS_PENDING}
    walk_in.show_results_countdown_to_users = True
    walk_in.save(update_fields=["show_results_countdown_to_users"])
    assert _derive(late, staff_view=False, now=now) == bls.RESULT_OVERDUE
    assert _annotated([late.pk], staff_view=False, now=now) == {late.pk: bls.RESULT_OVERDUE}


def _groups_fixture(lab):
    """One or two bookings per group with known slot starts; returns the expected default order."""
    now, f, eq, s = lab.now, lab.f, lab.eq, lab.student

    def at(hours):
        return now + timedelta(hours=hours)

    def accepted(start_h, accepted_h):
        return _trace(f.booking(s, eq, at(start_h)), SampleTraceStatus.SAMPLE_ACCEPTED, at(accepted_h))

    overdue_older_due = accepted(-100, -101)
    overdue_newer_due = accepted(-60, -61)
    pending_old = accepted(-10, -11)
    pending_new = accepted(-5, -6)
    booked_old = f.booking(s, eq, at(24))
    booked_new = f.booking(s, eq, at(48))
    choice_old = _status(f.booking(s, eq, at(-30)), BookingStatus.DISRUPTION_PENDING)
    choice_new = _status(f.booking(s, eq, at(-20)), BookingStatus.DISRUPTION_PENDING)
    absent_old = _status(f.booking(s, eq, at(-80)), BookingStatus.ABSENT)
    absent_new = _status(f.booking(s, eq, at(-70)), BookingStatus.ABSENT)
    unused = _status(f.booking(s, eq, at(-90)), BookingStatus.BOOKING_NOT_UTILIZED)
    cancelled_old = _status(f.booking(s, eq, at(-200)), BookingStatus.CANCELLED)
    refunded_new = _status(f.booking(s, eq, at(-150)), BookingStatus.REFUNDED)
    completed_old = _status(f.booking(s, eq, at(-300)), BookingStatus.COMPLETED)
    completed_new = _status(f.booking(s, eq, at(-250)), BookingStatus.COMPLETED)
    other = _status(f.booking(s, eq, at(-1)), BookingStatus.PENDING_PAYMENT)
    return [
        overdue_older_due, overdue_newer_due,
        pending_old, pending_new,
        booked_old, booked_new,
        choice_old, choice_new,
        absent_new, absent_old,
        unused,
        refunded_new, cancelled_old,
        completed_new, completed_old,
        other,
    ]


def _list(client, **params):
    resp = client.get("/api/bookings/", {"list_view": "1", "limit": 100, **params})
    assert resp.status_code == 200, resp.data
    return resp.data


@pytest.mark.django_db
def test_default_order_groups_secondary_order_and_pagination(lab):
    expected = [b.pk for b in _groups_fixture(lab)]
    client = lab.f.client_for(lab.admin)

    data = _list(client, ordering="default")
    assert [row["real_booking_id"] for row in data["bookings"]] == expected
    groups = [row["list_status_group"] for row in data["bookings"]]
    assert groups == sorted(groups) and groups[0] == 1 and groups[-1] == 9
    assert data["bookings"][0]["status_display"] == "Result Overdue"
    assert data["bookings"][2]["status_display"] == "Pending"
    assert data["bookings"][2]["status"] == BookingStatus.BOOKED  # stored status unchanged

    paged = []
    for offset in range(0, len(expected), 5):
        paged += [row["real_booking_id"] for row in _list(client, ordering="default", limit=5, offset=offset)["bookings"]]
    assert paged == expected
    # Filters that add DISTINCT (search, slot dates) keep the same order.
    since = (lab.now - timedelta(days=30)).date().isoformat()
    filtered = _list(client, ordering="default", search=lab.eq.name, start_date=since)["bookings"]
    assert [row["real_booking_id"] for row in filtered] == expected

    # A column sort still wins; without ordering the old newest-first default is unchanged.
    by_start = [row["real_booking_id"] for row in _list(client, ordering="start_time")["bookings"]]
    assert by_start != expected and len(by_start) == len(expected)
    newest = [row["real_booking_id"] for row in _list(client)["bookings"]]
    assert newest == sorted(expected, reverse=True)


@pytest.mark.django_db
def test_list_status_filter(lab):
    rows = _groups_fixture(lab)
    client = lab.f.client_for(lab.admin)

    def ids(**params):
        return {row["real_booking_id"] for row in _list(client, **params)["bookings"]}

    assert ids(list_status="RESULT_OVERDUE") == {rows[0].pk, rows[1].pk}
    assert ids(list_status="RESULTS_OVERDUE") == {rows[0].pk, rows[1].pk}
    assert ids(list_status="RESULT_OVERDUE") == ids(results_overdue="1")
    assert ids(list_status="RESULTS_PENDING") == {rows[2].pk, rows[3].pk}
    assert ids(list_status="BOOKED") == {rows[4].pk, rows[5].pk}
    assert ids(status="BOOKED") == {r.pk for r in rows[:6]}  # stored-status filter is unchanged
    assert ids(list_status="CANCELLED") == {rows[12].pk}
    assert ids(list_status="all") == {r.pk for r in rows}
    resp = client.get("/api/bookings/", {"list_status": "NOPE"})
    assert resp.status_code == 400


@pytest.mark.django_db
def test_user_my_bookings_hides_result_overdue_unless_countdown_shown(lab):
    rows = _groups_fixture(lab)
    client = lab.f.client_for(lab.student)
    data = _list(client, ordering="default")
    statuses = [row["list_status"] for row in data["bookings"]]
    assert bls.RESULT_OVERDUE not in statuses
    assert [row["real_booking_id"] for row in data["bookings"]][:4] == [r.pk for r in rows[:4]]
    assert statuses[:4] == [bls.RESULTS_PENDING] * 4


def _csv_rows(resp):
    text = b"".join(resp.streaming_content).decode("utf-8-sig") if resp.streaming else resp.content.decode("utf-8-sig")
    reader = list(csv.reader(io.StringIO(text)))
    header_at = next(i for i, r in enumerate(reader) if "Booking ID" in r)
    header = reader[header_at]
    return [dict(zip(header, r)) for r in reader[header_at + 1:] if r]


@pytest.mark.django_db
def test_export_follows_default_order_and_shows_new_statuses(lab):
    rows = _groups_fixture(lab)
    client = lab.f.client_for(lab.admin)
    resp = client.get("/api/bookings/export/", {"export_format": "csv", "view": "staff", "ordering": "default"})
    assert resp.status_code == 200
    exported = _csv_rows(resp)
    refs = {b.pk: b.virtual_booking_id for b in Booking.objects.filter(pk__in=[r.pk for r in rows])}
    assert [r["Booking ID"] for r in exported] == [refs[b.pk] for b in rows]
    assert [r["Status"] for r in exported[:5]] == ["Result Overdue", "Result Overdue", "Pending", "Pending", "Booked"]

    resp = client.get("/api/bookings/export/", {"export_format": "csv", "view": "staff", "list_status": "RESULTS_PENDING"})
    assert [r["Booking ID"] for r in _csv_rows(resp)] and {r["Status"] for r in _csv_rows(resp)} == {"Pending"}


@pytest.mark.django_db
def test_report_breakdown_counts_new_statuses(lab):
    rows = _groups_fixture(lab)
    client = lab.f.client_for(lab.admin)
    resp = client.get("/api/bookings/stats/")
    assert resp.status_code == 200, resp.data
    counts = resp.data["status_counts"]
    assert counts[bls.RESULT_OVERDUE] == 2 and counts[bls.RESULTS_PENDING] == 2 and counts[BookingStatus.BOOKED] == 2
    assert sum(counts.values()) == resp.data["total_bookings"] == len(rows)

    resp = client.get("/api/bookings/stats/", {"status": bls.RESULTS_PENDING})
    assert resp.status_code == 200 and resp.data["total_bookings"] == 2


@pytest.mark.django_db
def test_awaiting_completion_rows_carry_list_status(lab):
    from iic_booking.equipment.completion_reminders import bookings_awaiting_completion, serialize_awaiting_booking

    rows = _groups_fixture(lab)
    awaiting = {
        b.booking_id: serialize_awaiting_booking(b, now=lab.now)["list_status"]
        for b in bookings_awaiting_completion(now=lab.now)
    }
    assert awaiting[rows[0].pk] == awaiting[rows[1].pk] == bls.RESULT_OVERDUE
    assert awaiting[rows[2].pk] == awaiting[rows[3].pk] == bls.RESULTS_PENDING


@pytest.mark.django_db
def test_slot_calendar_hover_status_for_staff(lab, rf):
    from iic_booking.equipment.models import DailySlot
    from iic_booking.equipment.serializers import DailySlotSerializer

    rows = _groups_fixture(lab)
    slots = list(DailySlot.objects.filter(booking_id__in=[rows[0].pk, rows[2].pk, rows[4].pk]).select_related("booking"))
    request = rf.get("/")
    request.user = lab.admin
    data = DailySlotSerializer(slots, many=True, context={"request": request}).data
    by_booking = {row["real_booking_id"]: row["booking_status_display"] for row in data}
    assert by_booking == {rows[0].pk: "Result Overdue", rows[2].pk: "Pending", rows[4].pk: "Booked"}

"""Results overdue time: anchor, per-equipment hours, counters, reminders, user visibility and who may change it."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.utils import timezone

from iic_booking.communication.service import CommunicationService
from iic_booking.equipment.completion_reminders import (
    bookings_awaiting_completion_for_user,
    send_booking_completion_reminders,
    serialize_awaiting_booking,
)
from iic_booking.equipment.models import (
    Booking,
    BookingSampleTrace,
    EquipmentManager,
    EquipmentOperator,
    EquipmentTemporaryOIC,
    SampleTraceStatus,
)
from iic_booking.equipment.results_overdue import (
    booking_results_due,
    is_results_overdue,
    overdue_booking_ids,
    results_due_at,
)
from iic_booking.equipment.serializers import BookingSerializer, EquipmentAdminWriteSerializer
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


def _staff(user_type):
    return UserFactory(user_type=user_type, admin_approved=True)


def _accepted(booking, at):
    row = BookingSampleTrace.objects.create(booking=booking, status=SampleTraceStatus.SAMPLE_ACCEPTED)
    BookingSampleTrace.objects.filter(pk=row.pk).update(created_at=at)
    return booking


def _reload(booking):
    return Booking.objects.select_related("equipment", "user").get(pk=booking.pk)


@pytest.fixture
def lab(egs_factory):
    eq = egs_factory.equipment()
    oic = _staff(UserType.MANAGER)
    operator = _staff(UserType.OPERATOR)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    EquipmentOperator.objects.create(equipment=eq, operator=operator, role=EquipmentOperator.Role.PRIMARY)
    return SimpleNamespace(
        f=egs_factory, eq=eq, oic=oic, operator=operator, student=egs_factory.student(),
        admin=_staff(UserType.ADMIN), now=timezone.now(),
    )


@pytest.mark.django_db
def test_new_equipment_defaults_to_24_hours_and_hidden_countdown(lab):
    lab.eq.refresh_from_db()
    assert lab.eq.results_overdue_after_hours == 24
    assert lab.eq.show_results_countdown_to_users is False


@pytest.mark.django_db
def test_anchor_is_the_later_of_booking_end_and_receipt_plus_booked_time(lab):
    now = lab.now
    start = now - timedelta(hours=50)  # two 1 h slots: ends 48 h ago, 2 h booked
    before_slot = _accepted(lab.f.booking(lab.student, lab.eq, start, slot_count=2), now - timedelta(hours=60))
    during_slot = _accepted(lab.f.booking(lab.student, lab.eq, start, slot_count=2), now - timedelta(hours=49))
    after_slot = _accepted(lab.f.booking(lab.student, lab.eq, start, slot_count=2), now - timedelta(hours=30))

    due = booking_results_due(_reload(before_slot))
    assert due.anchor == start + timedelta(hours=2) and not due.counted_from_receipt
    assert due.due_at == start + timedelta(hours=26)
    assert due.booked == timedelta(hours=2)

    due = booking_results_due(_reload(during_slot))
    assert due.anchor == now - timedelta(hours=47) and due.counted_from_receipt  # receipt + 2 h > slot end
    assert results_due_at(_reload(during_slot)) == now - timedelta(hours=23)

    due = booking_results_due(_reload(after_slot))
    assert due.anchor == now - timedelta(hours=28)
    assert due.due_at == now - timedelta(hours=4)


@pytest.mark.django_db
def test_walk_in_and_unreceived_bookings(lab):
    walk_in = lab.f.equipment(sample_submission_lead_hours=0, sample_collect_deadline_hours=0)
    start = lab.now - timedelta(hours=10)
    booking = _reload(lab.f.booking(lab.student, walk_in, start))
    due = booking_results_due(booking)
    assert due.anchor == start + timedelta(hours=1) and due.received_at is None
    assert due.due_at == start + timedelta(hours=25)

    never_received = _reload(lab.f.booking(lab.student, lab.eq, lab.now - timedelta(days=5)))
    assert booking_results_due(never_received) is None
    assert not is_results_overdue(never_received, None, lab.now)


@pytest.mark.django_db
def test_custom_hours_and_extension(lab):
    now = lab.now
    lab.eq.results_overdue_after_hours = 48
    lab.eq.save(update_fields=["results_overdue_after_hours"])
    ended_30h = _accepted(lab.f.booking(lab.student, lab.eq, now - timedelta(hours=31)), now - timedelta(days=3))
    ended_50h = _accepted(lab.f.booking(lab.student, lab.eq, now - timedelta(hours=51)), now - timedelta(days=3))
    extended = _accepted(lab.f.booking(lab.student, lab.eq, now - timedelta(hours=51)), now - timedelta(days=3))
    Booking.objects.filter(pk=extended.pk).update(operator_absent_hold_until=now + timedelta(hours=5))

    assert set(overdue_booking_ids(Booking.objects.all(), now)) == {ended_50h.pk}
    due = booking_results_due(_reload(extended))
    assert due.extended and due.due_at == now + timedelta(hours=5)

    other = lab.f.equipment()  # default 24 h
    ended_30h_default = _accepted(lab.f.booking(lab.student, other, now - timedelta(hours=31)), now - timedelta(days=3))
    ids = set(overdue_booking_ids(Booking.objects.all(), now))
    assert ended_30h_default.pk in ids and ended_30h.pk not in ids


@pytest.mark.django_db
def test_counter_text_before_and_after_the_due_time(lab):
    now = lab.now
    booking = _accepted(lab.f.booking(lab.student, lab.eq, now - timedelta(hours=11)), now - timedelta(days=1))
    row_obj = next(b for b in bookings_awaiting_completion_for_user(lab.operator, now) if b.pk == booking.pk)

    before = serialize_awaiting_booking(row_obj, now)
    assert before["is_overdue"] is False and before["overdue"] == ""
    assert before["results_due_at"] == (now + timedelta(hours=14)).isoformat()
    assert before["results_due_at_display"] and before["overdue_after_hours"] == 24

    later = now + timedelta(hours=17)
    after = serialize_awaiting_booking(row_obj, later)
    assert after["is_overdue"] is True and after["overdue"] == "3 h"  # counted from the due time, not the slot end

    res = lab.f.client_for(lab.operator).get("/api/bookings/awaiting-completion/")
    assert res.status_code == 200 and res.data["count"] == 1 and res.data["overdue_count"] == 0


@pytest.mark.django_db
def test_no_reminder_before_the_due_time_and_one_after(lab):
    now = lab.now
    booking = _accepted(lab.f.booking(lab.student, lab.eq, now - timedelta(hours=11)), now - timedelta(days=1))
    due = results_due_at(_reload(booking))

    with patch.object(CommunicationService, "send_email") as send_email:
        assert send_booking_completion_reminders(now=due - timedelta(minutes=1)) == 0
    send_email.assert_not_called()

    with patch.object(CommunicationService, "send_email") as send_email:
        assert send_booking_completion_reminders(now=due) == 2  # the OIC and the Lab Operator
    ctx = send_email.call_args_list[0].kwargs["template_context"]
    assert booking.virtual_booking_id in ctx["bookings_html"] and "Results due by" in ctx["bookings_html"]

    from iic_booking.equipment.pending_actions import collect_pending_actions

    assert "bookings_awaiting_completion" not in {i["key"] for i in collect_pending_actions(lab.operator)}


@pytest.mark.django_db
def test_results_overdue_payload_for_staff_and_optional_for_users(lab):
    now = timezone.now()
    booking = _reload(_accepted(lab.f.booking(lab.student, lab.eq, now - timedelta(hours=40)), now - timedelta(days=3)))

    def payload(user):
        return BookingSerializer(booking, context={"request": SimpleNamespace(user=user)}).get_results_overdue(booking)

    staff = payload(lab.operator)
    assert staff["overdue"] is True and staff["overdue_by"] == "15 h" and staff["hours"] == 24
    assert staff["visible_to_user"] is False
    assert payload(lab.oic)["overdue"] is True
    assert payload(lab.student) is None

    type(lab.eq).objects.filter(pk=lab.eq.pk).update(show_results_countdown_to_users=True)
    booking = _reload(booking)
    user_view = payload(lab.student)
    assert user_view["overdue"] is True and user_view["due_display"] and user_view["visible_to_user"] is True

    Booking.objects.filter(pk=booking.pk).update(status="COMPLETED")
    booking = _reload(booking)
    assert payload(lab.operator) is None


@pytest.mark.django_db
def test_lifecycle_countdown_stops_at_the_slot_end(lab):
    from iic_booking.equipment.serializers import build_booking_lifecycle_countdown

    now = timezone.now()
    running = _reload(_accepted(lab.f.booking(lab.student, lab.eq, now - timedelta(minutes=30)), now - timedelta(hours=1)))
    assert build_booking_lifecycle_countdown(running)["phase"] == "booking"
    ended = _reload(_accepted(lab.f.booking(lab.student, lab.eq, now - timedelta(hours=3)), now - timedelta(hours=4)))
    assert build_booking_lifecycle_countdown(ended) is None


@pytest.mark.django_db
def test_who_may_change_the_results_overdue_time(lab):
    f = lab.f
    url = f"/api/oic/equipment-settings/{lab.eq.pk}/"
    row = f.client_for(lab.oic).get("/api/oic/equipment-settings/").data["equipments"][0]["settings"]
    assert row["results_overdue_after_hours"] == 24 and row["show_results_countdown_to_users"] is False

    body = {"results_overdue_after_hours": 36, "show_results_countdown_to_users": True}
    assert f.client_for(lab.oic).patch(url, body, format="json").status_code == 200
    lab.eq.refresh_from_db()
    assert (lab.eq.results_overdue_after_hours, lab.eq.show_results_countdown_to_users) == (36, True)

    other_oic = _staff(UserType.MANAGER)
    assert f.client_for(other_oic).patch(url, body, format="json").status_code == 403
    assert f.client_for(lab.operator).patch(url, body, format="json").status_code == 403
    assert f.client_for(lab.student).patch(url, body, format="json").status_code == 403

    temp = _staff(UserType.MANAGER)
    EquipmentTemporaryOIC.objects.create(
        equipment=lab.eq, temporary_oic=temp, primary_oic=lab.oic, resume_at=timezone.now() + timedelta(days=3)
    )
    assert f.client_for(temp).patch(url, {"results_overdue_after_hours": 12}, format="json").status_code == 200
    assert f.client_for(lab.admin).patch(url, {"results_overdue_after_hours": 720}, format="json").status_code == 200
    for bad in (0, 721, "x"):
        res = f.client_for(lab.oic).patch(url, {"results_overdue_after_hours": bad}, format="json")
        assert res.status_code == 400 and "results_overdue_after_hours" in res.data["errors"], bad
    lab.eq.refresh_from_db()
    assert lab.eq.results_overdue_after_hours == 720

    def _valid(user, data):
        s = EquipmentAdminWriteSerializer(lab.eq, data=data, partial=True, context={"request": SimpleNamespace(user=user)})
        return s.is_valid(), s.errors

    ok, errors = _valid(_staff(UserType.DEPT_ADMIN), {"results_overdue_after_hours": 10})
    assert not ok and "results_overdue_after_hours" in errors
    ok, errors = _valid(lab.oic, {"results_overdue_after_hours": 10, "show_results_countdown_to_users": False})
    assert ok, errors
    ok, errors = _valid(lab.admin, {"results_overdue_after_hours": 0})
    assert not ok

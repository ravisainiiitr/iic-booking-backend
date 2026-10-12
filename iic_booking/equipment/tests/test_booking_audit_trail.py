"""Booking Event History audit trail: who changed the status (role or System), why and from where."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.utils import timezone

from iic_booking.equipment import booking_audit
from iic_booking.equipment.booking_events import create_booking_event
from iic_booking.equipment.models import (
    BookingDisruptionKind,
    BookingEvent,
    BookingEventType,
    BookingStatus,
    EquipmentManager,
    EquipmentTemporaryOIC,
)
from iic_booking.equipment.serializers import BookingEventSerializer
from iic_booking.equipment.tests.conftest import _EgsFactory
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def lab(monkeypatch):
    from iic_booking.equipment import tasks, waitlist

    monkeypatch.setattr(tasks.send_manual_disruption_operational_followup, "apply_async", lambda *a, **k: None)
    monkeypatch.setattr(waitlist, "notify_waitlist_slots_available", lambda *a, **k: 0)
    f = _EgsFactory()
    eq = f.equipment()
    oic = UserFactory(user_type=UserType.MANAGER, department=f.department)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    return SimpleNamespace(f=f, eq=eq, student=f.student(), oic=oic)


def _audit(event):
    return BookingEventSerializer(event).data["audit"]


def _status_events(booking):
    return list(BookingEvent.objects.filter(booking=booking).exclude(new_status__isnull=True).order_by("created_at"))


@pytest.fixture
def scheduled_job():
    def run(name, fn):
        task_id = f"t-{name}"
        booking_audit._on_task_prerun(task_id=task_id, task=SimpleNamespace(name=name))
        try:
            return fn()
        finally:
            booking_audit._on_task_postrun(task_id=task_id)

    return run


def test_oic_marking_operator_absent_records_who_and_why(lab):
    booking = lab.f.booking(lab.student, lab.eq, lab.f.future(days=1))

    res = lab.f.client_for(lab.oic).post(
        f"/api/bookings/{booking.pk}/absent/", {"notes": "Operator on medical leave"}, format="json"
    )

    assert res.status_code == 200, res.data
    event = BookingEvent.objects.get(booking=booking, new_status=BookingStatus.DISRUPTION_PENDING)
    assert (event.previous_status, event.created_by) == (BookingStatus.BOOKED, lab.oic)
    audit = _audit(event)
    assert audit["actor_role"] == booking_audit.ROLE_OIC
    assert audit["actor_role_label"] == "Officer In Charge"
    assert audit["reason_code"] == booking_audit.REASON_OPERATOR_ABSENT
    assert audit["reason_label"] == "Operator Absent"
    assert audit["reason"] == "Operator on medical leave"
    assert audit["source"] == booking_audit.SOURCE_WEB
    assert "Marked Operator Absent" in event.comment


def test_safeguard_job_marks_operator_absent_as_system(lab, scheduled_job):
    from iic_booking.equipment.maintenance_policy import apply_operator_absent_disruption_for_booking

    booking = lab.f.booking(lab.student, lab.eq, lab.f.future(days=-3))

    scheduled_job(
        "equipment.auto_mark_operator_absent_disruption_after_booking_end",
        lambda: apply_operator_absent_disruption_for_booking(booking, reason="Results deadline passed"),
    )

    event = BookingEvent.objects.get(booking=booking, new_status=BookingStatus.DISRUPTION_PENDING)
    assert event.created_by is None
    audit = _audit(event)
    assert (audit["actor_kind"], audit["actor_role"]) == ("system", booking_audit.ROLE_SYSTEM)
    assert audit["source_label"] == "Scheduled job: Operator-absent safeguard"
    assert audit["reason"] == "Results deadline passed"


def test_analysis_not_possible_and_maintenance_are_recorded(lab):
    from iic_booking.equipment.maintenance_policy import (
        apply_maintenance_disruption_for_booking_manually,
        apply_other_disruption_for_booking_manually,
    )

    first = lab.f.booking(lab.student, lab.eq, lab.f.future(days=1))
    second = lab.f.booking(lab.student, lab.eq, lab.f.future(days=2))
    with booking_audit.audit_scope(source=booking_audit.SOURCE_WEB, actor=lab.oic):
        apply_other_disruption_for_booking_manually(first, reason="Detector fault")
        apply_maintenance_disruption_for_booking_manually(second, notes="Vacuum pump service")

    other = _audit(BookingEvent.objects.get(booking=first, new_status=BookingStatus.DISRUPTION_PENDING))
    assert (other["reason_code"], other["reason"]) == (booking_audit.REASON_ANALYSIS_NOT_POSSIBLE, "Detector fault")
    maint = _audit(BookingEvent.objects.get(booking=second, new_status=BookingStatus.DISRUPTION_PENDING))
    assert (maint["reason_code"], maint["actor_role"]) == (booking_audit.REASON_UNDER_MAINTENANCE, booking_audit.ROLE_OIC)


def test_auto_refund_after_five_days_is_recorded_as_system(lab, scheduled_job):
    from iic_booking.equipment.maintenance_policy import auto_cancel_expired_maintenance_bookings
    from iic_booking.equipment.tests.fabrication_helpers import funded_student

    student, _sub = funded_student(lab.f, balance="1000.00")
    booking = lab.f.booking(student, lab.eq, lab.f.future(days=-6), total_charge="100.00")
    booking.status = BookingStatus.DISRUPTION_PENDING
    booking.disruption_kind = BookingDisruptionKind.OPERATOR_ABSENT
    booking.maintenance_disruption_flag = True
    booking.maintenance_decision_deadline_at = timezone.now() - timedelta(minutes=1)
    booking.save()

    assert scheduled_job("equipment.process_maintenance_booking_deadlines", auto_cancel_expired_maintenance_bookings) == 1

    event = BookingEvent.objects.get(booking=booking, new_status=BookingStatus.REFUNDED)
    assert event.created_by is None
    audit = _audit(event)
    assert audit["actor_kind"] == "system"
    assert audit["reason_code"] == booking_audit.REASON_DISRUPTION_DEADLINE
    assert audit["reason"] == "No refund/reschedule choice within 5 days of Operator Absent"
    assert audit["source_label"] == "Scheduled job: 5-day disruption decision deadline"


def test_user_refund_after_operator_absent_is_recorded(lab):
    from iic_booking.equipment.booking_cancellation import perform_booking_cancellation
    from iic_booking.equipment.tests.fabrication_helpers import funded_student

    student, _sub = funded_student(lab.f, balance="1000.00")
    booking = lab.f.booking(student, lab.eq, lab.f.future(days=-1), total_charge="100.00")
    booking.status = BookingStatus.DISRUPTION_PENDING
    booking.disruption_kind = BookingDisruptionKind.OPERATOR_ABSENT
    booking.maintenance_disruption_flag = True
    booking.save()

    perform_booking_cancellation(
        booking,
        slot_ids=list(booking.daily_slots.values_list("id", flat=True)),
        should_refund=True,
        cancel_notes="",
        actor=student,
        allow_started_slots=True,
    )

    event = BookingEvent.objects.get(booking=booking, previous_status=BookingStatus.DISRUPTION_PENDING)
    audit = _audit(event)
    assert audit["actor_role"] == booking_audit.ROLE_USER
    assert audit["reason_code"] == booking_audit.REASON_REFUND_AFTER_DISRUPTION
    assert audit["reason"] == "User chose a full refund after Operator Absent"


def test_reschedule_choice_names_the_disruption(lab):
    booking = lab.f.booking(lab.student, lab.eq, lab.f.future(days=1))
    booking.disruption_kind = BookingDisruptionKind.MAINTENANCE
    booking.maintenance_disruption_flag = True

    by_user = booking_audit.disruption_choice_audit(
        booking, BookingStatus.DISRUPTION_PENDING, lab.student, choice="reschedule"
    )["audit"]
    by_staff = booking_audit.disruption_choice_audit(
        booking, BookingStatus.DISRUPTION_PENDING, lab.oic, choice="reschedule"
    )["audit"]

    assert by_user["reason"] == "User chose to reschedule after Under Maintenance"
    assert by_staff["reason"] == "Staff rescheduled the booking after Under Maintenance"
    booking.maintenance_disruption_flag = False
    assert booking_audit.disruption_choice_audit(booking, BookingStatus.BOOKED, lab.student, choice="refund") == {}


def test_status_change_without_an_event_is_still_recorded(lab, django_capture_on_commit_callbacks):
    booking = lab.f.booking(lab.student, lab.eq, lab.f.future(days=1))
    booking.refresh_from_db()

    with booking_audit.audit_scope(source=booking_audit.SOURCE_WEB, actor=lab.oic):
        with django_capture_on_commit_callbacks(execute=True):
            booking.status = BookingStatus.COMPLETED
            booking.save(update_fields=["status"])

    (event,) = _status_events(booking)
    assert (event.previous_status, event.new_status) == (BookingStatus.BOOKED, BookingStatus.COMPLETED)
    audit = _audit(event)
    assert audit["auto_recorded"] is True
    assert audit["actor_role"] == booking_audit.ROLE_OIC


def test_status_change_with_its_own_event_is_not_recorded_twice(lab, django_capture_on_commit_callbacks):
    booking = lab.f.booking(lab.student, lab.eq, lab.f.future(days=1))
    booking.refresh_from_db()

    with django_capture_on_commit_callbacks(execute=True):
        booking.status = BookingStatus.COMPLETED
        booking.save()
        create_booking_event(
            booking=booking,
            event_type=BookingEventType.COMPLETED,
            created_by=lab.oic,
            previous_status=BookingStatus.BOOKED,
            new_status=BookingStatus.COMPLETED,
            send_notification=False,
        )

    assert len(_status_events(booking)) == 1


def test_unlogged_change_in_a_request_is_attributed_to_the_signed_in_user(lab, django_capture_on_commit_callbacks):
    booking = lab.f.booking(lab.student, lab.eq, lab.f.future(days=1))
    booking.refresh_from_db()
    request = SimpleNamespace(user=lab.oic)

    with booking_audit.audit_scope(source=booking_audit.SOURCE_WEB, source_label="Web portal", request=request):
        with django_capture_on_commit_callbacks(execute=True):
            booking.status = BookingStatus.PENDING
            booking.save(update_fields=["status"])

    (event,) = _status_events(booking)
    assert event.created_by == lab.oic
    assert event.metadata["audit"]["source_label"] == "Web portal"


def test_event_without_actor_on_another_users_booking_keeps_the_booking_user(lab):
    booking = lab.f.booking(lab.student, lab.eq, lab.f.future(days=1))
    other = lab.f.student()

    with booking_audit.audit_scope(source=booking_audit.SOURCE_WEB, request=SimpleNamespace(user=other)):
        event = create_booking_event(booking, BookingEventType.COMMENT, comment="Waitlist", send_notification=False)

    assert event.created_by == lab.student
    assert booking_audit.actor_role_for_booking(other, booking) == booking_audit.ROLE_OTHER_USER


def test_background_job_without_actor_is_the_system(lab, scheduled_job):
    booking = lab.f.booking(lab.student, lab.eq, lab.f.future(days=1))

    event = scheduled_job(
        "equipment.some_job",
        lambda: create_booking_event(booking, BookingEventType.COMMENT, comment="Swept", send_notification=False),
    )

    assert event.created_by is None
    assert event.metadata["audit"]["source_label"] == "Background job: equipment.some_job"


def test_temporary_oic_and_admin_roles(lab):
    booking = lab.f.booking(lab.student, lab.eq, lab.f.future(days=1))
    temp = UserFactory(user_type=UserType.MANAGER, department=lab.f.department)
    EquipmentTemporaryOIC.objects.create(
        equipment=lab.eq,
        primary_oic=lab.oic,
        temporary_oic=temp,
        resume_at=timezone.now() + timedelta(days=3),
    )
    admin = UserFactory(user_type=UserType.ADMIN)

    assert booking_audit.actor_role_for_booking(temp, booking) == booking_audit.ROLE_TEMP_OIC
    assert booking_audit.actor_role_for_booking(admin, booking) == booking_audit.ROLE_MAIN_ADMIN
    assert booking_audit.actor_role_for_booking(lab.student, booking) == booking_audit.ROLE_USER


def test_events_stored_before_the_trail_are_attributed(lab):
    booking = lab.f.booking(lab.student, lab.eq, lab.f.future(days=1))
    by_oic = BookingEvent.objects.create(booking=booking, event_type=BookingEventType.COMMENT, created_by=lab.oic)
    automatic = BookingEvent.objects.create(
        booking=booking,
        event_type=BookingEventType.CANCELLED,
        created_by=lab.student,
        comment="Auto-cancelled: no choice within the deadline.",
    )
    system = BookingEvent.objects.create(booking=booking, event_type=BookingEventType.COMMENT, created_by=None)

    assert _audit(by_oic)["actor_role"] == booking_audit.ROLE_OIC
    assert _audit(automatic)["actor_kind"] == "system"
    assert _audit(system)["actor_role"] == booking_audit.ROLE_SYSTEM

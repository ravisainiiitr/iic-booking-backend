"""Equipment Under Maintenance handled day by day (``equipment.maintenance_auto``)."""

from __future__ import annotations

import importlib
from datetime import datetime, time, timedelta

import pytest
from django.apps import apps
from django.utils import timezone

from iic_booking.equipment import maintenance_auto
from iic_booking.equipment.models import (
    Booking,
    BookingEvent,
    BookingStatus,
    DailySlot,
    EquipmentMaintenancePeriod,
    EquipmentStatus,
    MaintenanceNotification,
    SlotMaster,
    SlotStatus,
)
from iic_booking.equipment.tests.test_disruption_log import (
    _book,
    _bulk,
    _client,
    _equipment,
    _future_weekday,
    _no_holidays,  # noqa: F401
    _oic_for,
    _user,
)
from iic_booking.users.models.user_type import UserType

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _locmem_email(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.FRONTEND_URL = "https://portal.example"


def _patch(user, eq, payload):
    return _client(user).patch(f"/api/equipments/{eq.pk}/", payload, format="json")


def _slot_at(eq, start, *, minutes=60, status=SlotStatus.AVAILABLE, number=None) -> DailySlot:
    master = SlotMaster.objects.filter(equipment=eq).order_by("slot_number")[0]
    if number is not None:
        master = SlotMaster.objects.create(
            equipment=eq, slot_number=number, open_time=time(0), close_time=time(1), is_active=True
        )
    return DailySlot.objects.create(
        slot_master=master,
        date=timezone.localtime(start).date(),
        start_datetime=start,
        end_datetime=start + timedelta(minutes=minutes),
        status=status,
    )


def _slots(eq, day) -> list[DailySlot]:
    """The equipment's regular slots on ``day`` (masters added by ``_slot_at`` use numbers >= 20)."""
    out = []
    for master in SlotMaster.objects.filter(equipment=eq, slot_number__lt=20).order_by("slot_number"):
        start = timezone.make_aware(datetime.combine(day, master.open_time))
        slot, _ = DailySlot.objects.get_or_create(
            slot_master=master,
            date=day,
            defaults={"start_datetime": start, "end_datetime": start + timedelta(hours=1), "status": SlotStatus.AVAILABLE},
        )
        out.append(slot)
    return out


def _later_today(eq, hours=1, number=20):
    return _slot_at(eq, timezone.now() + timedelta(hours=hours), number=number)


def _subjects(mailoutbox, user):
    return [m.subject for m in mailoutbox if user.email in m.to]


def _mark(user, eq, capture):
    with capture(execute=True):
        res = _patch(user, eq, {"status": "REPAIR", "disruption_reason": "Detector fault"})
    assert res.status_code == 200, res.data
    return res


def _operational(user, eq, capture):
    with capture(execute=True):
        res = _patch(user, eq, {"status": "ACTIVE"})
    assert res.status_code == 200, res.data
    return res


@pytest.fixture
def lab():
    eq = _equipment()
    oic = _oic_for(eq)
    return eq, oic


# --- marking ------------------------------------------------------------------------------------------------------


def test_marking_converts_only_todays_remaining_slots_and_records_the_period(lab, django_capture_on_commit_callbacks):
    eq, oic = lab
    past = _slot_at(eq, timezone.now() - timedelta(hours=3), number=21)
    later = _later_today(eq)
    future = _slots(eq, _future_weekday())

    _mark(oic, eq, django_capture_on_commit_callbacks)

    past.refresh_from_db()
    later.refresh_from_db()
    assert later.status == SlotStatus.UNDER_MAINTENANCE
    assert past.status == SlotStatus.AVAILABLE
    assert {s.status for s in DailySlot.objects.filter(pk__in=[s.pk for s in future])} == {SlotStatus.AVAILABLE}
    period = EquipmentMaintenancePeriod.objects.get(equipment=eq)
    assert period.ended_at is None and period.started_by_id == oic.pk
    assert period.auto_slot_ids == [later.pk]
    assert period.last_checked_on == timezone.localdate()
    assert period.disruption_event is not None and period.disruption_event.ended_at is None


def test_marking_puts_todays_booking_on_hold_and_tells_every_upcoming_user(
    lab, mailoutbox, django_capture_on_commit_callbacks
):
    eq, oic = lab
    today_user, later_user = _user(UserType.STUDENT), _user(UserType.STUDENT)
    today = _book(eq, _later_today(eq), today_user)
    day = _future_weekday()
    s9, s10, *_ = _slots(eq, day)
    future = _book(eq, s9, later_user)
    future_2 = _book(eq, s10, later_user)

    _mark(oic, eq, django_capture_on_commit_callbacks)

    today.refresh_from_db()
    future.refresh_from_db()
    assert today.status == BookingStatus.DISRUPTION_PENDING and today.maintenance_disruption_flag
    assert today.disruption_release_slot_status == SlotStatus.UNDER_MAINTENANCE
    assert future.status == BookingStatus.BOOKED
    today_subjects = _subjects(mailoutbox, today_user)
    assert len(today_subjects) == 1 and today_subjects[0].startswith("Action needed:")
    later_subjects = _subjects(mailoutbox, later_user)
    assert later_subjects == [f"{eq.name} is under maintenance – your upcoming bookings"]
    body = next(m for m in mailoutbox if later_user.email in m.to).body
    assert future.virtual_booking_id in body and future_2.virtual_booking_id in body
    assert "8:30 AM" in body
    staff = _subjects(mailoutbox, oic)
    assert len(staff) == 1 and staff[0].startswith(f"Under maintenance: {eq.name} – 3 bookings affected")
    kinds = sorted(MaintenanceNotification.objects.values_list("kind", flat=True))
    assert kinds == ["DAY", "MARKED", "MARKED", "STAFF_MARKED"]


def test_marking_emails_are_sent_at_once_despite_quiet_hours(lab, mailoutbox, django_capture_on_commit_callbacks):
    from iic_booking.communication.models import NotificationDeliverySettings, NotificationPreference

    eq, oic = lab
    user = _user(UserType.STUDENT)
    _book(eq, _slots(eq, _future_weekday())[0], user)
    NotificationDeliverySettings.objects.create(
        user=user, quiet_hours_enabled=True, quiet_start=time(0, 0), quiet_end=time(23, 59)
    )
    NotificationPreference.objects.create(user=user, category="bookings", email_enabled=True, email_digest=True)

    _mark(oic, eq, django_capture_on_commit_callbacks)

    assert len(_subjects(mailoutbox, user)) == 1


def test_start_period_twice_sends_nothing_new(lab, mailoutbox, django_capture_on_commit_callbacks):
    eq, oic = lab
    _book(eq, _slots(eq, _future_weekday())[0], _user(UserType.STUDENT))
    _mark(oic, eq, django_capture_on_commit_callbacks)
    sent = len(mailoutbox)

    with django_capture_on_commit_callbacks(execute=True):
        out = maintenance_auto.start_period(eq)

    assert out.already_active
    assert len(mailoutbox) == sent
    assert EquipmentMaintenancePeriod.objects.filter(equipment=eq).count() == 1


# --- 08:30 check --------------------------------------------------------------------------------------------------


def _morning(day):
    return timezone.make_aware(datetime.combine(day, time(8, 30)))


def test_daily_check_handles_that_days_bookings_once(lab, mailoutbox, django_capture_on_commit_callbacks):
    eq, oic = lab
    user = _user(UserType.STUDENT)
    day = _future_weekday()
    s9, s10, s11, s12 = _slots(eq, day)
    booking = _book(eq, s10, user)
    _mark(oic, eq, django_capture_on_commit_callbacks)
    assert _subjects(mailoutbox, user) == [f"{eq.name} is under maintenance – your upcoming bookings"]

    with django_capture_on_commit_callbacks(execute=True):
        stats = maintenance_auto.daily_check(now=_morning(day))

    assert stats["handled"] == 1 and stats["bookings_on_hold"] == 1
    booking.refresh_from_db()
    assert booking.status == BookingStatus.DISRUPTION_PENDING
    assert {s.status for s in DailySlot.objects.filter(pk__in=[s9.pk, s11.pk, s12.pk])} == {
        SlotStatus.UNDER_MAINTENANCE
    }
    subjects = _subjects(mailoutbox, user)
    assert len(subjects) == 2 and subjects[1].startswith("Action needed:")
    assert any(s.startswith(f"Still under maintenance: {eq.name}") for s in _subjects(mailoutbox, oic))
    sent = len(mailoutbox)

    with django_capture_on_commit_callbacks(execute=True):
        again = maintenance_auto.daily_check(now=_morning(day) + timedelta(minutes=5))

    assert again["handled"] == 0 and again["skipped"] == 1
    assert len(mailoutbox) == sent
    assert EquipmentMaintenancePeriod.objects.get(equipment=eq).last_checked_on == day


def test_daily_check_without_bookings_sends_no_staff_summary(lab, mailoutbox, django_capture_on_commit_callbacks):
    eq, oic = lab
    day = _future_weekday()
    _slots(eq, day)
    _mark(oic, eq, django_capture_on_commit_callbacks)
    sent = len(mailoutbox)

    with django_capture_on_commit_callbacks(execute=True):
        stats = maintenance_auto.daily_check(now=_morning(day))

    assert stats["handled"] == 1 and stats["slots_marked"] == 4
    assert len(mailoutbox) == sent


def test_daily_check_closes_period_when_status_changed_without_signal(lab, django_capture_on_commit_callbacks):
    from iic_booking.equipment.models import Equipment

    eq, oic = lab
    _mark(oic, eq, django_capture_on_commit_callbacks)
    Equipment.objects.filter(pk=eq.pk).update(status=EquipmentStatus.ACTIVE)

    with django_capture_on_commit_callbacks(execute=True):
        stats = maintenance_auto.daily_check(now=_morning(_future_weekday()))

    assert stats["ended"] == 1
    assert EquipmentMaintenancePeriod.objects.get(equipment=eq).ended_at is not None


def test_equipment_already_under_maintenance_is_not_touched_by_daily_check(mailoutbox):
    from iic_booking.equipment.models import Equipment

    eq = _equipment()
    _oic_for(eq)
    user = _user(UserType.STUDENT)
    day = _future_weekday()
    booking = _book(eq, _slots(eq, day)[0], user)
    Equipment.objects.filter(pk=eq.pk).update(status=EquipmentStatus.REPAIR)

    stats = maintenance_auto.daily_check(now=_morning(day))
    report = maintenance_auto.dry_run_report_text(now=_morning(day) - timedelta(days=1))

    assert stats["periods"] == 0
    booking.refresh_from_db()
    assert booking.status == BookingStatus.BOOKED
    assert mailoutbox == []
    assert f"upcoming_to_notify=[{booking.booking_id}]" in report and "users_to_notify=1" in report
    assert not EquipmentMaintenancePeriod.objects.exists()


# --- legacy MAINTENANCE status ------------------------------------------------------------------------------------


def test_legacy_maintenance_status_counts_as_under_maintenance():
    from iic_booking.equipment.maintenance_policy import is_equipment_under_maintenance_status

    assert is_equipment_under_maintenance_status(EquipmentStatus.MAINTENANCE)
    assert is_equipment_under_maintenance_status("maintenance")
    assert not is_equipment_under_maintenance_status(EquipmentStatus.ACTIVE)


def test_mark_repair_command_moves_legacy_maintenance_through_the_normal_path(
    lab, mailoutbox, django_capture_on_commit_callbacks
):
    from io import StringIO

    from django.core.management import call_command

    from iic_booking.equipment.models import DisruptionEvent, Equipment

    eq, oic = lab
    Equipment.objects.filter(pk=eq.pk).update(status=EquipmentStatus.MAINTENANCE)
    later = _later_today(eq)
    user = _user(UserType.STUDENT)
    _book(eq, _slots(eq, _future_weekday())[0], user)

    dry = StringIO()
    call_command("maintenance_auto", "--mark-repair", str(eq.pk), stdout=dry)
    eq.refresh_from_db()
    assert eq.status == EquipmentStatus.MAINTENANCE and "Dry run only" in dry.getvalue()

    out = StringIO()
    with django_capture_on_commit_callbacks(execute=True):
        call_command("maintenance_auto", "--mark-repair", str(eq.pk), "--reason", "Legacy status", "--apply", stdout=out)

    eq.refresh_from_db()
    later.refresh_from_db()
    assert eq.status == EquipmentStatus.REPAIR
    period = EquipmentMaintenancePeriod.objects.get(equipment=eq, ended_at__isnull=True)
    assert f"period={period.pk}" in out.getvalue() and "slots_marked=1" in out.getvalue()
    assert later.status == SlotStatus.UNDER_MAINTENANCE
    assert DisruptionEvent.objects.filter(equipment=eq, ended_at__isnull=True).count() == 1
    assert len(_subjects(mailoutbox, user)) == 1


def test_repair_to_inactive_does_not_start_a_second_period(lab, django_capture_on_commit_callbacks):
    eq, oic = lab
    _mark(oic, eq, django_capture_on_commit_callbacks)
    with django_capture_on_commit_callbacks(execute=True):
        _patch(_user(UserType.ADMIN), eq, {"status": "INACTIVE"})
    assert EquipmentMaintenancePeriod.objects.filter(equipment=eq).count() == 1


# --- operational again --------------------------------------------------------------------------------------------


def test_operational_restores_only_slots_the_mechanism_set(lab, django_capture_on_commit_callbacks):
    eq, oic = lab
    later = _later_today(eq)
    day = _future_weekday()
    s9, s10, s11, s12 = _slots(eq, day)
    _mark(oic, eq, django_capture_on_commit_callbacks)
    with django_capture_on_commit_callbacks(execute=True):
        maintenance_auto.daily_check(now=_morning(day))
    res = _bulk(oic, eq, status="UNDER_MAINTENANCE", slot_ids=[s11.pk], disruption_reason="Filament")
    assert res.status_code == 200, res.data
    generated = _slot_at(eq, timezone.make_aware(datetime.combine(day + timedelta(days=1), time(15))),
                         status=SlotStatus.UNDER_MAINTENANCE, number=22)

    _operational(oic, eq, django_capture_on_commit_callbacks)

    statuses = dict(DailySlot.objects.filter(
        pk__in=[later.pk, s9.pk, s10.pk, s11.pk, s12.pk, generated.pk]).values_list("pk", "status"))
    assert statuses[s11.pk] == SlotStatus.UNDER_MAINTENANCE
    assert statuses[later.pk] == statuses[s9.pk] == statuses[s12.pk] == statuses[generated.pk] == SlotStatus.AVAILABLE
    period = EquipmentMaintenancePeriod.objects.get(equipment=eq)
    assert period.ended_at is not None and period.ended_by_id == oic.pk


def test_operational_tells_users_with_remaining_bookings_and_staff(lab, mailoutbox, django_capture_on_commit_callbacks):
    eq, oic = lab
    user = _user(UserType.STUDENT)
    booking = _book(eq, _slots(eq, _future_weekday())[0], user)
    _mark(oic, eq, django_capture_on_commit_callbacks)

    _operational(oic, eq, django_capture_on_commit_callbacks)

    subjects = _subjects(mailoutbox, user)
    assert subjects[-1] == f"{eq.name} is operational – your booking stands"
    assert booking.virtual_booking_id in next(m for m in reversed(mailoutbox) if user.email in m.to).body
    assert _subjects(mailoutbox, oic)[-1] == f"Operational again: {eq.name}"
    sent = len(mailoutbox)
    _mark(oic, eq, django_capture_on_commit_callbacks)
    assert EquipmentMaintenancePeriod.objects.filter(equipment=eq).count() == 2
    assert len(_subjects(mailoutbox, user)) == len(subjects) + 1
    assert len(mailoutbox) > sent


def test_operational_without_period_keeps_the_previous_behaviour():
    from iic_booking.equipment.models import Equipment

    eq = _equipment()
    oic = _oic_for(eq)
    day = _future_weekday()
    slots = _slots(eq, day)
    Equipment.objects.filter(pk=eq.pk).update(status=EquipmentStatus.REPAIR)
    DailySlot.objects.filter(pk__in=[s.pk for s in slots]).update(status=SlotStatus.UNDER_MAINTENANCE)

    res = _patch(oic, eq, {"status": "ACTIVE"})

    assert res.status_code == 200, res.data
    assert {s.status for s in DailySlot.objects.filter(pk__in=[s.pk for s in slots])} == {SlotStatus.AVAILABLE}


# --- results overdue pause ----------------------------------------------------------------------------------------


def _awaiting_results(eq, user, hours_ago=30):
    slot = _slot_at(eq, timezone.now() - timedelta(hours=hours_ago + 1), number=30 + hours_ago)
    booking = _book(eq, slot, user)
    Booking.objects.filter(pk=booking.pk).update(status=BookingStatus.PROCESSING)
    booking.refresh_from_db()
    return booking


def test_results_overdue_paused_while_under_maintenance_and_resumed_with_extension(
    lab, django_capture_on_commit_callbacks
):
    from iic_booking.equipment.results_overdue import booking_results_due, overdue_booking_ids

    eq, oic = lab
    booking = _awaiting_results(eq, _user(UserType.STUDENT))
    original_due = booking_results_due(booking).due_at
    assert overdue_booking_ids(Booking.objects.filter(pk=booking.pk)) == [booking.pk]

    _mark(oic, eq, django_capture_on_commit_callbacks)

    booking.refresh_from_db()
    assert booking.results_paused_at is not None
    assert overdue_booking_ids(Booking.objects.filter(pk=booking.pk)) == []
    assert BookingEvent.objects.filter(booking=booking, metadata__results_pause="paused").exists()

    paused_for = timedelta(hours=10)
    Booking.objects.filter(pk=booking.pk).update(results_paused_at=timezone.now() - paused_for)
    _operational(oic, eq, django_capture_on_commit_callbacks)

    booking.refresh_from_db()
    assert booking.results_paused_at is None and booking.results_pause_period is None
    extended = booking.operator_absent_hold_until - original_due
    assert abs(extended - paused_for) < timedelta(minutes=1)
    assert overdue_booking_ids(Booking.objects.filter(pk=booking.pk)) == []
    event = BookingEvent.objects.get(booking=booking, metadata__results_pause="resumed")
    assert event.metadata["paused_seconds"] >= int(paused_for.total_seconds()) - 60


def test_paused_booking_is_skipped_by_safeguard_and_listed_as_paused(lab, django_capture_on_commit_callbacks):
    from iic_booking.equipment.results_deadline import AutomationState, safeguard_due_at

    eq, oic = lab
    booking = _awaiting_results(eq, _user(UserType.STUDENT))
    _mark(oic, eq, django_capture_on_commit_callbacks)
    booking.refresh_from_db()

    due, _mode = safeguard_due_at(
        booking, booking.daily_slots.first().end_datetime, legacy_hours=1,
        state=AutomationState(enabled=False, since=None),
    )
    assert due is None

    res = _client(oic).get("/api/bookings/results-overdue/")
    assert res.status_code == 200
    assert res.data["count"] == 0
    assert [r["booking_id"] for r in res.data["paused"]] == [booking.pk]
    assert res.data["paused"][0]["reason"] == maintenance_auto.PAUSE_REASON

    from rest_framework.request import Request
    from rest_framework.test import APIRequestFactory, force_authenticate

    from iic_booking.equipment.serializers import BookingSerializer

    raw = APIRequestFactory().get("/")
    force_authenticate(raw, user=oic)
    request = Request(raw)
    request._force_auth_user = oic
    request.user = oic
    data = BookingSerializer(booking, context={"request": request}).data
    assert data["results_paused"]["paused"] is True
    assert data["results_paused"]["reason"] == maintenance_auto.PAUSE_REASON


def test_completed_booking_during_maintenance_is_unpaused_without_extension(lab, django_capture_on_commit_callbacks):
    eq, oic = lab
    booking = _awaiting_results(eq, _user(UserType.STUDENT))
    _mark(oic, eq, django_capture_on_commit_callbacks)
    Booking.objects.filter(pk=booking.pk).update(status=BookingStatus.COMPLETED)

    _operational(oic, eq, django_capture_on_commit_callbacks)

    booking.refresh_from_db()
    assert booking.results_paused_at is None and booking.operator_absent_hold_until is None


# --- staff banner -------------------------------------------------------------------------------------------------


def test_maintenance_status_banner_counts(lab, django_capture_on_commit_callbacks):
    eq, oic = lab
    u1, u2 = _user(UserType.STUDENT), _user(UserType.STUDENT)
    _book(eq, _later_today(eq), u1)
    _book(eq, _slots(eq, _future_weekday())[0], u2)
    _awaiting_results(eq, u2)
    _mark(oic, eq, django_capture_on_commit_callbacks)

    res = _client(oic).get("/api/equipments/maintenance-status/")
    other = _client(_oic_for(_equipment())).get("/api/equipments/maintenance-status/")
    student = _client(u1).get("/api/equipments/maintenance-status/")

    assert res.status_code == 200
    (row,) = res.data["periods"]
    assert row["equipment_id"] == eq.pk
    assert (row["bookings_affected"], row["bookings_on_hold"], row["users_notified"], row["results_paused"]) == (2, 1, 2, 1)
    assert other.data["periods"] == [] and student.data["periods"] == []


# --- templates and schedule ---------------------------------------------------------------------------------------


def test_catalog_has_maintenance_templates_in_the_bookings_category():
    from iic_booking.communication.default_email_templates import get_default_email_templates
    from iic_booking.communication.notification_preferences import BY_KEY, category_for

    codes = {t["code"] for t in get_default_email_templates()}
    for code in maintenance_auto.TEMPLATE_CODES:
        assert code in codes
        assert category_for(code) == "bookings" and BY_KEY["bookings"].locked


def test_template_migration_creates_missing_and_leaves_customised_rows():
    from iic_booking.communication.models import CommunicationTemplate

    mod = importlib.import_module("iic_booking.communication.migrations.0061_equipment_maintenance_email_templates")
    CommunicationTemplate.objects.create(
        code=maintenance_auto.TEMPLATE_DAY, communication_type="email", name="Custom day", subject="Custom",
        body_text="Custom body", body_html="<p>Custom</p>",
    )

    mod.create_missing(apps, None)
    mod.create_missing(apps, None)

    rows = CommunicationTemplate.objects.filter(code__in=maintenance_auto.TEMPLATE_CODES)
    assert rows.count() == 4
    assert rows.get(code=maintenance_auto.TEMPLATE_DAY).subject == "Custom"


def test_schedule_migration_registers_a_single_0830_row():
    from django_celery_beat.models import PeriodicTask

    mod = importlib.import_module("iic_booking.equipment.migrations.0253_maintenance_auto_daily_check_schedule")
    mod.create_schedule(apps, None)
    mod.create_schedule(apps, None)
    PeriodicTask.objects.create(name="dupe", task=mod.TASK, crontab=PeriodicTask.objects.get(task=mod.TASK).crontab)
    mod.create_schedule(apps, None)

    row = PeriodicTask.objects.get(task=mod.TASK)
    assert row.enabled
    assert (row.crontab.minute, row.crontab.hour, str(row.crontab.timezone)) == ("30", "8", "Asia/Kolkata")

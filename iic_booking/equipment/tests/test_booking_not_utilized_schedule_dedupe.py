import importlib

import pytest
from django.apps import apps
from django_celery_beat.models import CrontabSchedule, IntervalSchedule, PeriodicTask, PeriodicTasks

TASK = "equipment.check_booking_not_utilized"

m0062 = importlib.import_module("iic_booking.equipment.migrations.0062_booking_not_utilized_schedule")
m0142 = importlib.import_module("iic_booking.equipment.migrations.0142_booking_not_utilized_celery_schedule")
m0251 = importlib.import_module("iic_booking.equipment.migrations.0251_dedupe_booking_not_utilized_schedule")


def _crontab():
    return CrontabSchedule.objects.get_or_create(
        minute="0", hour="20", day_of_week="*", day_of_month="*", month_of_year="*"
    )[0]


@pytest.mark.django_db
def test_dedupe_keeps_oldest_enabled_row_and_bumps_change_marker():
    cron = _crontab()
    first = PeriodicTask.objects.create(name="Daily booking not utilized check 20:00", task=TASK, crontab=cron)
    PeriodicTask.objects.create(
        name="Auto booking not utilized 20:00 IST (weekdays per task logic)", task=TASK, crontab=cron
    )
    PeriodicTasks.objects.all().delete()

    m0251.dedupe_booking_not_utilized_schedule(apps, None)

    rows = list(PeriodicTask.objects.filter(task=TASK))
    assert [r.pk for r in rows] == [first.pk]
    assert rows[0].enabled and rows[0].crontab_id == cron.pk
    assert PeriodicTasks.objects.filter(ident=1).exists()


@pytest.mark.django_db
def test_dedupe_prefers_enabled_row_and_keeps_rows_on_other_schedules():
    cron = _crontab()
    every = IntervalSchedule.objects.create(every=6, period=IntervalSchedule.HOURS)
    PeriodicTask.objects.create(name="old disabled", task=TASK, crontab=cron, enabled=False)
    live = PeriodicTask.objects.create(name="live", task=TASK, crontab=cron, enabled=True)
    other = PeriodicTask.objects.create(name="other schedule", task=TASK, interval=every, enabled=True)

    m0251.dedupe_booking_not_utilized_schedule(apps, None)

    assert set(PeriodicTask.objects.filter(task=TASK).values_list("pk", flat=True)) == {live.pk, other.pk}


@pytest.mark.django_db
def test_dedupe_is_noop_with_single_row():
    PeriodicTask.objects.create(name="only", task=TASK, crontab=_crontab())
    PeriodicTasks.objects.all().delete()

    m0251.dedupe_booking_not_utilized_schedule(apps, None)

    assert PeriodicTask.objects.filter(task=TASK).count() == 1
    assert not PeriodicTasks.objects.exists()


@pytest.mark.django_db
def test_registration_migrations_create_a_single_row_and_rerun_safely():
    m0062.create_buffer_check_schedule(apps, None)
    m0142.create_booking_not_utilized_schedule(apps, None)
    m0062.create_buffer_check_schedule(apps, None)
    m0142.create_booking_not_utilized_schedule(apps, None)

    rows = PeriodicTask.objects.filter(task=TASK)
    assert rows.count() == 1
    row = rows.get()
    assert row.enabled
    assert (row.crontab.minute, row.crontab.hour) == ("0", "20")

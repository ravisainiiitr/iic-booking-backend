"""
End-to-end check on production of calendar sync:
  1. internal/external booking users get a private subscription link (Google / Outlook / webcal);
  2. the public .ics feed lists their active bookings and drops cancelled ones;
  3. the per-booking .ics download is owner-only;
  4. resetting the link revokes the old one;
  5. staff roles (OIC shown here) are refused and their feed stops working.

Acts as test.student@iic-booking.test and test.faculty@iic-booking.test. Test equipment, slots and
zero-charge bookings are inserted directly (no wallet movement); the faculty account is given the OIC
role temporarily. Everything happens inside one database transaction that is rolled back at the end.
Feed tokens are never printed.

Usage (inside the django container): python - < scripts/ops/e2e_calendar_sync.py
"""

import os
import sys
import uuid
from urllib.parse import urlparse

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")
django.setup()

from datetime import timedelta  # noqa: E402
from decimal import Decimal  # noqa: E402
from unittest.mock import patch  # noqa: E402

from celery.app.task import Task  # noqa: E402
from django.conf import settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.db import transaction  # noqa: E402
from django.test.utils import override_settings  # noqa: E402
from django.utils import timezone  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from iic_booking.equipment.models import (  # noqa: E402
    Booking,
    BookingStatus,
    CalendarFeedToken,
    ChargeProfile,
    DailySlot,
    Equipment,
    EquipmentProfileType,
    EquipmentStatus,
    SlotMaster,
)
from iic_booking.users.models import UserType  # noqa: E402

STUDENT_EMAIL = "test.student@iic-booking.test"
FACULTY_EMAIL = "test.faculty@iic-booking.test"
TAG = "ZZE2C" + uuid.uuid4().hex[:4].upper()
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print("PASS" if ok else "FAIL", "|", name, "|", detail)


class _Rollback(Exception):
    pass


def _host():
    hosts = [h for h in getattr(settings, "ALLOWED_HOSTS", []) if h and h != "*" and not h.startswith(".")]
    return hosts[0] if hosts else "localhost"


def _client(user=None):
    client = APIClient(HTTP_HOST=_host())
    if user is not None:
        client.force_authenticate(user=user)
    return client


def _cover_orphan_columns():
    """
    Production tables can carry NOT NULL columns from other release branches that this code has no
    field for, so plain inserts fail. Give those columns a default for this process only.
    """
    from django.apps import apps
    from django.db import connection, models

    defaults = {
        "BooleanField": (models.BooleanField, False),
        "CharField": (models.CharField, ""),
        "TextField": (models.TextField, ""),
    }
    with connection.cursor() as cursor:
        tables = set(connection.introspection.table_names(cursor))
        for model in apps.get_models():
            table = model._meta.db_table
            if model._meta.proxy or not model._meta.managed or table not in tables:
                continue
            known = {f.column for f in model._meta.concrete_fields}
            for col in connection.introspection.get_table_description(cursor, table):
                if col.name in known or col.null_ok or hasattr(model, col.name):
                    continue
                kind = connection.introspection.get_field_type(col.type_code, col)
                if kind.endswith("IntegerField") or kind in ("DecimalField", "FloatField"):
                    field_cls, default = models.IntegerField, 0
                elif kind in defaults:
                    field_cls, default = defaults[kind]
                else:
                    print("INFO | orphan column without a default:", table, col.name, kind)
                    continue
                kwargs = {"max_length": 255} if field_cls is models.CharField else {}
                field_cls(default=default, **kwargs).contribute_to_class(model, col.name)


def run():
    from iic_booking.equipment.calendar_sync import is_calendar_sync_eligible

    User = get_user_model()
    student = User.objects.filter(email__iexact=STUDENT_EMAIL).first()
    faculty = User.objects.filter(email__iexact=FACULTY_EMAIL).first()
    if student is None or faculty is None:
        check("test accounts found", False, f"student={bool(student)} faculty={bool(faculty)}")
        return
    print("student", student.pk, student.user_type, "| faculty", faculty.pk, faculty.user_type, "| host", _host())
    check("both test accounts are eligible",
          is_calendar_sync_eligible(student) and is_calendar_sync_eligible(faculty),
          f"student={student.user_type} active={student.is_active} faculty={faculty.user_type} active={faculty.is_active}")

    eq = Equipment.objects.create(
        name=f"{TAG} Calendar Test Equipment",
        code=f"{TAG}CAL",
        slot_duration_minutes=60,
        user_rating_enabled=False,
        status=EquipmentStatus.ACTIVE,
        location="IIC test bench, Room 0",
    )
    profile = ChargeProfile.objects.create(
        equipment=eq, user_type=student.user_type, profile_type=EquipmentProfileType.HOUR,
        time_formula="60", primary_unit_charge=Decimal("0.00"),
    )
    base = (timezone.now() + timedelta(days=5)).replace(minute=0, second=0, microsecond=0)
    counter = {"n": 900}

    def booking(status, hour_offsets):
        b = Booking.objects.create(
            user=student, equipment=eq, charge_profile=profile, status=status,
            total_charge=Decimal("0.00"), total_time_minutes=60 * len(hour_offsets),
            virtual_booking_id=f"{TAG}{uuid.uuid4().hex[:4].upper()}", user_type_snapshot=student.user_type,
        )
        for offset in hour_offsets:
            counter["n"] += 1
            start = base + timedelta(hours=offset)
            master = SlotMaster.objects.create(
                equipment=eq, slot_number=counter["n"], open_time=timezone.localtime(start).time(),
                close_time=timezone.localtime(start + timedelta(hours=1)).time(), is_active=True,
            )
            DailySlot.objects.create(
                slot_master=master, date=timezone.localtime(start).date(), start_datetime=start,
                end_datetime=start + timedelta(hours=1), status="BOOKED", booking=b,
            )
        return b

    booked = booking(BookingStatus.BOOKED, [0, 1])
    cancelled = booking(BookingStatus.CANCELLED, [30])
    stu = _client(student)

    res = stu.get("/api/calendar-sync/", secure=True)
    data = getattr(res, "data", {}) or {}
    feed_url = data.get("feed_url") or ""
    parsed = urlparse(feed_url)
    check("student gets a private https subscription link",
          res.status_code == 200 and parsed.scheme == "https" and parsed.path.startswith("/api/calendar/feed/")
          and str(data.get("webcal_url", "")).startswith("webcal://")
          and str(data.get("google_url", "")).startswith("https://calendar.google.com/")
          and str(data.get("outlook_url", "")).startswith("https://outlook.live.com/"),
          f"status={res.status_code} scheme={parsed.scheme} host={parsed.netloc}")

    feed = _client().get(parsed.path, secure=True)
    body = feed.content.decode("utf-8", "replace").replace("\r\n ", "")
    check("public feed returns the student's calendar",
          feed.status_code == 200 and feed["Content-Type"].startswith("text/calendar")
          and body.startswith("BEGIN:VCALENDAR"),
          f"status={feed.status_code} type={feed.get('Content-Type')}")
    check("feed lists the active booking as one merged event",
          f"UID:booking-{booked.booking_id}-1@" in body and f"UID:booking-{booked.booking_id}-2@" not in body
          and "LOCATION:IIC test bench\\, Room 0" in body,
          f"events={body.count('BEGIN:VEVENT')}")
    check("feed leaves out the cancelled booking", f"booking-{cancelled.booking_id}-" not in body)
    check("feed access is recorded",
          CalendarFeedToken.objects.filter(user=student, last_accessed_at__isnull=False).exists())

    one = stu.get(f"/api/bookings/{booked.booking_id}/calendar.ics", secure=True)
    check("owner can download the single booking .ics",
          one.status_code == 200 and "attachment;" in one.get("Content-Disposition", ""),
          f"status={one.status_code}")
    other = _client(faculty).get(f"/api/bookings/{booked.booking_id}/calendar.ics", secure=True)
    check("another user cannot download it", other.status_code == 404, f"status={other.status_code}")

    reset = stu.post("/api/calendar-sync/regenerate/", {}, format="json", secure=True)
    new_path = urlparse((getattr(reset, "data", {}) or {}).get("feed_url") or "").path
    old = _client().get(parsed.path, secure=True)
    new = _client().get(new_path, secure=True) if new_path else None
    check("reset link revokes the old one",
          reset.status_code == 200 and new_path != parsed.path and old.status_code == 404
          and new is not None and new.status_code == 200,
          f"reset={reset.status_code} old={old.status_code} new={getattr(new, 'status_code', None)}")

    fac = _client(faculty)
    fac_res = fac.get("/api/calendar-sync/", secure=True)
    fac_path = urlparse((getattr(fac_res, "data", {}) or {}).get("feed_url") or "").path
    User.objects.filter(pk=faculty.pk).update(user_type=UserType.MANAGER)
    faculty.refresh_from_db()
    fac = _client(faculty)
    refused = fac.get("/api/calendar-sync/", secure=True)
    stale = _client().get(fac_path, secure=True) if fac_path else None
    check("OIC role is refused and its old feed stops working",
          fac_res.status_code == 200 and refused.status_code == 403 and stale is not None and stale.status_code == 404,
          f"before={fac_res.status_code} as_oic={refused.status_code} feed={getattr(stale, 'status_code', None)}")


def main():
    print("TAG", TAG)
    _cover_orphan_columns()
    User = get_user_model()
    test_users = User.objects.filter(email__in=[STUDENT_EMAIL, FACULTY_EMAIL])
    tokens_before = sorted(CalendarFeedToken.objects.filter(user__in=test_users).values_list("pk", "token"))
    with override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"), \
            patch.object(Task, "apply_async", lambda *a, **k: None), \
            patch("iic_booking.communication.service.CommunicationService.send_push_notification",
                  lambda *a, **k: None):
        try:
            with transaction.atomic():
                try:
                    run()
                except Exception as exc:
                    import traceback

                    traceback.print_exc()
                    check("test ran without an unexpected error", False, f"{type(exc).__name__}: {exc}")
                raise _Rollback()
        except _Rollback:
            print("ROLLED BACK: all test data and temporary changes removed")

    tokens_after = sorted(CalendarFeedToken.objects.filter(user__in=test_users).values_list("pk", "token"))
    leftovers = {
        "equipment": Equipment.objects.filter(code__startswith=TAG).count(),
        "bookings": Booking.objects.filter(equipment__code__startswith=TAG).count(),
        "feed_tokens_changed": int(tokens_before != tokens_after),
        "test_accounts_with_staff_role": test_users.filter(user_type__in=[UserType.MANAGER, UserType.ADMIN]).count(),
    }
    check("cleanup: nothing left behind", not any(leftovers.values()), str(leftovers))
    failed = [name for name, ok in RESULTS if not ok]
    print("SUMMARY", len(RESULTS) - len(failed), "passed,", len(failed), "failed")
    sys.exit(1 if failed else 0)


main()

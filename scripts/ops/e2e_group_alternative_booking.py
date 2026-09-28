"""
End-to-end check of "Automatically search and allocate alternate equipment" on production.

Runs as test.student@iic-booking.test against the real booking endpoint. Every row it creates
(test group, two test equipment, slots, bookings, wallet entries, events) lives inside one
database transaction that is rolled back at the end, so nothing is left behind. Emails go to the
in-memory backend and Celery tasks are not queued while the test runs.

Usage (inside the django container): python - < scripts/ops/e2e_group_alternative_booking.py
"""

import os
import sys
import uuid

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")
django.setup()

from datetime import datetime, time, timedelta  # noqa: E402
from decimal import Decimal  # noqa: E402
from unittest.mock import patch  # noqa: E402

from celery.app.task import Task  # noqa: E402
from django.conf import settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.db import transaction  # noqa: E402
from django.test.utils import override_settings  # noqa: E402
from django.utils import timezone  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from iic_booking.equipment import equipment_group_service as egs  # noqa: E402
from iic_booking.equipment.api_views import _get_charge_profile_pricing_profile_for_user  # noqa: E402
from iic_booking.equipment.models import (  # noqa: E402
    Booking,
    BookingEvent,
    ChargeProfile,
    DailySlot,
    Equipment,
    EquipmentGroup,
    EquipmentProfileType,
    EquipmentStatus,
    SlotMaster,
)

STUDENT_EMAIL = "test.student@iic-booking.test"
TAG = "ZZE2E" + uuid.uuid4().hex[:4].upper()
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print("PASS" if ok else "FAIL", "|", name, "|", detail)


class _Rollback(Exception):
    pass


def _host():
    hosts = [h for h in getattr(settings, "ALLOWED_HOSTS", []) if h and h != "*" and not h.startswith(".")]
    return hosts[0] if hosts else "localhost"


def _client(user):
    client = APIClient(HTTP_HOST=_host())
    client.force_authenticate(user=user)
    return client


def _post(client, path, body):
    res = client.post(path, body, format="json", secure=True)
    data = getattr(res, "data", None)
    if data is None:
        try:
            data = res.json()
        except Exception:
            data = {"raw": res.content[:300]}
    return res.status_code, data


def _booking_id(data):
    for key in ("real_booking_id", "id"):
        if isinstance(data.get(key), int):
            return data[key]
    return None


def run():
    User = get_user_model()
    student = User.objects.filter(email__iexact=STUDENT_EMAIL).first()
    if student is None:
        print("FAIL | test student not found; stopping")
        return
    ultra = Equipment.objects.filter(pk=2).select_related("internal_department").first()
    department = getattr(ultra, "internal_department", None)
    user_type = student.user_type
    print("student", student.pk, "user_type", user_type, "department for test equipment", getattr(department, "pk", None))
    client = _client(student)

    group = EquipmentGroup.objects.create(name=f"{TAG} Test Group", alternative_booking_enabled=True)

    def make_equipment(suffix, priority):
        eq = Equipment.objects.create(
            name=f"{TAG} Test Equipment {suffix}",
            code=f"{TAG}{suffix}",
            slot_duration_minutes=60,
            user_rating_enabled=False,
            internal_department=department,
            status=EquipmentStatus.ACTIVE,
            equipment_group=group,
            alternative_priority=priority,
        )
        ChargeProfile.objects.create(
            equipment=eq,
            user_type=user_type,
            profile_type=EquipmentProfileType.HOUR,
            time_formula="60",
            primary_unit_charge=Decimal("1.00"),
            pricing_profile=_get_charge_profile_pricing_profile_for_user(student, eq),
        )
        return eq

    eq_a = make_equipment("A", 10)
    eq_b = make_equipment("B", 20)
    print("created", eq_a.code, eq_a.pk, eq_b.code, eq_b.pk, "group", group.pk)
    for eq in (eq_a, eq_b):
        reason = egs.equipment_eligibility_error(student, eq, user_type=user_type)
        check(f"test student may book {eq.code}", reason is None, reason or "")

    counter = {"n": 900}

    def make_slot(eq, start, status="AVAILABLE"):
        counter["n"] += 1
        end = start + timedelta(minutes=60)
        master = SlotMaster.objects.create(
            equipment=eq,
            slot_number=counter["n"],
            open_time=timezone.localtime(start).time().replace(microsecond=0),
            close_time=timezone.localtime(end).time().replace(microsecond=0),
            is_active=True,
        )
        return DailySlot.objects.create(
            slot_master=master, date=timezone.localtime(start).date(),
            start_datetime=start, end_datetime=end, status=status,
        )

    def bookable(eq, start):
        probe = make_slot(eq, start)
        ok = egs.slot_passes_booking_rules(eq, probe, booking_user=student, actor=student,
                                           user_type=user_type, is_admin=False)
        master = probe.slot_master
        probe.delete()
        master.delete()
        return ok

    lo, hi = egs.slot_window_bounds(eq_b, user_type, False)
    tz = timezone.get_current_timezone()
    starts = []
    day = max(lo, timezone.localdate() + timedelta(days=1))
    while day <= hi and len(starts) < 3:
        for hour in (10, 12, 14):
            start = timezone.make_aware(datetime.combine(day, time(hour, 0)), tz)
            if start > timezone.now() and bookable(eq_b, start) and bookable(eq_a, start):
                starts.append(start)
                if len(starts) == 3:
                    break
        day += timedelta(days=1)
    print("slot window", lo, hi, "test times", [s.isoformat() for s in starts])
    if len(starts) < 3:
        check("found three bookable test times in the slot window", False, str(starts))
        return
    t1, t2, t3 = starts

    status_code, data = _get_detail(client, eq_a)
    check("equipment detail offers the option (group_alternatives_enabled)",
          status_code == 200 and data.get("group_alternatives_enabled") is True, f"status={status_code}")

    base = {"input_values": {}, "status": "pending", "waitlist_on_failure": False, "offer_group_alternatives": True}

    # 1. Ticked: the selected slot on A is taken, B is free at the same time -> booked on B automatically.
    a1 = make_slot(eq_a, t1, status="BOOKED")
    make_slot(eq_b, t1)
    code, data = _post(client, f"/api/equipments/{eq_a.pk}/book/",
                       {**base, "slot_ids": [a1.id], "auto_allocate_alternative": True})
    allocated = (data or {}).get("allocated_alternative") or {}
    bid = _booking_id(data or {})
    booking = Booking.objects.filter(pk=bid).first() if bid else None
    check("ticked: booking allocated automatically to the alternate equipment",
          200 <= code < 300 and (allocated.get("equipment") or {}).get("equipment_id") == eq_b.pk
          and booking is not None and booking.equipment_id == eq_b.pk,
          f"status={code} booking={bid} error={(data or {}).get('error')}")
    if booking is not None:
        slots = list(booking.daily_slots.order_by("start_datetime"))
        check("ticked: booked slot is on B at the requested time",
              bool(slots) and all(s.slot_master.equipment_id == eq_b.pk for s in slots) and slots[0].start_datetime == t1,
              f"slots={[s.id for s in slots]}")
        event = BookingEvent.objects.filter(booking=booking).order_by("id").first()
        meta = getattr(event, "metadata", None) or {}
        check("ticked: audit records auto allocation from A",
              meta.get("auto_allocated") is True and meta.get("alternative_of_equipment_id") == eq_a.pk, str(meta)[:200])
        print("   booking", booking.pk, booking.virtual_booking_id, "charge", booking.total_charge, "status", booking.status)

    # 2. Unticked: same situation -> alternatives are offered, nothing is booked until the user confirms.
    a2 = make_slot(eq_a, t2, status="BOOKED")
    b2 = make_slot(eq_b, t2)
    before = Booking.objects.filter(user=student).count()
    code, data = _post(client, f"/api/equipments/{eq_a.pk}/book/",
                       {**base, "slot_ids": [a2.id], "auto_allocate_alternative": False})
    alternatives = (data or {}).get("alternatives") or []
    b2.refresh_from_db()
    check("unticked: user is asked to confirm (409 with the alternate equipment)",
          code == 409 and (data or {}).get("code") == egs.ALTERNATIVES_AVAILABLE_CODE
          and bool(alternatives) and alternatives[0]["equipment_id"] == eq_b.pk,
          f"status={code} alternatives={[a.get('equipment_id') for a in alternatives]}")
    check("unticked: nothing booked before confirmation",
          Booking.objects.filter(user=student).count() == before and b2.status == "AVAILABLE" and b2.booking_id is None)
    if alternatives:
        alt = alternatives[0]
        code, data = _post(client, f"/api/equipments/{eq_b.pk}/book/", {
            "slot_ids": alt["slot_ids"], "input_values": alt["input_values"], "status": "pending",
            "waitlist_on_failure": False, "alternative_of_equipment_id": eq_a.pk,
        })
        bid = _booking_id(data or {})
        booking = Booking.objects.filter(pk=bid).first() if bid else None
        check("unticked: after confirmation the booking is made on B",
              200 <= code < 300 and booking is not None and booking.equipment_id == eq_b.pk,
              f"status={code} booking={bid} error={(data or {}).get('error')}")
        if booking is not None:
            event = BookingEvent.objects.filter(booking=booking).order_by("id").first()
            meta = getattr(event, "metadata", None) or {}
            check("unticked: audit records a confirmed (not automatic) alternative",
                  meta.get("auto_allocated") is False and meta.get("alternative_of_equipment_id") == eq_a.pk,
                  str(meta)[:200])

    # 3. No free slot on A this week (no slot selected): ticked -> earliest free slot on B is booked.
    make_slot(eq_b, t3)
    no_slot = {"input_values": {}, "status": "pending", "waitlist_on_failure": False,
               "request_waitlist_without_slot_selection": True, "offer_group_alternatives": True}
    code, data = _post(client, f"/api/equipments/{eq_a.pk}/book/", {**no_slot, "auto_allocate_alternative": False})
    check("no slot selected, unticked: alternate equipment offered for confirmation",
          code == 409 and ((data or {}).get("alternatives") or [{}])[0].get("equipment_id") == eq_b.pk,
          f"status={code} error={(data or {}).get('error')}")
    code, data = _post(client, f"/api/equipments/{eq_a.pk}/book/", {**no_slot, "auto_allocate_alternative": True})
    bid = _booking_id(data or {})
    booking = Booking.objects.filter(pk=bid).first() if bid else None
    check("no slot selected, ticked: earliest free slot on B booked automatically",
          200 <= code < 300 and booking is not None and booking.equipment_id == eq_b.pk,
          f"status={code} booking={bid} error={(data or {}).get('error')}")

    # 4. No other equipment available -> unchanged failure, no booking, not waitlisted.
    Equipment.objects.filter(pk=eq_b.pk).update(status=EquipmentStatus.MAINTENANCE)
    a4 = make_slot(eq_a, t1 + timedelta(hours=1), status="BOOKED")
    before = Booking.objects.filter(user=student).count()
    code, data = _post(client, f"/api/equipments/{eq_a.pk}/book/",
                       {**base, "slot_ids": [a4.id], "auto_allocate_alternative": True})
    check("no alternate equipment available: booking fails as before, nothing booked or waitlisted",
          code == 400 and Booking.objects.filter(user=student).count() == before
          and "waitlist_position" not in (data or {}), f"status={code} error={(data or {}).get('error')}")
    Equipment.objects.filter(pk=eq_b.pk).update(status=EquipmentStatus.ACTIVE)

    # 5. Group switch off -> option hidden and no alternatives.
    EquipmentGroup.objects.filter(pk=group.pk).update(alternative_booking_enabled=False)
    status_code, data = _get_detail(client, eq_a)
    check("group switch off: option not offered", data.get("group_alternatives_enabled") is False, f"status={status_code}")
    code, data = _post(client, f"/api/equipments/{eq_a.pk}/book/",
                       {**base, "slot_ids": [a4.id], "auto_allocate_alternative": True})
    check("group switch off: no alternatives, original failure", code == 400 and "alternatives" not in (data or {}),
          f"status={code}")


def _get_detail(client, eq):
    res = client.get(f"/api/equipments/{eq.pk}/", secure=True)
    data = getattr(res, "data", None) or {}
    return res.status_code, data


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
                print("INFO | orphan NOT NULL column (not in code):", table, col.name, kind, "-> default", repr(default))


def main():
    print("TAG", TAG, "alternative flag", settings.EQUIPMENT_GROUP_ALTERNATIVE_BOOKING_ENABLED)
    _cover_orphan_columns()
    with override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"), \
            patch.object(Task, "apply_async", lambda *a, **k: None):
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
            print("ROLLED BACK: all test data removed")

    leftovers = {
        "equipment": Equipment.objects.filter(code__startswith=TAG).count(),
        "groups": EquipmentGroup.objects.filter(name__startswith=TAG).count(),
        "bookings": Booking.objects.filter(equipment__code__startswith=TAG).count(),
    }
    check("cleanup: no test equipment, group or booking left", not any(leftovers.values()), str(leftovers))
    failed = [name for name, ok in RESULTS if not ok]
    print("SUMMARY", len(RESULTS) - len(failed), "passed,", len(failed), "failed")
    sys.exit(1 if failed else 0)


main()

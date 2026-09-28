"""
End-to-end check on production of:
  1. waitlisting when no alternate equipment in the group is free (and when the group switch is off),
     subject to the waitlist queue depth;
  2. first-come-first-serve waitlist allocation when a booking is cancelled and when the Officer in
     charge makes more slots available;
  3. urgent booking requests: Type A rush relief (advance-week booking and held slots) and Type B
     (50% surcharge; supervisor then OIC approval, and OIC rejection).

Acts as test.student@iic-booking.test and test.faculty@iic-booking.test against the real endpoints.
Officer-in-charge steps are done by one test account temporarily given the admin role. Every row it
creates or changes (test equipment, slots, bookings, waitlist entries, urgent requests, wallet
entries, the temporary role) lives inside one database transaction that is rolled back at the end.
Emails go to the in-memory backend; push notifications and Celery tasks are not sent.

Usage (inside the django container): python - < scripts/ops/e2e_waitlist_urgent.py
"""

import os
import sys
import uuid
from contextlib import contextmanager

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
from django.test import TestCase  # noqa: E402
from django.test.utils import override_settings  # noqa: E402
from django.utils import timezone  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from iic_booking.equipment import equipment_group_service as egs  # noqa: E402
from iic_booking.equipment.api_views import (  # noqa: E402
    _get_charge_profile_pricing_profile_for_user,
    get_equipment_slot_window_reference_config,
    get_internal_slot_window_date_bounds,
)
from iic_booking.equipment.models import (  # noqa: E402
    Booking,
    ChargeProfile,
    DailySlot,
    Equipment,
    EquipmentGroup,
    EquipmentProfileType,
    EquipmentStatus,
    SlotMaster,
    UrgentBookingRequest,
    WaitlistEntry,
)
from iic_booking.equipment.waitlist import active_waitlist_position  # noqa: E402
from iic_booking.users.models import UserType  # noqa: E402
from iic_booking.users.repositories.wallet_repository import WalletRepository  # noqa: E402

STUDENT_EMAIL = "test.student@iic-booking.test"
FACULTY_EMAIL = "test.faculty@iic-booking.test"
TAG = "ZZE2W" + uuid.uuid4().hex[:4].upper()
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print("PASS" if ok else "FAIL", "|", name, "|", detail)


def skip(name, detail=""):
    print("SKIP", "|", name, "|", detail)


class _Rollback(Exception):
    pass


def _host():
    hosts = [h for h in getattr(settings, "ALLOWED_HOSTS", []) if h and h != "*" and not h.startswith(".")]
    return hosts[0] if hosts else "localhost"


def _client(user):
    client = APIClient(HTTP_HOST=_host())
    client.force_authenticate(user=user)
    return client


def _call(client, method, path, body=None):
    if method == "get":
        res = client.get(path, secure=True)
    else:
        res = getattr(client, method)(path, body or {}, format="json", secure=True)
    data = getattr(res, "data", None)
    if data is None:
        try:
            data = res.json()
        except Exception:
            data = {"raw": res.content[:300]}
    if not isinstance(data, dict):
        data = {"data": data}
    return res.status_code, data


def _booking(data):
    for key in ("real_booking_id", "id", "booking_id"):
        if isinstance(data.get(key), int):
            return Booking.objects.filter(pk=data[key]).first()
    return None


@contextmanager
def as_admin(user):
    User = get_user_model()
    original = user.user_type
    User.objects.filter(pk=user.pk).update(user_type=UserType.ADMIN)
    user.user_type = UserType.ADMIN
    try:
        yield
    finally:
        User.objects.filter(pk=user.pk).update(user_type=original)
        user.user_type = original


def balance(user, eq):
    target, _ = WalletRepository.get_booking_wallet_target(user, eq.internal_department)
    if target is None:
        return None
    target.refresh_from_db()
    return Decimal(str(target.balance))


def debit(before, after):
    if before is None or after is None:
        return None
    return before - after


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
    User = get_user_model()
    student = User.objects.filter(email__iexact=STUDENT_EMAIL).first()
    faculty = User.objects.filter(email__iexact=FACULTY_EMAIL).first()
    if student is None or faculty is None:
        check("test accounts found", False, f"student={bool(student)} faculty={bool(faculty)}")
        return
    ultra = Equipment.objects.filter(pk=2).select_related("internal_department").first()
    department = getattr(ultra, "internal_department", None)
    print("student", student.pk, student.user_type, "| faculty", faculty.pk, faculty.user_type,
          "| department", getattr(department, "pk", None))
    stu, fac = _client(student), _client(faculty)
    tz = timezone.get_current_timezone()
    counter = {"n": 900}

    def aware(day, hour):
        return timezone.make_aware(datetime.combine(day, time(hour, 0)), tz)

    def make_equipment(suffix, group=None, priority=10, **fields):
        eq = Equipment.objects.create(
            name=f"{TAG} Test Equipment {suffix}",
            code=f"{TAG}{suffix}",
            slot_duration_minutes=60,
            user_rating_enabled=False,
            internal_department=department,
            status=EquipmentStatus.ACTIVE,
            equipment_group=group,
            alternative_priority=priority,
            reschedule_hours_threshold=2,
            urgent_peak_window_minutes=None,
            **fields,
        )
        for user in {student.user_type: student, faculty.user_type: faculty}.values():
            ChargeProfile.objects.create(
                equipment=eq,
                user_type=user.user_type,
                profile_type=EquipmentProfileType.HOUR,
                time_formula="60",
                primary_unit_charge=Decimal("1.00"),
                pricing_profile=_get_charge_profile_pricing_profile_for_user(user, eq),
            )
        return eq

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

    def bookable(eq, start, user):
        probe = make_slot(eq, start)
        ok = egs.slot_passes_booking_rules(eq, probe, booking_user=user, actor=user,
                                           user_type=user.user_type, is_admin=False)
        master = probe.slot_master
        probe.delete()
        master.delete()
        return ok

    def urgent_day(eq):
        """First day after the window that the urgent-request 'no slots available' check looks at."""
        today = timezone.localdate()
        week_start = today - timedelta(days=today.weekday())
        search_end = week_start + timedelta(days=6)
        rw, rt = get_equipment_slot_window_reference_config(eq)
        if rw is not None and rt is not None:
            _lo, _hi, before = get_internal_slot_window_date_bounds(eq, timezone.now())
            if not before:
                search_end = week_start + timedelta(days=13)
        return search_end + timedelta(days=2)

    def book(eq):
        return f"/api/equipments/{eq.pk}/book/"

    base = {"input_values": {}, "status": "pending"}

    # ------------------------------------------------------------------ 1. waitlist
    group = EquipmentGroup.objects.create(name=f"{TAG} Test Group", alternative_booking_enabled=True)
    eq_a = make_equipment("A", group, 10, waitlist_queue_depth=1)
    eq_b = make_equipment("B", group, 20)
    Equipment.objects.filter(pk=eq_b.pk).update(status=EquipmentStatus.MAINTENANCE)
    eq_a.refresh_from_db()
    for user in (student, faculty):
        reason = egs.equipment_eligibility_error(user, eq_a, user_type=user.user_type)
        check(f"{user.user_type} may book the test equipment", reason is None, reason or "")

    lo, hi = egs.slot_window_bounds(eq_a, student.user_type, False)
    starts = []
    day = max(lo, timezone.localdate() + timedelta(days=1))
    while day <= hi and len(starts) < 4:
        for hour in (10, 12, 14, 16):
            start = aware(day, hour)
            if start > timezone.now() + timedelta(hours=3) and bookable(eq_a, start, student) \
                    and bookable(eq_a, start, faculty):
                starts.append(start)
                if len(starts) == 4:
                    break
        day += timedelta(days=1)
    print("slot window", lo, hi, "test times", [s.isoformat() for s in starts])
    if len(starts) < 4:
        check("found four bookable test times in the slot window", False, str(starts))
        return
    t1, t2, t3, t4 = starts

    f1 = make_slot(eq_a, t1)
    code, data = _call(fac, "post", book(eq_a), {**base, "slot_ids": [f1.id], "waitlist_on_failure": False})
    faculty_booking = _booking(data)
    check("setup: faculty books a slot on A (cancelled later)", 200 <= code < 300 and faculty_booking is not None,
          f"status={code} error={data.get('error')}")

    taken = make_slot(eq_a, t2, status="BOOKED")
    before = Booking.objects.filter(user=student).count()
    code, data = _call(stu, "post", book(eq_a), {
        **base, "slot_ids": [taken.id], "waitlist_on_failure": True,
        "offer_group_alternatives": True, "auto_allocate_alternative": True,
    })
    entry = WaitlistEntry.objects.filter(equipment=eq_a, user=student, status="ACTIVE").first()
    check("no alternate equipment free: no booking made, student waitlisted at WL1",
          code == 400 and data.get("waitlist_position") == 1 and entry is not None
          and Booking.objects.filter(user=student).count() == before
          and "alternatives" not in data and "allocated_alternative" not in data,
          f"status={code} error={data.get('error')}")
    check("waitlist reference shown to the user",
          data.get("waitlist_code") == "WL1" and str(data.get("virtual_booking_id") or "").endswith("W"),
          f"{data.get('waitlist_code')} {data.get('virtual_booking_id')}")

    code, data = _call(fac, "post", book(eq_a), {
        **base, "slot_ids": [taken.id], "waitlist_on_failure": True,
        "offer_group_alternatives": True, "auto_allocate_alternative": False,
    })
    check("queue depth (1) reached: faculty not waitlisted, told the queue is full",
          code == 400 and data.get("waitlist_full") is True
          and not WaitlistEntry.objects.filter(equipment=eq_a, user=faculty).exists(),
          f"status={code} error={data.get('error')}")

    Equipment.objects.filter(pk=eq_a.pk).update(waitlist_queue_depth=2)
    EquipmentGroup.objects.filter(pk=group.pk).update(alternative_booking_enabled=False)
    code, data = _call(fac, "post", book(eq_a), {**base, "slot_ids": [taken.id]})
    check("group switch off, room in queue (depth 2): faculty waitlisted at WL2",
          code == 400 and data.get("waitlist_position") == 2, f"status={code} error={data.get('error')}")

    if faculty_booking is not None:
        with TestCase.captureOnCommitCallbacks(execute=False) as callbacks:
            code, data = _call(fac, "post", f"/api/bookings/{faculty_booking.pk}/user-cancel/",
                               {"refund": True, "notes": "E2E waitlist check"})
        waitlist_runs = [cb for cb in callbacks if "waitlist" in getattr(cb, "__qualname__", "")]
        for cb in waitlist_runs:
            cb()
        f1.refresh_from_db()
        got = Booking.objects.filter(pk=f1.booking_id).first() if f1.booking_id else None
        check("cancelled slot allocated FCFS to the first in queue (student)",
              code == 200 and got is not None and got.user_id == student.pk and got.equipment_id == eq_a.pk,
              f"cancel={code} error={data.get('error')} fcfs_runs={len(waitlist_runs)} slot={f1.status}")
        check("student leaves the queue once allocated",
              not WaitlistEntry.objects.filter(equipment=eq_a, user=student, status="ACTIVE").exists())
    else:
        skip("FCFS after cancellation", "faculty booking could not be made")
    faculty_entry = WaitlistEntry.objects.filter(equipment=eq_a, user=faculty, status="ACTIVE").first()
    check("faculty still waiting and moves up to WL1",
          faculty_entry is not None and active_waitlist_position(faculty_entry) == 1)

    if faculty_entry is not None:
        code, data = _call(fac, "post", f"/api/waitlist/{faculty_entry.pk}/cancel/", {})
        check("faculty can leave the queue", code == 200 and data.get("status") == "OPT_OUT", f"status={code}")
    code, data = _call(stu, "post", book(eq_a), {**base, "slot_ids": [taken.id], "waitlist_on_failure": True})
    check("student joins the queue again at WL1", code == 400 and data.get("waitlist_position") == 1,
          f"status={code} error={data.get('error')}")
    opened = make_slot(eq_a, t3, status="BLOCKED")
    with as_admin(faculty):
        code, data = _call(fac, "post", f"/api/admin/equipment/{eq_a.pk}/bulk-slot-status/",
                           {"slot_ids": [opened.id], "status": "AVAILABLE"})
    opened.refresh_from_db()
    got = Booking.objects.filter(pk=opened.booking_id).first() if opened.booking_id else None
    check("OIC makes a new slot available: allocated FCFS to the waiting student",
          code == 200 and got is not None and got.user_id == student.pk,
          f"status={code} slot={opened.status} error={data.get('error')}")
    check("queue empty after allocation", not WaitlistEntry.objects.filter(equipment=eq_a, status="ACTIVE").exists())

    EquipmentGroup.objects.filter(pk=group.pk).update(alternative_booking_enabled=True)
    Equipment.objects.filter(pk=eq_b.pk).update(status=EquipmentStatus.ACTIVE)
    b4 = make_slot(eq_b, t4)
    a4 = make_slot(eq_a, t4, status="BOOKED")
    code, data = _call(stu, "post", book(eq_a), {
        **base, "slot_ids": [a4.id], "rush_relief": True, "waitlist_on_failure": False,
        "offer_group_alternatives": True, "auto_allocate_alternative": True,
    })
    b4.refresh_from_db()
    check("Type A rush-relief booking is not moved to other equipment of the group",
          code == 400 and "allocated_alternative" not in data and "alternatives" not in data
          and b4.status == "AVAILABLE", f"status={code} error={data.get('error')}")

    # ------------------------------------------------------------------ 2. urgent requests
    create = "/api/urgent-booking-requests/create/"

    def fail_twice(client, eq):
        blocked = make_slot(eq, t1, status="BOOKED")
        return [
            _call(client, "post", book(eq), {**base, "slot_ids": [blocked.id], "waitlist_on_failure": False})[0]
            for _ in range(2)
        ]

    def attempts(client, eq):
        _code, body = _call(client, "get", f"/api/booking-attempt-logs/my-unsuccessful/?equipment_id={eq.pk}")
        return body.get("peak_qualified_attempts")

    def urgent_slot(eq, hour=10):
        return make_slot(eq, aware(urgent_day(eq), hour))

    # Type A: advance-week booking at the normal rate
    eq_c = make_equipment("C")
    code, data = _call(stu, "post", create, {"equipment_id": eq_c.pk, "request_type": "NO_SLOT",
                                             "disclaimer_accepted": True})
    check("Type A: refused before two unsuccessful peak-window attempts",
          code == 400 and data.get("code") == "RUSH_RELIEF_NOT_QUALIFIED", f"status={code} error={data.get('error')}")
    codes = fail_twice(stu, eq_c)
    n = attempts(stu, eq_c)
    check("Type A: two unsuccessful attempts make the student eligible", codes == [400, 400] and (n or 0) >= 2,
          f"codes={codes} attempts={n}")
    uc = urgent_slot(eq_c)
    bal0 = balance(student, eq_c)
    code, data = _call(stu, "post", book(eq_c), {
        **base, "slot_ids": [uc.id], "rush_relief": True, "waitlist_on_failure": False,
        "offer_group_alternatives": True,
    })
    bk = _booking(data)
    paid = debit(bal0, balance(student, eq_c))
    check("Type A rush relief: advance-week slot booked at the normal rate (no surcharge)",
          200 <= code < 300 and bk is not None and bk.status == "BOOKED"
          and bk.total_charge == Decimal("1.00") and paid == Decimal("1.00"),
          f"status={code} charge={getattr(bk, 'total_charge', None)} debited={paid} error={data.get('error')}")
    used = UrgentBookingRequest.objects.filter(user=student, equipment=eq_c, request_type="NO_SLOT",
                                               status="APPROVED").first()
    check("Type A rush relief: usage recorded and the attempt window reset",
          used is not None and bk is not None and used.hold_booking_id == bk.pk and attempts(stu, eq_c) == 0,
          f"recorded={used is not None} attempts_now={attempts(stu, eq_c)}")

    # Type A: held slots auto-confirmed by the request
    eq_d = make_equipment("D")
    fail_twice(stu, eq_d)
    ud = urgent_slot(eq_d)
    bal0 = balance(student, eq_d)
    code, data = _call(stu, "post", book(eq_d), {**base, "slot_ids": [ud.id], "create_as_hold": True})
    hold = _booking(data)
    check("Type A with held slots: hold created, wallet not charged",
          200 <= code < 300 and hold is not None and hold.status == "HOLD" and debit(bal0, balance(student, eq_d)) == 0,
          f"status={code} charge={getattr(hold, 'total_charge', None)} error={data.get('error')}")
    if hold is not None:
        code, data = _call(stu, "post", create, {"equipment_id": eq_d.pk, "request_type": "NO_SLOT",
                                                 "disclaimer_accepted": True, "hold_booking_id": hold.pk,
                                                 "slots_requested": 1})
        hold.refresh_from_db()
        paid = debit(bal0, balance(student, eq_d))
        check("Type A with held slots: auto-approved, surcharge removed, charged at the normal rate",
              code == 201 and data.get("auto_approved") is True and hold.status == "BOOKED"
              and hold.total_charge == Decimal("1.00") and paid == Decimal("1.00"),
              f"status={code} charge={hold.total_charge} debited={paid} error={data.get('error')}")

    # Type B: student, supervisor approval then OIC approval
    eq_e = make_equipment("E")
    code, data = _call(stu, "post", create, {"equipment_id": eq_e.pk, "request_type": "REVIEWER_URGENT",
                                             "disclaimer_accepted": True, "reviewer_comment": "short"})
    check("Type B: a reason of at least 10 characters is required", code == 400, f"status={code} error={data.get('error')}")
    ue = urgent_slot(eq_e)
    bal0 = balance(student, eq_e)
    code, data = _call(stu, "post", book(eq_e), {**base, "slot_ids": [ue.id], "create_as_hold": True})
    hold = _booking(data)
    check("Type B: slots held at 50% surcharge, wallet not charged yet",
          200 <= code < 300 and hold is not None and hold.status == "HOLD"
          and hold.total_charge == Decimal("1.50") and debit(bal0, balance(student, eq_e)) == 0,
          f"status={code} charge={getattr(hold, 'total_charge', None)} error={data.get('error')}")
    urg = None
    if hold is not None:
        code, data = _call(stu, "post", create, {
            "equipment_id": eq_e.pk, "request_type": "REVIEWER_URGENT", "disclaimer_accepted": True,
            "reviewer_comment": "E2E test: reviewer asked for an urgent repeat measurement.",
            "hold_booking_id": hold.pk, "slots_requested": 1,
        })
        urg = UrgentBookingRequest.objects.filter(pk=data.get("id")).select_related("supervisor").first()
        check("Type B: request submitted, pending review, slots not yet booked",
              code == 201 and urg is not None and urg.status == "PENDING" and hold.status == "HOLD",
              f"status={code} error={data.get('error')}")
    if urg is not None:
        sup = urg.supervisor
        print("INFO | student's supervisor for Type B:",
              "none" if sup is None else ("test faculty" if sup.pk == faculty.pk else "another (non-test) account"))
        if urg.pending_supervisor_approval:
            with as_admin(faculty):
                code, data = _call(fac, "patch", f"/api/urgent-booking-requests/{urg.pk}/", {"status": "APPROVED"})
            check("Type B: OIC cannot approve before the supervisor",
                  code == 400 and data.get("code") == "SUPERVISOR_APPROVAL_PENDING", f"status={code}")
            if sup is not None and sup.pk == faculty.pk:
                code, data = _call(fac, "post", f"/api/urgent-booking-requests/{urg.pk}/wallet-approve/",
                                   {"action": "APPROVE", "wallet_notes": "E2E"})
                check("Type B: supervisor (test faculty) approves and forwards to the OIC",
                      code == 200 and data.get("supervisor_decision") == "APPROVED", f"status={code} error={data.get('error')}")
            else:
                skip("Type B supervisor approval", "the student's supervisor is not a test account")
        urg.refresh_from_db()
        if not urg.pending_supervisor_approval:
            with as_admin(faculty):
                code, data = _call(fac, "patch", f"/api/urgent-booking-requests/{urg.pk}/",
                                   {"status": "APPROVED", "admin_notes": "E2E approval"})
            urg.refresh_from_db()
            hold.refresh_from_db()
            paid = debit(bal0, balance(student, eq_e))
            check("Type B: OIC final approval books the slot and charges the wallet with the surcharge",
                  code == 200 and urg.status == "APPROVED" and hold.status == "BOOKED" and paid == Decimal("1.50"),
                  f"status={code} booking={hold.status} debited={paid} error={data.get('error')}")
        else:
            skip("Type B OIC approval", "waiting for a supervisor who is not a test account")

    # Type B: faculty (no supervisor), rejected by the OIC
    eq_f = make_equipment("F")
    uf = urgent_slot(eq_f)
    fbal0 = balance(faculty, eq_f)
    code, data = _call(fac, "post", book(eq_f), {**base, "slot_ids": [uf.id], "create_as_hold": True})
    fhold = _booking(data)
    check("Type B by faculty: slots held", 200 <= code < 300 and fhold is not None and fhold.status == "HOLD",
          f"status={code} error={data.get('error')}")
    if fhold is not None:
        code, data = _call(fac, "post", create, {
            "equipment_id": eq_f.pk, "request_type": "REVIEWER_URGENT", "disclaimer_accepted": True,
            "reviewer_comment": "E2E test: faculty urgent request to be rejected.",
            "hold_booking_id": fhold.pk, "slots_requested": 1,
        })
        furg = UrgentBookingRequest.objects.filter(pk=data.get("id")).first()
        check("Type B by faculty: goes straight to the OIC (no supervisor step)",
              code == 201 and furg is not None and not furg.pending_supervisor_approval,
              f"status={code} error={data.get('error')}")
        if furg is not None:
            with as_admin(student):
                code, data = _call(stu, "patch", f"/api/urgent-booking-requests/{furg.pk}/",
                                   {"status": "REJECTED", "admin_notes": "E2E rejection"})
            fhold.refresh_from_db()
            uf.refresh_from_db()
            check("Type B rejected by OIC: hold released, slot free again, wallet not charged",
                  code == 200 and fhold.status == "CANCELLED" and uf.status == "AVAILABLE" and uf.booking_id is None
                  and debit(fbal0, balance(faculty, eq_f)) == 0,
                  f"status={code} booking={fhold.status} slot={uf.status} error={data.get('error')}")


def main():
    print("TAG", TAG)
    _cover_orphan_columns()
    bypass = frozenset({STUDENT_EMAIL, FACULTY_EMAIL})
    with override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"), \
            patch.object(Task, "apply_async", lambda *a, **k: None), \
            patch("iic_booking.users.legacy_ledger.booking_lock.BOOKING_LOCK_BYPASS_EMAILS", bypass), \
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

    User = get_user_model()
    leftovers = {
        "equipment": Equipment.objects.filter(code__startswith=TAG).count(),
        "groups": EquipmentGroup.objects.filter(name__startswith=TAG).count(),
        "bookings": Booking.objects.filter(equipment__code__startswith=TAG).count(),
        "waitlist": WaitlistEntry.objects.filter(equipment__code__startswith=TAG).count(),
        "urgent_requests": UrgentBookingRequest.objects.filter(equipment__code__startswith=TAG).count(),
        "test_accounts_with_admin_role": User.objects.filter(
            email__in=[STUDENT_EMAIL, FACULTY_EMAIL], user_type=UserType.ADMIN
        ).count(),
    }
    check("cleanup: nothing left behind", not any(leftovers.values()), str(leftovers))
    failed = [name for name, ok in RESULTS if not ok]
    print("SUMMARY", len(RESULTS) - len(failed), "passed,", len(failed), "failed")
    sys.exit(1 if failed else 0)


main()

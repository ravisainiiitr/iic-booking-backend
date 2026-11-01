"""
End-to-end check of Slot Exchange on production, through the real endpoints:
  1. a booked slot on an exchange-enabled instrument shows the booker and the exchange choice to another
     internal user (GET /api/slot-exchanges/options/);
  2. that user proposes a one-for-one exchange of slots of the same charge (POST /api/slot-exchanges/);
  3. the other user sees it as incoming and accepts it: the slots change hands, no money moves, both bookings
     get an event;
  4. a second proposal is declined, a third withdrawn;
  5. refusals: charge mismatch, instrument with Slot Exchange off, and an external user.

Acts as test.student@iic-booking.test and test.faculty@iic-booking.test on a temporary instrument in the IIC
department. Every row it creates or changes lives inside one database transaction that is rolled back at the end.
Nothing leaves the server: email, SMS and push sending are switched off for the run, Slot Exchange notifications are
only recorded (and checked), and nothing is queued. Read-only summary at the end: how many IIC instruments
have Slot Exchange on.

Usage (inside the django container): python - < scripts/ops/e2e_slot_exchange.py
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

from iic_booking.equipment.models import (  # noqa: E402
    Booking,
    BookingStatus,
    ChargeProfile,
    DailySlot,
    Equipment,
    EquipmentProfileType,
    SlotMaster,
)
from iic_booking.communication.service import CommunicationService  # noqa: E402
from iic_booking.equipment import slot_exchange as sx  # noqa: E402
from iic_booking.equipment.slot_exchange_models import SlotExchangeRequest  # noqa: E402
from iic_booking.users.models import Department, UserType  # noqa: E402

STUDENT_EMAIL = "test.student@iic-booking.test"
FACULTY_EMAIL = "test.faculty@iic-booking.test"
EXTERNAL_EMAILS = ("test.external@iic-booking.test", "test.industry@iic-booking.test")
TAG = "ZZSX" + uuid.uuid4().hex[:4].upper()
RESULTS = []
NOTIFIED = []  # (request id, kind) the app would have sent


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


def _call(client, method, path, body=None):
    res = client.get(path, secure=True) if method == "get" else client.post(path, body or {}, format="json", secure=True)
    data = getattr(res, "data", None)
    if not isinstance(data, dict):
        data = {"data": data}
    return res.status_code, data


def _iic_department():
    for needle in ("Institute Instrumentation Centre", "Instrumentation Centre", "IIC"):
        dept = Department.objects.filter(name__icontains=needle).order_by("pk").first()
        if dept is not None:
            return dept
    return Department.objects.filter(code__iexact="IIC").first()


def run():
    User = get_user_model()
    student = User.objects.filter(email__iexact=STUDENT_EMAIL).first()
    faculty = User.objects.filter(email__iexact=FACULTY_EMAIL).first()
    if student is None or faculty is None:
        check("test accounts found", False, f"student={bool(student)} faculty={bool(faculty)}")
        return
    dept = _iic_department()
    check("IIC department found", dept is not None, getattr(dept, "name", ""))
    print("student", student.pk, student.user_type, "| faculty", faculty.pk, faculty.user_type)
    tz = timezone.get_current_timezone()
    slot_no = {"n": 900}

    def aware(day, hour):
        return timezone.make_aware(datetime.combine(day, time(hour, 0)), tz)

    def make_equipment(suffix, *, enabled=True):
        eq = Equipment.objects.create(
            name=f"{TAG} Slot Exchange {suffix}", code=f"{TAG}{suffix}", slot_duration_minutes=60,
            internal_department=dept, status="ACTIVE", user_rating_enabled=False, slot_exchange_enabled=enabled,
        )
        for user_type in (UserType.STUDENT, UserType.FACULTY):
            ChargeProfile.objects.create(equipment=eq, user_type=user_type, profile_type=EquipmentProfileType.HOUR,
                                         time_formula="60", primary_unit_charge=Decimal("100.00"))
        return eq

    def slot(eq, start, booking):
        slot_no["n"] += 1
        end = start + timedelta(hours=1)
        master = SlotMaster.objects.create(equipment=eq, slot_number=slot_no["n"], is_active=True,
                                           open_time=timezone.localtime(start).time(),
                                           close_time=timezone.localtime(end).time())
        return DailySlot.objects.create(slot_master=master, date=timezone.localtime(start).date(),
                                        start_datetime=start, end_datetime=end, status="BOOKED", booking=booking)

    def book(owner, eq, start, count, charge):
        b = Booking.objects.create(
            user=owner, equipment=eq, charge_profile=ChargeProfile.objects.filter(equipment=eq, user_type=owner.user_type).first(),
            status=BookingStatus.BOOKED, total_charge=Decimal(charge), total_time_minutes=60 * count, input_values={},
            virtual_booking_id=f"{TAG}{uuid.uuid4().hex[:6].upper()}", user_type_snapshot=owner.user_type,
        )
        return b, [slot(eq, start + timedelta(hours=i), b) for i in range(count)]

    day = timezone.localdate() + timedelta(days=3)
    eq = make_equipment("A")
    a, (a1, a2) = book(student, eq, aware(day, 10), 2, "200.00")
    b, (b1,) = book(faculty, eq, aware(day + timedelta(days=1), 10), 1, "100.00")
    stu, fac = _client(student), _client(faculty)

    code, data = _call(fac, "get", f"/api/slot-exchanges/options/?slot_id={a2.pk}")
    check("faculty sees the student's booked slot and can propose an exchange",
          code == 200 and data.get("exchange_available") is True and data.get("booker", {}).get("name"),
          f"status={code} available={data.get('exchange_available')} reason={data.get('exchange_unavailable_reason')}")

    code, data = _call(fac, "post", "/api/slot-exchanges/",
                       {"offered_slot_ids": [b1.pk], "requested_slot_ids": [a2.pk], "message": "E2E swap"})
    req_id = data.get("id")
    check("proposal created and pending", code == 201 and data.get("status") == "PENDING",
          f"status={code} body_status={data.get('status')} error={data.get('error') or data.get('code')}")

    code, data = _call(stu, "get", "/api/slot-exchanges/")
    check("student sees it as incoming", code == 200 and data.get("pending_incoming", 0) >= 1,
          f"status={code} pending_incoming={data.get('pending_incoming')}")

    code, data = _call(stu, "post", f"/api/slot-exchanges/{req_id}/accept/")
    a.refresh_from_db()
    b.refresh_from_db()
    a_slots = set(a.daily_slots.values_list("pk", flat=True))
    b_slots = set(b.daily_slots.values_list("pk", flat=True))
    check("accepted: slots changed hands", code == 200 and a_slots == {a1.pk, b1.pk} and b_slots == {a2.pk},
          f"status={code} student_slots={sorted(a_slots)} faculty_slots={sorted(b_slots)} error={data.get('error')}")
    check("no money moved", str(a.total_charge) == "200.00" and str(b.total_charge) == "100.00",
          f"student={a.total_charge} faculty={b.total_charge}")
    check("both bookings record the exchange",
          a.events.filter(metadata__slot_exchange_id=req_id).exists()
          and b.events.filter(metadata__slot_exchange_id=req_id).exists())

    # Decline and withdraw.
    code, data = _call(stu, "post", "/api/slot-exchanges/",
                       {"offered_slot_ids": [b1.pk], "requested_slot_ids": [a2.pk], "message": "swap back?"})
    back_id = data.get("id")
    code, data = _call(fac, "post", f"/api/slot-exchanges/{back_id}/decline/", {"note": "no thanks"})
    check("a proposal can be declined", code == 200 and data.get("status") == "DECLINED",
          f"status={code} body_status={data.get('status')}")
    code, data = _call(stu, "post", "/api/slot-exchanges/",
                       {"offered_slot_ids": [b1.pk], "requested_slot_ids": [a2.pk]})
    wid = data.get("id")
    code, data = _call(stu, "post", f"/api/slot-exchanges/{wid}/cancel/")
    check("a proposal can be withdrawn", code == 200 and data.get("status") in ("CANCELLED", "WITHDRAWN"),
          f"status={code} body_status={data.get('status')}")
    # Decline and withdraw notify at once (and are recorded here, not sent); "requested" and "accepted" are sent
    # after the transaction commits, which a rolled-back run never does.
    check("decline and withdraw notify the other user (recorded, not sent)",
          (back_id, "declined") in NOTIFIED and (wid, "cancelled") in NOTIFIED, f"notifications={NOTIFIED}")

    # Refusals.
    c, (c1,) = book(faculty, eq, aware(day + timedelta(days=2), 10), 1, "150.00")
    code, data = _call(fac, "post", "/api/slot-exchanges/", {"offered_slot_ids": [c1.pk], "requested_slot_ids": [a1.pk]})
    check("refused when charges differ", code == 400 and data.get("code") == "CHARGE_MISMATCH",
          f"status={code} code={data.get('code')}")
    off = make_equipment("B", enabled=False)
    d, (d1,) = book(student, off, aware(day, 14), 1, "100.00")
    e, (e1,) = book(faculty, off, aware(day, 15), 1, "100.00")
    code, data = _call(fac, "post", "/api/slot-exchanges/", {"offered_slot_ids": [e1.pk], "requested_slot_ids": [d1.pk]})
    check("refused when Slot Exchange is off for the instrument", code == 400 and data.get("code") == "EXCHANGE_DISABLED",
          f"status={code} code={data.get('code')}")
    external = User.objects.filter(email__in=EXTERNAL_EMAILS).first()
    if external is not None:
        code, data = _call(_client(external), "post", "/api/slot-exchanges/",
                           {"offered_slot_ids": [b1.pk], "requested_slot_ids": [a2.pk]})
        check("refused for an external user", code in (400, 403) and data.get("code") in ("NOT_INTERNAL", None),
              f"status={code} code={data.get('code')}")
    else:
        print("SKIP | refused for an external user | no external test account")


def main():
    print("TAG", TAG)
    silent = lambda *a, **k: None  # noqa: E731
    with override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"), \
            patch.object(Task, "apply_async", silent), \
            patch.object(sx, "notify", lambda req, kind: NOTIFIED.append((req.pk, kind))), \
            patch.object(CommunicationService, "send_email", silent), \
            patch.object(CommunicationService, "send_email_with_attachments", silent), \
            patch.object(CommunicationService, "send_sms", silent), \
            patch.object(CommunicationService, "send_push_notification", silent):
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
        "bookings": Booking.objects.filter(equipment__code__startswith=TAG).count(),
        "exchange_requests": SlotExchangeRequest.objects.filter(requester_booking__equipment__code__startswith=TAG).count(),
    }
    check("cleanup: nothing left behind", not any(leftovers.values()), str(leftovers))
    print("INFO | notifications recorded, not sent:", NOTIFIED)
    dept = _iic_department()
    if dept is not None:
        iic = Equipment.objects.filter(internal_department=dept).exclude(status="DISPOSED")
        print("INFO | IIC instruments:", iic.count(), "| Slot Exchange on:", iic.filter(slot_exchange_enabled=True).count())
    failed = [name for name, ok in RESULTS if not ok]
    print("SUMMARY", len(RESULTS) - len(failed), "passed,", len(failed), "failed")
    sys.exit(1 if failed else 0)


main()

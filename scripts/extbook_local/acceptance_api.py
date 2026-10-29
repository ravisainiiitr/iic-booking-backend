"""End-to-end API acceptance run for the external-booking enhancement on the seeded local database.

Runs in-process (Django test client: real URLs, middleware, token login and views) against iic_extbook_accept_dev,
moving the simulated clock through the original timeline (28 Oct 2026 opening, payment deadlines, completion,
09:30 SRIC batch). No gateway, mail server or other external service is involved (see acceptance_local settings).
Run once on a freshly seeded database (``extbook-local.ps1 restore`` resets it from the seeded snapshot).
Results: printed, and written to .acceptance_local/acceptance_api_results.json.
"""

import json
import subprocess
import sys
import traceback
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path

from _bootstrap import ROOT, setup

settings = setup()

from django.db import connection  # noqa: E402
from django.test import Client  # noqa: E402
from django.utils import timezone  # noqa: E402

from config.settings.acceptance_local import set_simulated_clock  # noqa: E402
from iic_booking.equipment.models import Booking, DailySlot, Equipment, UrgentBookingRequest  # noqa: E402
from iic_booking.external_services import tasks  # noqa: E402
from iic_booking.external_services.models import (  # noqa: E402
    SampleReceiptScan,
    SpecialAllocation,
    SricDispatchAttempt,
    SricInvoiceRequest,
)
from iic_booking.users.models import User  # noqa: E402

IST = timezone.get_fixed_timezone(330)
API = "/api/v1/external-booking/"
CREDENTIALS = ROOT / ".acceptance_local" / "credentials.txt"
QR_PYTHON = Path(sys.argv[1]) if len(sys.argv) > 1 else None  # optional: python of a venv with pypdfium2 + zxing-cpp
QR_SCRIPT = Path(sys.argv[2]) if len(sys.argv) > 2 else None
RESULTS = []


def at(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


def clock(dt):
    set_simulated_clock(dt)
    return dt


def check(case, name, ok, detail=""):
    RESULTS.append({"case": case, "check": name, "ok": bool(ok), "detail": str(detail)[:400]})
    print(f"  [{'PASS' if ok else 'FAIL'}] {case}: {name}" + (f"  ({str(detail)[:160]})" if detail and not ok else ""))
    return ok


def password():
    for line in CREDENTIALS.read_text(encoding="utf-8").splitlines():
        if line.startswith("password: "):
            return line.split(": ", 1)[1].strip()
    raise SystemExit("credentials file missing; run the seed first")


class Api:
    """One signed-in person: token login through the real endpoint; signs in again after a long clock jump."""

    def __init__(self, key):
        self.email = f"{key}@acceptance.invalid"
        self.user = User.objects.get(email=self.email)
        self.client = Client(HTTP_HOST="127.0.0.1")
        self.token = None
        self.login()

    def login(self):
        r = self.client.post("/api/auth/login/", data=json.dumps({"email": self.email, "password": password()}),
                             content_type="application/json")
        assert r.status_code == 200, (self.email, r.status_code, r.content[:300])
        self.token = r.json()["token"]

    def call(self, method, path, data=None, *, multipart=False, retry=True):
        kwargs = {"HTTP_AUTHORIZATION": f"Token {self.token}"}
        fn = getattr(self.client, method)
        if method == "get":
            r = fn(path, data or {}, **kwargs)
        elif multipart:
            r = fn(path, data or {}, **kwargs)
        else:
            r = fn(path, data=json.dumps(data or {}), content_type="application/json", **kwargs)
        if r.status_code == 401 and retry:
            self.login()
            return self.call(method, path, data, multipart=multipart, retry=False)
        return r

    def get(self, path, params=None):
        return self.call("get", path, params)

    def post(self, path, data=None, **kw):
        return self.call("post", path, data, **kw)

    def patch(self, path, data=None):
        return self.call("patch", path, data)

    def delete(self, path):
        return self.call("delete", path)


def body(r):
    try:
        return r.json()
    except Exception:
        return {"_raw": r.content[:200]}


def free_slot(eq, d, hour=None):
    qs = DailySlot.objects.filter(slot_master__equipment=eq, date=d, status="AVAILABLE", booking__isnull=True)
    for s in qs.order_by("start_datetime"):
        if hour is None or timezone.localtime(s.start_datetime).hour >= hour:
            return s
    raise AssertionError(f"no free slot on {d} for {eq.code}")


def book(api, eq, slot, **extra):
    r = api.post(f"/api/equipments/{eq.pk}/book/", {"slot_ids": [slot.pk], "input_values": {"A": 1}, **extra})
    slot.refresh_from_db()
    booking = Booking.objects.filter(pk=slot.booking_id).first() if slot.booking_id else None
    return r, booking


def checkout_pay(api, eq, slot, choice, *, capture=True):
    """External self-booking through the checkout (nothing held before payment). Online choices use the mock gateway
    in this process: the order is created by the application, then captured and delivered by signed webhook.
    Returns (checkout payload after payment, booking or None)."""
    from mock_razorpay import MockGateway

    r = api.post(f"{API}checkouts/", {"equipment_id": eq.pk, "slot_ids": [slot.pk], "input_values": {"A": 1}})
    if r.status_code != 201:
        return body(r), None
    cid = body(r)["id"]
    gw = MockGateway()
    with gw.active():
        r = api.post(f"{API}checkouts/{cid}/pay/", {"choice": choice})
    if r.status_code != 200:
        return body(r), None
    order = body(r).get("order")
    if order and capture:
        gw.webhook(gw.capture(order["order_id"]))  # the order was created through this gateway instance above
    out = body(api.get(f"{API}checkouts/{cid}/"))
    booking = Booking.objects.filter(pk=(out.get("booking") or {}).get("id")).first()
    return out, booking


def ids_in(obj, keys=("id", "request_id", "urgent_request_id")):
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, int):
                out.add(v)
            else:
                out |= ids_in(v, keys)
    elif isinstance(obj, list):
        for v in obj:
            out |= ids_in(v, keys)
    return out


def is_pdf(r):
    content = b"".join(r.streaming_content) if getattr(r, "streaming", False) else r.content
    return r.status_code == 200 and content[:5] == b"%PDF-", content


def synthetic_invoice_pdf(number):
    from reportlab.pdfgen import canvas

    buf = BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 750, f"SYNTHETIC TEST INVOICE {number} - local acceptance only, not a tax document")
    c.save()
    return buf.getvalue()


def special_request(api, eq, start, *, ack=None, description="Synthetic sample (acceptance)"):
    data = {
        "equipment_id": eq.pk, "input_values": {"A": 1}, "number_of_samples": 1,
        "preferred_start": start.isoformat(), "preferred_end": (start + timedelta(hours=1)).isoformat(),
        "alternatives": [], "sample_description": description,
    }
    if ack is not None:
        data["short_notice_ack_version"] = ack
    return api.post(f"{API}special-requests/", data)


def send_signed_webhook(order, *, tag):
    import hashlib
    import hmac

    payload = {"event": "payment.captured", "payload": {"payment": {"entity": {
        "id": f"pay_LOCAL{tag}", "order_id": order.razorpay_order_id, "method": "upi",
        "amount": int(order.total_amount * 100), "currency": "INR", "status": "captured"}}}}
    raw = json.dumps(payload).encode()
    sig = hmac.new(settings.RAZORPAY_WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return Client(HTTP_HOST="127.0.0.1").post("/api/payments/razorpay/webhook/", data=raw,
                                               content_type="application/json", HTTP_X_RAZORPAY_SIGNATURE=sig)


def gateway_order(booking, tag):
    """The order row create_order would store after the gateway call, guarded by the same refusal."""
    from iic_booking.external_services.special_payment import payment_refusal
    from iic_booking.payments.fees import compute_fee_breakup
    from iic_booking.payments.models import PaymentOrder

    refusal = payment_refusal(booking)
    if refusal:
        return None, refusal
    b = compute_fee_breakup(booking.amount_due)
    return PaymentOrder.objects.create(
        razorpay_order_id=f"order_LOCAL{tag}", receipt=f"local_{tag}", purpose="BOOKING_SHORTFALL",
        user=booking.user, booking=booking, department=booking.settlement_department, base_amount=b.base_amount,
        convenience_fee=b.convenience_fee, fee_gst=b.fee_gst, total_amount=b.total_amount, currency="INR",
        status="CREATED", idempotency_key=f"local:{tag}", raw_create_response={"simulated": True},
    ), None


def main():
    xrd = Equipment.objects.get(code="ACCXRD")
    sem = Equipment.objects.get(code="ACCSEM")
    with connection.cursor() as cur:
        cur.execute("select current_database(), coalesce(host(inet_server_addr()),'socket')")
        print("database:", cur.fetchone())

    # ---- before the opening (28 Oct 2026 20:55 IST) --------------------------------------------------------------
    clock(at(2026, 10, 28, 20, 55))
    ext, ext2, ind = Api("ext.academic"), Api("ext.academic2"), Api("ext.industry")
    student, faculty, oic = Api("student"), Api("faculty"), Api("oic")
    operator, accounts, admin = Api("operator"), Api("accounts"), Api("admin")

    w = body(ext.get(f"{API}window/"))
    check("A", "before 21:00 on 28 Oct the window is closed", w.get("enabled") and not w.get("is_open"), w)
    check("A", "the opening is 28 Oct 2026 21:00 IST", (w.get("opens_at") or "").startswith("2026-10-28T21:00"), w)
    r, b = book(ext, xrd, free_slot(xrd, date(2026, 11, 10), 10))
    check("B", "booking before the opening is refused (EXTERNAL_WINDOW_CLOSED)",
          r.status_code == 400 and body(r).get("code") == "EXTERNAL_WINDOW_CLOSED" and b is None, body(r))

    # ---- opening: 28 Oct 2026 21:05 IST --------------------------------------------------------------------------
    clock(at(2026, 10, 28, 21, 5))
    w = body(ext.get(f"{API}window/"))
    check("A", "window open: 9 Nov to 6 Dec 2026",
          w.get("is_open") and w.get("first_date") == "2026-11-09" and w.get("last_date") == "2026-12-06", w)
    check("A", "window applies to the external user", w.get("applies_to_you") is True, w)
    check("A", "next expansion Wed 4 Nov 21:00 opens 7-13 Dec",
          (w.get("next_expansion_at") or "").startswith("2026-11-04T21:00") and w.get("next_week_start") == "2026-12-07",
          w)
    s = ext.get(f"/api/equipments/{xrd.pk}/slots/", {"start_date": "2026-11-09", "end_date": "2026-11-15"})
    check("A", "slot grid for an external user carries the window", s.status_code == 200 and
          (body(s).get("external_window") or {}).get("first_date") == "2026-11-09", str(body(s))[:300])

    for d in (date(2026, 11, 3), date(2026, 12, 8)):
        r, b = book(ext, xrd, free_slot(xrd, d, 10))
        check("B", f"ordinary external booking on {d} (outside) is refused",
              r.status_code == 400 and body(r).get("code") == "EXTERNAL_WINDOW_CLOSED" and b is None, body(r))
    probe_slot = free_slot(xrd, date(2026, 11, 10), 10)
    r, held = book(ext, xrd, probe_slot)
    check("B", "direct external booking asks for a payment choice and holds nothing",
          r.status_code == 409 and body(r).get("code") == "external_checkout_required" and held is None, body(r))
    out, ext_ordinary = checkout_pay(ext, xrd, probe_slot, "full_gateway")
    check("B", "zero-balance external booking: online payment captured -> booked inside the window",
          out.get("state") == "confirmed" and ext_ordinary is not None and ext_ordinary.status == "BOOKED", out)
    out, ind_booking = checkout_pay(ind, sem, free_slot(sem, date(2026, 11, 11), 10), "full_wallet")
    check("B", "industry user (wallet funded) pays from the wallet and is booked inside the window",
          out.get("state") == "confirmed" and ind_booking is not None and ind_booking.status == "BOOKED", out)

    # security: external holds
    r, b = book(ext, xrd, free_slot(xrd, date(2026, 11, 12), 10), create_as_hold=True)
    check("SEC", "external create_as_hold is refused (EXTERNAL_HOLD_NOT_ALLOWED)",
          r.status_code == 403 and body(r).get("code") == "EXTERNAL_HOLD_NOT_ALLOWED" and b is None, body(r))
    r, staff_hold = book(oic, xrd, free_slot(xrd, date(2026, 11, 12), 11), create_as_hold=True, user_id=ext2.user.pk)
    check("SEC", "OIC can still create a HOLD on behalf of an external user",
          r.status_code in (200, 201) and staff_hold is not None and staff_hold.status == "HOLD",
          f"{r.status_code} {body(r)} {getattr(staff_hold, 'status', None)}")

    # ---- C / Q: internal users ----------------------------------------------------------------------------------
    s = student.get(f"/api/equipments/{xrd.pk}/slots/", {"start_date": "2026-11-02", "end_date": "2026-11-08"})
    ew = body(s).get("external_window")
    check("C", "internal student's slot grid is not limited by the external window",
          s.status_code == 200 and (not ew or not ew.get("applies_to_you")), str(ew)[:200])
    r, sb = book(student, xrd, free_slot(xrd, date(2026, 11, 3), 10))
    check("C", "internal student books next week as before (not refused by the external window)",
          body(r).get("code") != "EXTERNAL_WINDOW_CLOSED" and sb is not None, f"{r.status_code} {body(r)}")
    r, fb = book(faculty, sem, free_slot(sem, date(2026, 11, 4), 10))
    check("Q", "internal faculty booking unchanged (wallet-paid booking created)",
          sb is not None and fb is not None and fb.status in ("BOOKED", "PENDING"), f"{r.status_code} {body(r)}")
    r, b = book(student, xrd, free_slot(xrd, date(2026, 12, 8), 10))
    check("Q", "internal booking far ahead follows the existing internal rule, not EXTERNAL_WINDOW_CLOSED",
          body(r).get("code") != "EXTERNAL_WINDOW_CLOSED", f"{r.status_code} {str(body(r))[:200]}")

    # ---- D: special requests -----------------------------------------------------------------------------------
    pol = body(ext.get(f"{API}special-requests/policy/"))
    r1 = special_request(ext, xrd, at(2026, 12, 9, 10))
    check("D", "special request outside the window is accepted", r1.status_code == 201, body(r1))
    req1 = body(r1).get("id")
    r = special_request(ext, xrd, at(2026, 11, 5, 11))
    check("D", "short-notice request without acknowledgement is refused",
          r.status_code == 400 and body(r).get("code") == "acknowledgement_required", body(r))
    version = ((body(r).get("policy") or {}).get("version")) or ((pol.get("short_notice") or {}).get("version"))
    r2 = special_request(ext, xrd, at(2026, 11, 5, 11), ack=version)
    check("D", "short-notice request with the current acknowledgement is accepted", r2.status_code == 201, body(r2))
    req2 = body(r2).get("id")
    r3 = special_request(ind, sem, at(2026, 12, 10, 10))
    check("D", "industry user can submit a special request", r3.status_code == 201, body(r3))
    req3 = body(r3).get("id")

    # isolation from the internal urgent-request screens
    ext_special_ids = {req1, req2, req3}
    lst = oic.get("/api/urgent-booking-requests/")
    check("SEC", "internal urgent-request list does not show external special requests",
          lst.status_code == 200 and not (ids_in(body(lst)) & ext_special_ids), f"{lst.status_code}")
    mine = ext.get("/api/urgent-booking-requests/my/")
    check("SEC", "'my urgent requests' does not show them either (403 for external users is the existing rule)",
          mine.status_code in (200, 403) and not (ids_in(body(mine)) & ext_special_ids), f"{mine.status_code}")
    check("SEC", "default manager hides EXTERNAL_SPECIAL rows",
          not UrgentBookingRequest.objects.filter(pk__in=ext_special_ids).exists()
          and UrgentBookingRequest.all_types.filter(pk__in=ext_special_ids).count() == 3)

    # ---- E: OIC review ----------------------------------------------------------------------------------------
    staff_rows = body(oic.get(f"{API}staff/special-requests/"))
    check("E", "OIC sees the requests for their equipment", ids_in(staff_rows) >= ext_special_ids, str(staff_rows)[:200])
    check("E", "an external user cannot open the staff queue",
          ext.get(f"{API}staff/special-requests/").status_code == 403)
    r = oic.post(f"{API}staff/special-requests/{req1}/clarify/", {"message": "Which 2-theta range do you need?"})
    check("E", "OIC asks for clarification", r.status_code == 200, body(r))
    r = ext.post(f"{API}special-requests/{req1}/respond/", {"message": "10 to 80 degrees, step 0.02"})
    check("E", "user answers the clarification", r.status_code == 200, body(r))
    cand = oic.get(f"{API}staff/special-requests/{req1}/slots/", {"start": "2026-12-09", "days": 1})
    target1 = free_slot(xrd, date(2026, 12, 9), 10)
    check("E", "candidate slots include the preferred slot", target1.pk in ids_in(body(cand), ("id", "slot_id")),
          str(body(cand))[:200])
    r = oic.post(f"{API}staff/special-requests/{req1}/allocate/", {"slot_ids": [target1.pk]})
    check("E", "OIC allocates a valid slot", r.status_code == 200, body(r))
    a1 = SpecialAllocation.objects.filter(request_id=req1).first()
    target2 = free_slot(xrd, date(2026, 11, 5), 11)
    r = oic.post(f"{API}staff/special-requests/{req2}/allocate/", {"slot_ids": [target2.pk]})
    check("E", "OIC allocates the short-notice request", r.status_code == 200, body(r))
    a2 = SpecialAllocation.objects.filter(request_id=req2).first()
    target3 = free_slot(sem, date(2026, 12, 10), 10)
    r = oic.post(f"{API}staff/special-requests/{req3}/allocate/", {"slot_ids": [target3.pk]})
    check("E", "OIC allocates the industry request", r.status_code == 200, body(r))
    a3 = SpecialAllocation.objects.filter(request_id=req3).first()
    r = ext2.get(f"{API}special-requests/{req1}/")
    check("SEC", "another external user cannot read the request", r.status_code in (403, 404), r.status_code)

    # ---- F: deadline ------------------------------------------------------------------------------------------
    d1 = body(ext.get(f"{API}special-requests/{req1}/"))
    alloc = d1.get("allocation") or {}
    expected = (a1.approved_at + timedelta(hours=24)) if a1 else None
    check("F", "user sees the payment deadline (approval + 24 h, earlier than the slot)",
          a1 and alloc.get("payment_deadline_at") and abs(
              datetime.fromisoformat(alloc["payment_deadline_at"]) - expected) < timedelta(seconds=1), alloc)
    pdf_ok, _ = is_pdf(ext.get(f"{API}special-requests/{req1}/provisional-invoice/"))
    check("I", "pro forma (provisional) invoice for the allocation downloads as PDF", pdf_ok)
    if ext_ordinary:
        pdf_ok, _ = is_pdf(ext.get(f"{API}bookings/{ext_ordinary.pk}/provisional-invoice/"))
        check("I", "pro forma invoice for an ordinary booking downloads as PDF", pdf_ok)
        check("SEC", "another external user cannot download it",
              ext2.get(f"{API}bookings/{ext_ordinary.pk}/provisional-invoice/").status_code in (403, 404))

    # ---- G: payment before the deadline (simulated gateway) --------------------------------------------------
    b1 = a1.booking
    order1, refusal = gateway_order(b1, "G1")
    check("G", "payment order allowed before the deadline", order1 is not None, refusal)
    r = send_signed_webhook(order1, tag="G1")
    b1.refresh_from_db()
    a1.refresh_from_db()
    check("G", "signed webhook settles: booking BOOKED, allocation paid",
          r.status_code == 200 and b1.status == "BOOKED" and a1.state == "paid", f"{r.status_code} {b1.status} {a1.state}")
    r = send_signed_webhook(order1, tag="G1")
    from iic_booking.payments.models import Payment

    check("SEC", "webhook replay is idempotent (one payment row, state unchanged)",
          r.status_code == 200 and Payment.objects.filter(payment_order=order1).count() == 1, r.status_code)
    bad = Client(HTTP_HOST="127.0.0.1").post("/api/payments/razorpay/webhook/", data=b'{"event":"payment.captured"}',
                                             content_type="application/json", HTTP_X_RAZORPAY_SIGNATURE="00")
    check("SEC", "unsigned/forged webhook is rejected", bad.status_code == 400, bad.status_code)

    # orders for the other two allocations are opened now, before their deadlines
    b2, b3 = a2.booking, a3.booking
    if a2.short_notice:
        r = ext.post(f"{API}special-requests/{req2}/acknowledge-short-notice/",
                     {"short_notice_ack_version": version})
        check("F", "short-notice allocation acknowledged before payment", r.status_code == 200, body(r))
    order2, _ = gateway_order(b2, "H2")
    order3, _ = gateway_order(b3, "G3")
    check("G", "orders opened before the deadline", order2 is not None and order3 is not None)

    # ---- H / grace: deadline + 5 min, + 10 min, + 16 min -------------------------------------------------------
    clock(a2.payment_deadline_at + timedelta(minutes=5))
    _o, refusal = gateway_order(b3, "late")
    check("G", "a new payment order after the deadline is refused", _o is None and refusal, refusal)
    res = tasks.expire_special_allocations()
    a2.refresh_from_db()
    check("H", "within the 15-minute grace nothing expires", a2.state == "awaiting_payment", res)
    clock(a3.payment_deadline_at + timedelta(minutes=10))
    r = send_signed_webhook(order3, tag="G3")
    b3.refresh_from_db()
    a3.refresh_from_db()
    check("G", "gateway payment confirmed 10 min after the deadline (grace) is accepted",
          r.status_code == 200 and a3.state == "paid" and b3.status == "BOOKED", f"{a3.state} {b3.status}")
    clock(a2.payment_deadline_at + timedelta(minutes=16))
    res = tasks.expire_special_allocations()
    a2.refresh_from_db()
    b2.refresh_from_db()
    target2.refresh_from_db()
    check("H", "unpaid allocation expires after deadline + grace", a2.state == "expired", res)
    check("H", "its booking is cancelled for the payment deadline",
          b2.status == "CANCELLED", f"{b2.status} {getattr(b2, 'cancellation_reason', '')}")
    check("H", "its slot is restored (AVAILABLE, no booking)", target2.status == "AVAILABLE" and not target2.booking_id,
          f"{target2.status} {target2.booking_id}")
    r = send_signed_webhook(order2, tag="H2")
    a2.refresh_from_db()
    b2.refresh_from_db()
    check("H", "a payment arriving after expiry is recorded as late and does not revive the booking",
          r.status_code == 200 and b2.status == "CANCELLED" and a2.late_payment_at is not None, f"{b2.status}")
    check("H", "expiry task is idempotent", tasks.expire_special_allocations() is not None and
          SpecialAllocation.objects.get(pk=a2.pk).state == "expired")

    # ---- window expansion on Wednesday 4 Nov 21:00 -------------------------------------------------------------
    clock(at(2026, 11, 4, 21, 1))
    w = body(ext.get(f"{API}window/"))
    check("A", "weekly expansion: on 4 Nov 21:01 the window runs to 13 Dec", w.get("last_date") == "2026-12-13", w)

    # ---- J: invoice preferences --------------------------------------------------------------------------------
    r = ext.post(f"{API}bookings/{b1.pk}/invoice/", {"option": "after_analysis"})
    check("J", "user chooses 'GST invoice after analysis'", r.status_code == 200, body(r))
    r = ind.post(f"{API}bookings/{b3.pk}/invoice/", {"option": "early"})
    early = body(r).get("policy") or {}
    check("J", "early option requires the disclosure acknowledgement",
          r.status_code == 400 and body(r).get("code") == "acknowledgement_required", body(r))
    r = ind.post(f"{API}bookings/{b3.pk}/invoice/", {"option": "early", "early_invoice_ack_version": early.get("version")})
    check("J", "early option with acknowledgement is recorded", r.status_code == 200, body(r))
    r = ind.post(f"{API}bookings/{b3.pk}/invoice/request-early/")
    check("K", "paid booking with early preference is queued for SRIC", r.status_code in (200, 201) and
          SricInvoiceRequest.objects.filter(booking=b3).count() == 1, body(r))
    r = ind.post(f"{API}bookings/{b3.pk}/invoice/request-early/")
    check("SEC", "duplicate early request creates no second SRIC request",
          SricInvoiceRequest.objects.filter(booking=b3).count() == 1, body(r))
    check("SEC", "another user cannot read the invoice status", ext2.get(f"{API}bookings/{b1.pk}/invoice/").status_code
          in (403, 404))

    # ---- O / P: sample label and receipt (industry booking b3 is BOOKED) ---------------------------------------
    r = ind.post(f"{API}addresses/", {"label": "Lab", "recipient_name": "Synthetic Sender", "line1": "1 Synthetic Street",
                                      "city": "Testpur", "state": "Uttarakhand", "postal_code": "247667",
                                      "country": "India", "phone": "9000000000", "is_default": True})
    check("N", "user saves an address", r.status_code in (200, 201), body(r))
    ind_addr = body(r).get("id")
    check("SEC", "another user cannot edit the saved address",
          ext2.patch(f"{API}addresses/{ind_addr}/", {"city": "Elsewhere"}).status_code == 404)
    label_r = ind.post(f"{API}bookings/{b3.pk}/sample-label/", {"sample_count": 2, "address_id": ind_addr})
    ok, pdf = is_pdf(label_r)
    check("O", "user prints the 4.5x10 in sample label (PDF)", ok, label_r.status_code)
    code = None
    if ok and QR_PYTHON and QR_SCRIPT:
        tmp = ROOT / ".acceptance_local" / "label_api.pdf"
        tmp.write_bytes(pdf)
        out = subprocess.run([str(QR_PYTHON), "-I", str(QR_SCRIPT), str(tmp)], capture_output=True, text=True)
        url = out.stdout.strip().splitlines()[0] if out.returncode == 0 and out.stdout.strip() else ""
        code = url.split("code=", 1)[1] if "code=" in url else None
        check("O", "label QR decodes to the staff receipt URL", code and "/staff/sample-receipt?code=" in url, url)
    if code:
        check("SEC", "the external user (even the label owner) cannot use the receipt lookup (404, label not disclosed)",
              ind.post(f"{API}staff/sample-receipt/lookup/", {"code": code}).status_code == 404)
        check("SEC", "another external user cannot either",
              ext2.post(f"{API}staff/sample-receipt/lookup/", {"code": code}).status_code == 404)
        check("SEC", "a wrong code is not found",
              operator.post(f"{API}staff/sample-receipt/lookup/", {"code": code + "x"}).status_code == 404)
        lk = operator.post(f"{API}staff/sample-receipt/lookup/", {"code": code})
        check("O", "lab operator scans the QR and sees the booking", lk.status_code == 200, body(lk))
        cf = operator.post(f"{API}staff/sample-receipt/confirm/", {"code": code, "outcome": "forwarded_to_lab"})
        check("P", "lab operator confirms receipt", cf.status_code == 200, body(cf))
        cf2 = operator.post(f"{API}staff/sample-receipt/confirm/", {"code": code, "outcome": "forwarded_to_lab"})
        check("SEC", "a second confirmation is reported as duplicate, one receipt recorded",
              cf2.status_code == 200 and body(cf2).get("recorded") is False and
              list(SampleReceiptScan.objects.filter(label__booking=b3, action__in=["confirmed", "duplicate"])
                   .order_by("pk").values_list("action", flat=True)) == ["confirmed", "duplicate"], body(cf2))
        check("P", "receipt scan rows recorded", SampleReceiptScan.objects.filter(label__booking=b3).exists())
    else:
        check("O", "QR decoding available", False, "pass the QR tool paths to decode the label")

    # ---- K: completion, queue, 09:30 batch --------------------------------------------------------------------
    clock(at(2026, 12, 9, 12, 0))
    r = oic.post(f"/api/bookings/{b1.pk}/complete/", {})
    b1.refresh_from_db()
    check("K", "OIC marks the special booking completed", r.status_code == 200 and b1.status == "COMPLETED",
          f"{r.status_code} {str(body(r))[:200]}")
    res = tasks.queue_completed_invoice_requests()
    check("K", "completed + paid + 'after analysis' booking enters the SRIC queue",
          SricInvoiceRequest.objects.filter(booking=b1).count() == 1, res)
    tasks.queue_completed_invoice_requests()
    check("SEC", "re-running the queue task creates no duplicate", SricInvoiceRequest.objects.filter(booking=b1).count()
          == 1)
    clock(at(2026, 12, 10, 9, 20))
    res = tasks.sric_invoice_batch()
    check("K", "batch does not send before 09:30", "before_0930" in json.dumps(res), res)
    clock(at(2026, 12, 10, 9, 31))
    mail_dir = Path(settings.EMAIL_FILE_PATH)
    before = set(mail_dir.glob("*.log")) if mail_dir.exists() else set()
    res = tasks.sric_invoice_batch()
    sent = SricDispatchAttempt.objects.filter(request__booking__in=[b1, b3]).count()
    new_mail = (set(mail_dir.glob("*.log")) - before) if mail_dir.exists() else set()
    to_test = any("sric-test@acceptance.invalid" in p.read_text(encoding="utf-8", errors="ignore") for p in new_mail)
    check("K", "09:30 working-day batch sends in TEST mode to the test mailbox (file backend)",
          sent >= 1 and to_test, f"{res} attempts={sent} files={len(new_mail)}")

    # ---- L / M: official invoice ---------------------------------------------------------------------------------
    req = SricInvoiceRequest.objects.get(booking=b1)
    q = accounts.get(f"{API}staff/invoice-requests/")
    check("L", "Accounts In Charge sees the request", req.pk in ids_in(body(q)), str(body(q))[:200])
    check("SEC", "external user cannot open the Accounts queue", ext.get(f"{API}staff/invoice-requests/").status_code
          == 403)
    from django.core.files.uploadedfile import SimpleUploadedFile

    up = accounts.post(f"{API}staff/invoice-requests/{req.pk}/upload/", {
        "file": SimpleUploadedFile("SYN-0001.pdf", synthetic_invoice_pdf("SYN-0001"), content_type="application/pdf"),
        "booking_reference": b1.virtual_booking_id, "invoice_number": "SYN-0001"}, multipart=True)
    check("L", "Accounts In Charge uploads a synthetic official invoice", up.status_code in (200, 201), body(up))
    bad = accounts.post(f"{API}staff/invoice-requests/{req.pk}/upload/", {
        "file": SimpleUploadedFile("x.pdf", b"not a pdf", content_type="application/pdf"),
        "booking_reference": b1.virtual_booking_id}, multipart=True)
    check("SEC", "a non-PDF upload is rejected", bad.status_code == 400, body(bad))
    doc_id = body(up).get("document_id")
    if doc_id:
        ok, _ = is_pdf(ext.get(f"{API}invoices/documents/{doc_id}/download/"))
        check("M", "user downloads the official invoice", ok)
        check("SEC", "another external user cannot download it",
              ext2.get(f"{API}invoices/documents/{doc_id}/download/").status_code in (403, 404))
        check("SEC", "an internal student cannot download it",
              student.get(f"{API}invoices/documents/{doc_id}/download/").status_code in (403, 404))

    # ---- N: printed copy ----------------------------------------------------------------------------------------
    r = ext.post(f"{API}addresses/", {"label": "Office", "recipient_name": "Synthetic Recipient", "line1": "2 Synthetic Road",
                                      "city": "Testpur", "state": "Uttarakhand", "postal_code": "247667",
                                      "country": "India", "phone": "9000000001"})
    addr = body(r).get("id")
    r = ext.post(f"{API}bookings/{b1.pk}/invoice/copy/", {"address_id": addr})
    check("N", "user requests a printed copy to the saved address", r.status_code in (200, 201), body(r))
    copy_id = body(r).get("id")
    rows = accounts.get(f"{API}staff/invoice-copies/")
    check("N", "Accounts sees the copy request", copy_id in ids_in(body(rows)), str(body(rows))[:200])
    if copy_id:
        ok, _ = is_pdf(accounts.get(f"{API}staff/invoice-copies/{copy_id}/envelope/"))
        check("N", "Accounts prints the envelope label", ok)

    # ---- admin settings authorisation --------------------------------------------------------------------------
    check("SEC", "OIC cannot change the window", oic.post(f"{API.replace('external-booking', 'admin/external-booking')}"
          "window/pause/", {"reason": "not allowed for OIC"}).status_code == 403)
    hist = admin.get("/api/v1/admin/external-booking/window/history/")
    check("SEC", "window change history records the local enabling", hist.status_code == 200 and
          "acceptance" in json.dumps(body(hist)).lower(), str(body(hist))[:200])


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        RESULTS.append({"case": "RUN", "check": "script completed", "ok": False, "detail": traceback.format_exc()[-400:]})
    passed = sum(r["ok"] for r in RESULTS)
    print(f"\n{passed} passed, {len(RESULTS) - passed} failed, {len(RESULTS)} checks")
    out = ROOT / ".acceptance_local" / "acceptance_api_results.json"
    out.write_text(json.dumps(RESULTS, indent=1), encoding="utf-8")
    sys.exit(0 if passed == len(RESULTS) else 1)

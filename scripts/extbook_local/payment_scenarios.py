"""Payment scenarios for external special allocations, through the mock gateway and the real payment code.

Run on a freshly restored acceptance database: ``extbook-local.ps1 restore`` then ``extbook-local.ps1 payments``.
For every scenario the booking status, payment order, payment rows, wallet balances, settlement fields and the
special-request audit events are checked. Results: printed and written to
``.acceptance_local/payment_scenarios_results.json``.

A simulated payment is only evidence of the application's behaviour on local synthetic data; it is never evidence
that money was realised.
"""

import json
import sys
import threading
import traceback
from datetime import date, timedelta

from acceptance_api import ROOT, Api, at, body, clock, free_slot, special_request
from django.db import connection, connections

from iic_booking.equipment.models import Booking, Equipment
from iic_booking.external_services import tasks
from iic_booking.external_services.models import SpecialAllocation, SpecialRequestEvent
from iic_booking.payments.models import Payment, PaymentOrder, PaymentSettlement
from iic_booking.users.models import SubWallet
from mock_razorpay import MockGateway, MockGatewayError

STAFF = "/api/v1/external-booking/staff/special-requests/"
RESULTS = []


def check(case, name, ok, detail=""):
    RESULTS.append({"case": case, "check": name, "ok": bool(ok), "detail": str(detail)[:400]})
    print(f"  [{'PASS' if ok else 'FAIL'}] {case}: {name}" + (f"  ({str(detail)[:200]})" if detail and not ok else ""))


def note(case, text):
    RESULTS.append({"case": case, "check": "OBSERVATION: " + text, "ok": True, "detail": ""})
    print(f"  [NOTE] {case}: {text}")


def wallets():
    return {sw.pk: sw.balance for sw in SubWallet.objects.all()}


def events(a):
    return list(SpecialRequestEvent.objects.filter(request_id=a.request_id).order_by("pk").values_list("action", flat=True))


def state(a):
    a.refresh_from_db()
    b = Booking.objects.get(pk=a.booking_id)
    return a, b


class World:
    def __init__(self):
        self.xrd = Equipment.objects.get(code="ACCXRD")
        self.users = [Api("ext.academic"), Api("ext.academic2"), Api("ext.industry")]
        self.oic = Api("oic")
        self.n = 0

    def allocation(self, d: date, hour: int):
        user = self.users[self.n % 3]
        self.n += 1
        r = special_request(user, self.xrd, at(d.year, d.month, d.day, hour))
        assert r.status_code == 201, body(r)
        rid = body(r)["id"]
        slot = free_slot(self.xrd, d, hour)
        r = self.oic.post(f"{STAFF}{rid}/allocate/", {"slot_ids": [slot.pk]})
        assert r.status_code == 200, body(r)
        a = SpecialAllocation.objects.get(request_id=rid)
        return user, a, slot


def concurrent(fns):
    out = [None] * len(fns)
    barrier = threading.Barrier(len(fns))

    def run(i, fn):
        try:
            barrier.wait()
            out[i] = fn()
        except Exception as exc:  # recorded and asserted by the caller
            out[i] = exc
        finally:
            connections.close_all()

    threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    return out


def main():
    with connection.cursor() as cur:
        cur.execute("select current_database(), host(inet_server_addr())")
        print("database:", cur.fetchone())
    clock(at(2026, 10, 28, 21, 10))
    w = World()
    gw = MockGateway()
    settlements_before = PaymentSettlement.objects.count()
    wallets_before = wallets()

    # One allocation per scenario, all approved now (deadline = approval + 24 h, slots in December).
    days = [date(2026, 12, d) for d in (7, 8, 9, 10, 11, 14, 15, 16, 17, 18, 21, 22, 23)]
    alloc = {}
    for key, d in zip(["s1", "s1b", "s2", "s6a", "s5", "s7a", "s7b", "s8", "s9", "s10", "r1", "r2", "r3"], days, strict=True):
        alloc[key] = w.allocation(d, 10)
    race_orders = {k: gw.create_booking_order(alloc[k][0].user, alloc[k][1].booking)[0] for k in ("r1", "r2", "r3")}
    for key, (_u, a, _s) in alloc.items():
        check("SETUP", f"{key}: allocation awaiting payment, deadline = approval + 24 h",
              a.state == "awaiting_payment" and a.payment_deadline_at == a.approved_at + timedelta(hours=24))

    # 1. valid successful payment (webhook)
    user, a, _ = alloc["s1"]
    order, resp = gw.create_booking_order(user.user, a.booking)
    check("1", "real create_order accepted the booking with the mock client", order.razorpay_order_id.startswith(
        "order_MOCK") and resp["key_id"] == "rzp_test_ACCEPTANCELOCAL")
    entity = gw.capture(order.razorpay_order_id)
    r = gw.webhook(entity)
    a, b = state(a)
    order.refresh_from_db()
    pays = list(Payment.objects.filter(payment_order=order))
    check("1", "webhook 200; booking BOOKED; order PAID; allocation paid",
          r.status_code == 200 and b.status == "BOOKED" and order.status == "PAID" and a.state == "paid",
          f"{r.status_code} {b.status} {order.status} {a.state}")
    check("1", "one captured payment for the order total; booking settled (amount_due 0, settled_at set)",
          len(pays) == 1 and pays[0].amount == order.total_amount and pays[0].status == "CAPTURED"
          and b.amount_due == 0 and b.payment_settled_at is not None, f"{pays} {b.amount_due}")
    check("1", "audit trail: submitted, allocated, paid", events(a)[-1] == "paid" and "allocated" in events(a), events(a))

    # 1b. valid successful payment through the checkout verify endpoint
    user, a, _ = alloc["s1b"]
    order, _ = gw.create_booking_order(user.user, a.booking)
    entity = gw.capture(order.razorpay_order_id)
    bad = gw.checkout_verify(user.token, entity, signature="0" * 64)
    a, b = state(a)
    check("1b", "checkout verify with a wrong signature is rejected and changes nothing",
          bad.status_code == 400 and b.status == "PENDING_PAYMENT" and a.state == "awaiting_payment", bad.status_code)
    other = gw.checkout_verify(w.users[(w.users.index(user) + 1) % 3].token, entity)
    check("1b", "another user cannot verify this order (403)", other.status_code == 403, other.status_code)
    ok = gw.checkout_verify(user.token, entity)
    a, b = state(a)
    check("1b", "checkout verify with the correct signature settles it", ok.status_code == 200 and b.status == "BOOKED"
          and a.state == "paid", f"{ok.status_code} {body(ok)}")

    # 2. failed payment, then a successful retry
    user, a, _ = alloc["s2"]
    order, _ = gw.create_booking_order(user.user, a.booking)
    r = gw.webhook(gw.fail(order.razorpay_order_id))
    a, b = state(a)
    order.refresh_from_db()
    check("2", "payment.failed: order FAILED, booking still awaiting payment, no payment row",
          r.status_code == 200 and order.status == "FAILED" and b.status == "PENDING_PAYMENT"
          and a.state == "awaiting_payment" and not Payment.objects.filter(payment_order=order).exists(),
          f"{order.status} {b.status} {a.state}")
    order2, _ = gw.create_booking_order(user.user, a.booking)
    r = gw.webhook(gw.capture(order2.razorpay_order_id))
    a, b = state(a)
    check("2", "retry with a new order succeeds", r.status_code == 200 and b.status == "BOOKED" and a.state == "paid")

    # 3. duplicate success callbacks (webhook replay + checkout verify for the same payment)
    user, a, _ = alloc["s1"]
    order = PaymentOrder.objects.filter(booking=a.booking, status="PAID").first()
    entity = gw.payments[Payment.objects.get(payment_order=order).razorpay_payment_id]
    r1, r2 = gw.webhook(entity), gw.checkout_verify(user.token, entity)
    a, b = state(a)
    check("3", "replayed webhook and late checkout verify: 200, still one payment, no extra audit event",
          r1.status_code == 200 and r2.status_code == 200 and Payment.objects.filter(payment_order=order).count() == 1
          and events(a).count("paid") == 1 and b.status == "BOOKED", f"{r1.status_code} {r2.status_code}")

    # 4. unknown payment / order reference
    try:
        gw.capture("order_MOCKdoesnotexist")
        check("4", "mock refuses to capture for an unknown order", False)
    except MockGatewayError:
        check("4", "mock refuses to capture for an unknown order", True)
    n_pay = Payment.objects.count()
    r = gw.raw_event({"event": "payment.captured", "payload": {"payment": {"entity": {
        "id": "pay_MOCKunknown01", "order_id": "order_MOCKdoesnotexist", "amount": 100, "status": "captured"}}}})
    check("4", "a signed event for an unknown order is rejected (400) and records nothing",
          r.status_code == 400 and Payment.objects.count() == n_pay, f"{r.status_code} {body(r)}")
    r = gw.raw_event({"event": "payment.captured", "payload": {}}, signature="f" * 64)
    check("4", "an event with a forged signature is rejected (400)", r.status_code == 400)

    # 5. incorrect amount
    user, a, _ = alloc["s5"]
    order, _ = gw.create_booking_order(user.user, a.booking)
    try:
        gw.capture(order.razorpay_order_id, amount_paise=100)
        check("5", "mock refuses a capture for a different amount (as the gateway does)", False)
    except MockGatewayError:
        check("5", "mock refuses a capture for a different amount (as the gateway does)", True)
    for label, extra in (("amount", {"amount": 100, "currency": "INR"}),
                         ("currency", {"amount": int(order.total_amount * 100), "currency": "USD"}),
                         ("missing amount", {"currency": "INR"})):
        r = gw.raw_event({"event": "payment.captured", "payload": {"payment": {"entity": {
            "id": f"pay_MOCKmismatch{len(label)}", "order_id": order.razorpay_order_id, "status": "captured",
            **extra}}}})
        a, b = state(a)
        order.refresh_from_db()
        check("5", f"signed event with a wrong {label} is rejected (400) and settles nothing",
              r.status_code == 400 and b.status == "PENDING_PAYMENT" and order.status == "CREATED"
              and a.state == "awaiting_payment" and not Payment.objects.filter(payment_order=order).exists(),
              f"HTTP {r.status_code} {body(r)} booking {b.status}")
    r = gw.webhook(gw.capture(order.razorpay_order_id))
    a, b = state(a)
    check("5", "the correct event for the same order still settles it", r.status_code == 200 and b.status == "BOOKED")

    # 6. duplicate concurrent settlement attempts
    user, a, _ = alloc["s6a"]
    order, _ = gw.create_booking_order(user.user, a.booking)
    entity = gw.capture(order.razorpay_order_id)
    res = concurrent([lambda: gw.webhook(entity), lambda: gw.webhook(entity), lambda: gw.checkout_verify(user.token, entity)])
    a, b = state(a)
    codes = [getattr(x, "status_code", repr(x)) for x in res]
    check("6", "three concurrent deliveries of one payment: all handled, one payment row, one 'paid' event",
          all(c == 200 for c in codes) and Payment.objects.filter(payment_order=order).count() == 1
          and events(a).count("paid") == 1 and b.status == "BOOKED", codes)
    ext_ord = Booking.objects.filter(user__email="ext.academic@acceptance.invalid", status="PENDING_PAYMENT",
                                     external_special_allocation__isnull=True).first()
    if ext_ord is None:  # a booking awaiting payment, for two different captures on one order
        from acceptance_api import book

        # External self-bookings no longer wait for payment (checkout first); the OIC booking on the user's behalf
        # is the remaining path that creates one.
        _r, ext_ord = book(w.oic, w.xrd, free_slot(w.xrd, date(2026, 11, 13), 10), user_id=w.users[0].user.pk)
    order, _ = gw.create_booking_order(ext_ord.user, ext_ord)
    e1, e2 = gw.capture(order.razorpay_order_id), gw.capture(order.razorpay_order_id)
    res = concurrent([lambda: gw.webhook(e1), lambda: gw.webhook(e2)])
    ext_ord.refresh_from_db()
    rows = Payment.objects.filter(payment_order=order).count()
    check("6", "two different captures for one order concurrently: booking settled once",
          all(getattr(x, "status_code", 0) == 200 for x in res) and ext_ord.status == "BOOKED",
          [getattr(x, "status_code", repr(x)) for x in res])
    note("6", f"two captured payments for one order produced {rows} payment row(s); a second capture for an "
              "already-paid order is not recorded by settle_order_success (existing behaviour), so a real double "
              "charge would have to be found in the Razorpay dashboard / settlement reconciliation.")

    # 10. payment event associated with the wrong booking (before the clock moves past the deadlines)
    user, a, _ = alloc["s10"]
    other_user = w.users[(w.users.index(user) + 1) % 3]
    try:
        gw.create_booking_order(other_user.user, a.booking)
        check("10", "another user cannot open a payment order for this booking", False)
    except Exception as exc:
        check("10", "another user cannot open a payment order for this booking", "Forbidden" in str(exc), exc)
    order_a, _ = gw.create_booking_order(user.user, a.booking)
    _u, other_alloc, _ = alloc["s8"]  # still awaiting payment at this point
    b_other = Booking.objects.get(pk=other_alloc.booking_id)
    before_other = (b_other.status, b_other.amount_due, b_other.payment_settled_at)
    ent = gw.capture(order_a.razorpay_order_id)
    ent["notes"] = {**ent["notes"], "booking_id": str(b_other.pk)}
    r = gw.webhook(ent)
    a, b = state(a)
    b_other.refresh_from_db()
    other_alloc.refresh_from_db()
    check("10", "event whose notes name another booking settles only the order's own booking",
          r.status_code == 200 and b.status == "BOOKED" and a.state == "paid"
          and (b_other.status, b_other.amount_due, b_other.payment_settled_at) == before_other
          and other_alloc.state == "awaiting_payment", f"{b.status} other={b_other.status}")
    note("10", "a payment is bound by the gateway order id; the order row was bound to one booking and one user when "
               "it was created, and booking ids in event notes are ignored.")

    # 7. payment completed before the deadline, callback after it
    user, a, _ = alloc["s7a"]
    order, _ = gw.create_booking_order(user.user, a.booking)
    entity = gw.capture(order.razorpay_order_id, at=(a.payment_deadline_at - timedelta(minutes=2)).timestamp())
    clock(a.payment_deadline_at + timedelta(minutes=10))
    r = gw.webhook(entity)
    a, b = state(a)
    check("7", "captured before the deadline, callback 10 min after: accepted within the 15-min grace",
          r.status_code == 200 and a.state == "paid" and b.status == "BOOKED", f"{a.state} {b.status}")
    user, a, slot = alloc["s7b"]
    clock(a.approved_at + timedelta(hours=1))
    order, _ = gw.create_booking_order(user.user, a.booking)
    entity = gw.capture(order.razorpay_order_id, at=(a.payment_deadline_at - timedelta(minutes=2)).timestamp())
    clock(a.payment_deadline_at + timedelta(minutes=20))
    r = gw.webhook(entity)
    a, b = state(a)
    slot.refresh_from_db()
    check("7", "callback 20 min after the deadline: booking not confirmed; allocation expired; late payment recorded",
          r.status_code == 200 and a.state == "expired" and b.status == "CANCELLED" and a.late_payment_at is not None,
          f"{a.state} {b.status}")
    check("7", "money recorded on the booking for Finance; slot released", b.amount_due == 0 and
          Payment.objects.filter(payment_order=order).count() == 1 and slot.booking_id is None,
          f"{b.amount_due} {slot.status}")
    check("7", "audit trail: expired then late_payment", events(a)[-2:] == ["expired", "late_payment"], events(a))
    note("7", "the gateway's own capture time (before the deadline) is not used: lateness is judged by when the "
              "callback is processed (plus 15 min grace). A payment made in time whose callback is delayed more "
              "than 15 min is treated as late (policy decision; see report).")

    # 8. payment initiated before the deadline, completed after it
    user, a, _ = alloc["s8"]
    clock(a.approved_at + timedelta(hours=2))
    order, _ = gw.create_booking_order(user.user, a.booking)
    clock(a.payment_deadline_at + timedelta(minutes=30))
    r = gw.webhook(gw.capture(order.razorpay_order_id))
    a, b = state(a)
    check("8", "order opened before the deadline, paid 30 min after: not confirmed; late payment recorded",
          r.status_code == 200 and a.state == "expired" and b.status == "CANCELLED" and a.late_payment_at is not None,
          f"{a.state} {b.status}")
    try:
        gw.create_booking_order(user.user, a.booking)
        check("8", "a new order after the deadline is refused by the application", False)
    except Exception as exc:
        check("8", "a new order after the deadline is refused by the application", True, exc)

    # 9. expired booking followed by a late payment event
    user, a, slot = alloc["s9"]
    clock(a.approved_at + timedelta(hours=3))
    order, _ = gw.create_booking_order(user.user, a.booking)
    clock(a.payment_deadline_at + timedelta(minutes=5))
    res5 = tasks.expire_special_allocations()
    a, _b = state(a)
    check("9", "expiry task inside the grace leaves it awaiting payment", a.state == "awaiting_payment", res5)
    clock(a.payment_deadline_at + timedelta(minutes=40))
    res = tasks.expire_special_allocations()
    res_again = tasks.expire_special_allocations()
    a, b = state(a)
    slot.refresh_from_db()
    check("9", "expiry task expires it once; the second run changes nothing",
          a.state == "expired" and b.status == "CANCELLED" and events(a).count("expired") == 1, f"{res} {res_again}")
    check("9", "slot restored to AVAILABLE with no booking", slot.status == "AVAILABLE" and slot.booking_id is None,
          f"{slot.status} {slot.booking_id}")
    r = gw.webhook(gw.capture(order.razorpay_order_id))
    a, b = state(a)
    slot.refresh_from_db()
    check("9", "late payment event: booking stays CANCELLED, slot stays free, late payment recorded",
          r.status_code == 200 and b.status == "CANCELLED" and slot.booking_id is None and a.late_payment_at
          and events(a)[-1] == "late_payment", f"{b.status} {events(a)}")

    # R. expiry racing settlement (real threads, separate database connections)
    from iic_booking.external_services import special_payment

    for key in ("r1", "r2", "r3"):
        _user, a, slot = alloc[key]
        order = race_orders[key]
        clock(a.payment_deadline_at + timedelta(minutes=16))
        entity = gw.capture(order.razorpay_order_id)
        res = concurrent([lambda e=entity: gw.webhook(e), lambda pk=a.pk: special_payment.expire_one(pk)])
        a, b = state(a)
        slot.refresh_from_db()
        ev = events(a)
        check("R", f"{key}: settlement and expiry at the same time finish without error or deadlock",
              not any(isinstance(x, Exception) or x is None for x in res)
              and getattr(res[0], "status_code", 0) == 200, [repr(x)[:80] for x in res])
        check("R", f"{key}: one consistent outcome: expired once, late payment once, one payment row, slot free",
              a.state == "expired" and b.status == "CANCELLED" and ev.count("expired") == 1
              and ev.count("late_payment") == 1 and Payment.objects.filter(payment_order=order).count() == 1
              and slot.booking_id is None, f"{a.state} {b.status} {ev} slot={slot.status}/{slot.booking_id}")

    # global checks
    check("ALL", "no wallet balance changed (special allocations are paid by gateway only)", wallets() == wallets_before)
    check("ALL", "no gateway settlement record was created or fetched", PaymentSettlement.objects.count()
          == settlements_before)
    check("ALL", "no refund was issued (refund calls are refused by the mock and none recorded)",
          not __import__("iic_booking.payments.models", fromlist=["PaymentRefund"]).PaymentRefund.objects.exists())


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        RESULTS.append({"case": "RUN", "check": "script completed", "ok": False, "detail": traceback.format_exc()[-400:]})
    passed = sum(r["ok"] for r in RESULTS)
    print(f"\n{passed} passed, {len(RESULTS) - passed} failed, {len(RESULTS)} checks")
    (ROOT / ".acceptance_local" / "payment_scenarios_results.json").write_text(json.dumps(RESULTS, indent=1),
                                                                                encoding="utf-8")
    sys.exit(0 if passed == len(RESULTS) else 1)

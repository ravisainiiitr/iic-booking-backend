"""Repeated expiry-versus-settlement races on the local acceptance database (synthetic data, mock gateway).

Usage: ``extbook-local.ps1 restore`` then ``extbook-local.ps1 race [<rounds per timing>]`` (default 20).

For each round: a fresh special allocation with a gateway order opened before the deadline; then, with the simulated
clock at a chosen offset from the deadline, a signed ``payment.captured`` webhook and ``special_payment.expire_one``
start at the same moment on two threads (separate database connections). Timings:

* ``grace``    deadline + 10 min: the payment is on time (15-min grace); it must win, expiry must do nothing.
* ``late``     deadline + 16 min: both paths treat it as late; one expiry, one late-payment record.
* ``boundary`` deadline + 15 min: either outcome is valid, but it must be exactly one of the two.

Invariants checked every round: both threads finish (no deadlock / exception), at most one payment row for the order,
no duplicate ``paid`` / ``expired`` / ``late_payment`` events, booking and allocation states agree, the slot is
booked iff the booking is confirmed, and no wallet balance changes.
"""

import json
import sys
import time
import traceback
from collections import Counter
from datetime import date, timedelta

from decimal import Decimal

from acceptance_api import ROOT, Api, at, body, clock, free_slot, special_request
from django.db import connection
from django.db.models import Sum

from iic_booking.users.models import SubWallet

from iic_booking.equipment.models import Booking, DailySlot, Equipment
from iic_booking.external_services import special_payment
from iic_booking.external_services.models import SpecialAllocation
from iic_booking.payments.models import Payment
from mock_razorpay import MockGateway
from payment_scenarios import concurrent, events, wallets

STAFF = "/api/v1/external-booking/staff/special-requests/"
CHECKOUT = "/api/v1/external-booking/checkouts/"
TIMINGS = {"grace": timedelta(minutes=10), "late": timedelta(minutes=16), "boundary": timedelta(minutes=15)}
DAYS = [date(2026, 12, d) for d in (21, 22, 23, 24, 25)]


def main(rounds: int) -> int:
    with connection.cursor() as cur:
        cur.execute("select current_database(), host(inet_server_addr())")
        print("database:", cur.fetchone())
    clock(at(2026, 10, 28, 21, 10))
    users = [Api("ext.academic"), Api("ext.academic2"), Api("ext.industry")]
    oic = Api("oic")
    equipment = list(Equipment.objects.filter(code__in=["ACCXRD", "ACCSEM"]).order_by("code"))
    gw = MockGateway()
    wallets_before = wallets()
    limit_ok = open_request_limit_race(equipment[0])

    plan =[(name, i) for i in range(rounds) for name in TIMINGS]
    cases = []
    for n, (name, _i) in enumerate(plan):
        eq = equipment[n % len(equipment)]
        user = users[n % len(users)]
        slot = None
        for d in DAYS:
            try:
                slot = free_slot(eq, d, 9)
                break
            except AssertionError:
                continue
        if slot is None:
            raise SystemExit(f"ran out of free December slots after {n} rounds; use fewer rounds")
        start = slot.start_datetime
        r = special_request(user, eq, start)
        assert r.status_code == 201, body(r)
        rid = body(r)["id"]
        r = oic.post(f"{STAFF}{rid}/allocate/", {"slot_ids": [slot.pk]})
        assert r.status_code == 200, body(r)
        a = SpecialAllocation.objects.get(request_id=rid)
        order, _ = gw.create_booking_order(user.user, a.booking)
        cases.append((name, a.pk, slot.pk, order))
    print(f"prepared {len(cases)} allocations with open orders")

    outcomes = Counter()
    failures = []
    for name, alloc_id, slot_id, order in cases:
        a = SpecialAllocation.objects.get(pk=alloc_id)
        clock(a.payment_deadline_at + TIMINGS[name])
        entity = gw.capture(order.razorpay_order_id)
        started = time.monotonic()
        res = concurrent([lambda e=entity: gw.webhook(e), lambda pk=alloc_id: special_payment.expire_one(pk)])
        elapsed = time.monotonic() - started
        a.refresh_from_db()
        b = Booking.objects.get(pk=a.booking_id)
        slot = DailySlot.objects.get(pk=slot_id)
        ev = Counter(events(a))
        pays = Payment.objects.filter(payment_order=order).count()
        problems = []
        if any(x is None or isinstance(x, Exception) for x in res):
            problems.append(f"thread error {[repr(x)[:120] for x in res]}")
        elif getattr(res[0], "status_code", 0) != 200:
            problems.append(f"webhook HTTP {res[0].status_code} {body(res[0])}")
        if elapsed > 30:
            problems.append(f"slow ({elapsed:.1f}s): possible lock wait")
        if pays != 1:
            problems.append(f"{pays} payment rows")
        if any(ev[k] > 1 for k in ("paid", "expired", "late_payment")):
            problems.append(f"duplicate events {dict(ev)}")
        paid_ok = (a.state == "paid" and b.status == "BOOKED" and ev["paid"] == 1 and ev["expired"] == 0
                   and slot.booking_id == b.pk)
        late_ok = (a.state == "expired" and b.status == "CANCELLED" and ev["expired"] == 1
                   and ev["late_payment"] == 1 and a.late_payment_at is not None and slot.booking_id is None
                   and slot.status in ("AVAILABLE", "NOT_AVAILABLE", "UNDER_MAINTENANCE"))
        outcome = "paid" if paid_ok else "expired+late" if late_ok else "INCONSISTENT"
        if outcome == "INCONSISTENT":
            problems.append(f"state {a.state} / booking {b.status} / slot {slot.status},{slot.booking_id} / {dict(ev)}")
        if name == "grace" and outcome != "paid":
            problems.append(f"inside the grace the payment must win, got {outcome}")
        if name == "late" and outcome != "expired+late":
            problems.append(f"after the grace the booking must expire, got {outcome}")
        outcomes[(name, outcome)] += 1
        if problems:
            failures.append({"timing": name, "allocation": alloc_id, "problems": problems})

    wallet_ok = wallets() == wallets_before
    print("outcomes by timing:")
    for (name, outcome), n in sorted(outcomes.items()):
        print(f"  {name:9} {outcome:14} {n}")
    print(f"wallet balances unchanged: {wallet_ok}")
    print(f"rounds: {len(cases)}, rounds with problems: {len(failures)}")
    for f in failures[:20]:
        print("  PROBLEM", f)
    (ROOT / ".acceptance_local" / "race_results.json").write_text(json.dumps({
        "rounds": len(cases), "outcomes": {f"{k[0]}:{k[1]}": v for k, v in outcomes.items()},
        "wallet_unchanged": wallet_ok, "failures": failures}, indent=1), encoding="utf-8")
    checkout_ok = checkout_races(equipment)  # after the wallet check above: these races spend wallet money
    return 0 if not failures and wallet_ok and limit_ok and checkout_ok else 1


def _fund(api, eq, amount: Decimal) -> None:
    """Synthetic top-up of the sub-wallet the booking is charged to, through the ledger (local acceptance data only)."""
    from iic_booking.users.repositories.wallet_repository import WalletRepository

    sub, _ = WalletRepository.get_booking_wallet_target(api.user, eq.internal_department)
    sub.credit(amount, "Acceptance race funding (synthetic)")


def checkout_races(equipment) -> bool:
    """External booking checkout under real concurrency (each thread its own connection):

    * two users pay from their wallets for the same last slot at the same moment: exactly one booking, the other
      user's wallet untouched;
    * one wallet that covers only one booking pays two checkouts at the same moment: one booking, no overdraft.
    """
    from iic_booking.external_services.models import ExternalBookingCheckout

    D = Decimal

    a, b = Api("ext.academic"), Api("ext.academic2")
    xrd, sem = equipment[0], equipment[1]
    slot = free_slot(xrd, date(2026, 12, 3), 9)

    def start(api, eq, s):
        r = api.post(f"{CHECKOUT}", {"equipment_id": eq.pk, "slot_ids": [s.pk], "input_values": {"A": 1}})
        assert r.status_code == 201, body(r)
        return body(r)

    def pay(api, cid):
        return lambda: api.post(f"{CHECKOUT}{cid}/pay/", {"choice": "full_wallet"})

    ok = True
    # 1. last slot
    ca = start(a, xrd, slot)
    _fund(a, xrd, D(ca["total_amount"]) + D("10.00"))
    _fund(b, xrd, D(ca["total_amount"]) + D("10.00"))
    cb = start(b, xrd, slot)
    before = {u.user.pk: sub_balance(u) for u in (a, b)}
    res = concurrent([pay(a, ca["id"]), pay(b, cb["id"])])
    codes = sorted(getattr(r, "status_code", repr(r)) for r in res)
    slot.refresh_from_db()
    debited = [u for u in (a, b) if sub_balance(u) != before[u.user.pk]]
    one = codes[0] == 200 and codes[1] in (400, 409) and slot.booking_id is not None and len(debited) == 1
    print(f"checkout race, last slot (2 users at once): HTTP {codes}, wallets debited {len(debited)} -> "
          f"{'OK' if one else 'INCONSISTENT'}")
    ok &= one
    # 2. one wallet, two checkouts, money for one
    s1, s2 = free_slot(xrd, date(2026, 12, 3), 11), free_slot(sem, date(2026, 12, 3), 11)
    c1, c2 = start(a, xrd, s1), start(a, sem, s2)
    need = max(D(c1["total_amount"]), D(c2["total_amount"]))
    have = sub_balance(a)
    if have < need:
        _fund(a, xrd, need - have)
    elif have >= D(c1["total_amount"]) + D(c2["total_amount"]):
        print("checkout race, one wallet: balance already covers both; skipped")
        return ok
    res = concurrent([pay(a, c1["id"]), pay(a, c2["id"])])
    codes = sorted(getattr(r, "status_code", repr(r)) for r in res)
    confirmed = ExternalBookingCheckout.objects.filter(public_id__in=[c1["id"], c2["id"]], state="confirmed").count()
    one = codes[0] == 200 and codes[1] in (400, 409) and confirmed == 1 and sub_balance(a) >= 0
    print(f"checkout race, one wallet for two checkouts: HTTP {codes}, confirmed {confirmed}, balance "
          f"{sub_balance(a)} -> {'OK' if one else 'INCONSISTENT'}")
    return ok and one


def sub_balance(api):
    return SubWallet.objects.filter(wallet__user=api.user).aggregate(s=Sum("balance"))["s"]


def open_request_limit_race(eq) -> bool:
    """Five simultaneous submissions by one user with a limit of 3 open requests: exactly 3 accepted."""
    from iic_booking.external_services.models import ExternalServicesSettings

    from django.test import Client

    limit = ExternalServicesSettings.load().max_open_special_requests
    owner = Api("ext.industry")  # one sign-in: a new sign-in replaces the user's token
    clients = []
    for _ in range(limit + 2):
        c = Api.__new__(Api)
        c.email, c.user, c.token, c.client = owner.email, owner.user, owner.token, Client(HTTP_HOST="127.0.0.1")
        clients.append(c)
    starts = [at(2026, 12, 21, 9 + i) for i in range(len(clients))]
    res = concurrent([lambda c=c, s=s: special_request(c, eq, s) for c, s in zip(clients, starts, strict=True)])
    codes = sorted(getattr(r, "status_code", repr(r)) for r in res)
    accepted = [body(r)["id"] for r in res if getattr(r, "status_code", 0) == 201]
    refused = [body(r).get("code") for r in res if getattr(r, "status_code", 0) != 201]
    ok = len(accepted) == limit and all(code == "too_many_open_requests" for code in refused)
    print(f"open-request limit race ({len(clients)} at once, limit {limit}): HTTP {codes} -> "
          f"{'OK' if ok else 'INCONSISTENT'}")
    for rid in accepted:  # withdraw so they do not count against the race rounds
        clients[0].post(f"/api/v1/external-booking/special-requests/{rid}/withdraw/", {})
    return ok


if __name__ == "__main__":
    try:
        sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 20))
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(2)

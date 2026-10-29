"""Local-only mock Razorpay gateway for acceptance testing (never part of the deployed application).

Only the gateway boundary is replaced; the application's own code runs unchanged:

* ``razorpay_service.get_razorpay_client`` and ``razorpay.Client`` are patched, in this process only, to a fake client
  whose ``order.create`` returns a synthetic ``order_MOCK...`` id. ``payment.refund`` and ``settlement.all`` raise:
  no refund or settlement is ever issued.
* Synthetic key id / secret (``rzp_test_ACCEPTANCELOCAL...``) are set with ``override_settings`` for the duration of
  ``MockGateway.active()`` so the checkout signature check can run; they cannot authorise anything at Razorpay, and
  the acceptance settings block all outbound network access anyway.
* Payment events are delivered through the real endpoints: ``/api/payments/razorpay/webhook/`` (signed with the
  local webhook secret from the acceptance settings) and ``/api/payments/razorpay/verify/`` (checkout signature).
* Like the real gateway, the mock only captures payments for orders it created and only for the order amount;
  ``raw_event`` exists to test how the application treats events the gateway would never send.

Refuses to load unless the acceptance settings are active (``settings.ACCEPTANCE_LOCAL``).

CLI: ``extbook-local.ps1 pay <booking_id> [-Outcome success|failure] [-Clock ...]``
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
import uuid
from contextlib import ExitStack, contextmanager
from unittest import mock

SYNTHETIC_KEY_ID = "rzp_test_ACCEPTANCELOCAL"
SYNTHETIC_KEY_SECRET = "acceptance-local-checkout-secret-not-real"


class MockGatewayError(Exception):
    """The mock gateway refuses something the real gateway would refuse (unknown order, wrong amount, refund)."""


def assert_acceptance() -> None:
    from django.conf import settings

    if not getattr(settings, "ACCEPTANCE_LOCAL", False) or settings.SETTINGS_MODULE != "config.settings.acceptance_local":
        raise RuntimeError("mock_razorpay runs only under config.settings.acceptance_local")


class _Orders:
    def __init__(self, gateway):
        self._gw = gateway

    def create(self, data=None, **kwargs):
        order_id = f"order_MOCK{uuid.uuid4().hex[:14]}"
        order = {"id": order_id, "entity": "order", "amount": int(data["amount"]), "currency": data.get("currency", "INR"),
                 "receipt": data.get("receipt", ""), "status": "created", "notes": dict(data.get("notes") or {}),
                 "created_at": int(time.time())}
        self._gw.orders[order_id] = order
        return dict(order)


class _Refused:
    def __init__(self, what):
        self._what = what

    def __getattr__(self, name):
        def refuse(*args, **kwargs):
            raise MockGatewayError(f"the acceptance mock never performs {self._what}.{name}")
        return refuse


class FakeClient:
    def __init__(self, gateway, auth=None):
        self.order = _Orders(gateway)
        self.payment = _Refused("payment")  # refunds, captures via API, fetches
        self.settlement = _Refused("settlement")
        self.refund = _Refused("refund")


class MockGateway:
    def __init__(self):
        assert_acceptance()
        self.orders: dict[str, dict] = {}
        self.payments: dict[str, dict] = {}

    @contextmanager
    def active(self):
        """Inject the fake client and synthetic keys for this process while the block runs."""
        import razorpay
        from django.test import override_settings

        from iic_booking.payments import razorpay_service

        assert_acceptance()
        with ExitStack() as stack:
            stack.enter_context(override_settings(RAZORPAY_KEY_ID=SYNTHETIC_KEY_ID,
                                                  RAZORPAY_KEY_SECRET=SYNTHETIC_KEY_SECRET))
            stack.enter_context(mock.patch.object(razorpay, "Client", lambda auth=None, **kw: FakeClient(self, auth)))
            stack.enter_context(mock.patch.object(razorpay_service, "get_razorpay_client", lambda: FakeClient(self)))
            yield self

    # --- what the customer / gateway does -----------------------------------------------------------------------

    def create_booking_order(self, user, booking):
        """The real application order creation (refusals, fee breakup, PaymentOrder row) with the fake client."""
        from django.db import transaction

        from iic_booking.payments.razorpay_service import create_order

        with self.active(), transaction.atomic():
            order, response = create_order(user=user, purpose="BOOKING_SHORTFALL", booking=booking)
        return order, response

    def adopt(self, payment_order) -> None:
        """Know an order that the mock gateway created in another local process (``backend -MockGateway``)."""
        if not str(payment_order.razorpay_order_id).startswith("order_MOCK"):
            raise MockGatewayError("only orders created by the mock gateway can be paid locally")
        self.orders[payment_order.razorpay_order_id] = {
            "id": payment_order.razorpay_order_id, "amount": int(payment_order.total_amount * 100),
            "currency": payment_order.currency, "notes": {},
        }

    def capture(self, order_id: str, *, amount_paise: int | None = None, at: float | None = None) -> dict:
        """The gateway captures a payment for one of its orders, for exactly the order amount."""
        order = self.orders.get(order_id)
        if order is None:
            raise MockGatewayError(f"unknown order {order_id!r}")
        if amount_paise is not None and int(amount_paise) != order["amount"]:
            raise MockGatewayError(f"amount {amount_paise} does not match order amount {order['amount']}")
        pay_id = f"pay_MOCK{uuid.uuid4().hex[:14]}"
        entity = {"id": pay_id, "entity": "payment", "order_id": order_id, "amount": order["amount"],
                  "currency": order["currency"], "status": "captured", "method": "upi", "captured": True,
                  "notes": dict(order["notes"]), "created_at": int(at or time.time()),
                  "acquirer_data": {"rrn": f"MOCK{pay_id[-10:]}"}}
        self.payments[pay_id] = entity
        return entity

    def fail(self, order_id: str) -> dict:
        order = self.orders.get(order_id)
        if order is None:
            raise MockGatewayError(f"unknown order {order_id!r}")
        return {"id": f"pay_MOCK{uuid.uuid4().hex[:14]}", "entity": "payment", "order_id": order_id,
                "amount": order["amount"], "currency": order["currency"], "status": "failed",
                "error_code": "BAD_REQUEST_ERROR", "error_description": "Simulated failure (acceptance)",
                "notes": dict(order["notes"]), "created_at": int(time.time())}

    # --- deliveries to the application ---------------------------------------------------------------------------

    @staticmethod
    def _client():
        from django.test import Client

        return Client(HTTP_HOST="127.0.0.1")

    def raw_event(self, payload: dict, *, signature: str | None = None):
        """POST any event to the real webhook view, signed with the local webhook secret unless given."""
        from django.conf import settings

        body = json.dumps(payload).encode()
        if signature is None:
            signature = hmac.new(settings.RAZORPAY_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        return self._client().post("/api/payments/razorpay/webhook/", data=body, content_type="application/json",
                                   HTTP_X_RAZORPAY_SIGNATURE=signature)

    def webhook(self, entity: dict):
        event = "payment.captured" if entity["status"] == "captured" else "payment.failed"
        return self.raw_event({"entity": "event", "event": event, "payload": {"payment": {"entity": entity}},
                               "created_at": int(time.time())})

    def checkout_verify(self, token: str, entity: dict, *, signature: str | None = None):
        """What the browser checkout handler posts back after a successful payment."""
        with self.active():
            if signature is None:
                signature = hmac.new(SYNTHETIC_KEY_SECRET.encode(), f"{entity['order_id']}|{entity['id']}".encode(),
                                     hashlib.sha256).hexdigest()
            return self._client().post(
                "/api/payments/razorpay/verify/",
                data=json.dumps({"razorpay_order_id": entity["order_id"], "razorpay_payment_id": entity["id"],
                                 "razorpay_signature": signature}),
                content_type="application/json", HTTP_AUTHORIZATION=f"Token {token}",
            )


def _cli(booking_id: int, outcome: str) -> int:
    from iic_booking.equipment.models import Booking

    booking = Booking.objects.get(pk=booking_id)
    gw = MockGateway()
    try:
        order, _ = gw.create_booking_order(booking.user, booking)
    except Exception as exc:  # the application's own refusal (e.g. deadline passed, nothing due)
        print(f"order refused by the application: {exc}")
        return 3
    entity = gw.capture(order.razorpay_order_id) if outcome == "success" else gw.fail(order.razorpay_order_id)
    r = gw.webhook(entity)
    booking.refresh_from_db()
    order.refresh_from_db()
    print(f"simulated {outcome}: webhook HTTP {r.status_code}; order {order.status} (total {order.total_amount}); "
          f"booking {booking.pk} status {booking.status}, amount_due {booking.amount_due}")
    return 0 if r.status_code == 200 else 2


def _cli_checkout(public_id: str, outcome: str) -> int:
    """Pay (or fail) the online part of an external booking checkout opened in the browser."""
    from iic_booking.external_services.models import ExternalBookingCheckout

    checkout = ExternalBookingCheckout.objects.select_related("payment_order").get(public_id=public_id)
    if checkout.payment_order is None:
        print(f"checkout {public_id} has no payment order (state {checkout.state}); choose an online option first")
        return 3
    gw = MockGateway()
    gw.adopt(checkout.payment_order)
    order_id = checkout.payment_order.razorpay_order_id
    entity = gw.capture(order_id) if outcome == "success" else gw.fail(order_id)
    r = gw.webhook(entity)
    checkout.refresh_from_db()
    print(f"simulated {outcome}: webhook HTTP {r.status_code}; checkout {checkout.state}"
          f"{' booking ' + str(checkout.booking_id) if checkout.booking_id else ''}"
          f"{' (' + checkout.outcome_note + ')' if checkout.outcome_note else ''}")
    return 0 if r.status_code == 200 else 2


if __name__ == "__main__":
    from _bootstrap import setup

    setup()
    outcome_arg = sys.argv[2] if len(sys.argv) > 2 else "success"
    if sys.argv[1].isdigit():
        sys.exit(_cli(int(sys.argv[1]), outcome_arg))
    sys.exit(_cli_checkout(sys.argv[1], outcome_arg))

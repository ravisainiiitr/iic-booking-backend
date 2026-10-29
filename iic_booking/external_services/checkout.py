"""External self-booking checkout: price the booking, let the user choose how to pay, and create the booking only when
the payment is confirmed. Once the user chooses an online payment the slots are held for them (``slot_holds``) for
``PAYMENT_WINDOW`` or until the payment is confirmed, whichever is earlier.

Amounts (server-side only; the client sends a choice, never an amount):

* ``R`` total charge, from the normal booking pipeline (``checkout_context.QUOTE``).
* ``W`` eligible wallet balance: what the booking sub-wallet may spend now (existing minimum-balance and block rules)
  minus the wallet parts of the user's other checkouts that are awaiting online payment on the same sub-wallet.
* Options: ``W = 0`` -> online only; ``0 < W < R`` -> online ``R`` or wallet ``W`` + online ``R - W``;
  ``W >= R`` -> online ``R`` or wallet ``R``. The online part is charged with the existing gateway convenience fee.

Confirmation always runs the normal booking pipeline again (``checkout_context.CONFIRM``) inside one transaction:
slots locked and re-checked (availability, maintenance, holidays, window, quotas, limits), the price compared with
the quote, and the wallet part debited in the same transaction as the booking. A wallet-only payment debits nothing
until that transaction succeeds. A gateway payment is applied by the existing Razorpay settlement path
(``razorpay_service.settle_order_success`` -> ``settle_checkout_order``); when the booking can no longer be made the
captured payment stays recorded (``Payment`` row, order ``PAID``) and the checkout is marked ``paid_not_confirmed`` for
Finance to refund under the existing rules. Nothing is refunded automatically.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any, Optional

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from . import checkout_context
from .models import CheckoutChoice, CheckoutState, ExternalBookingCheckout

logger = logging.getLogger(__name__)

PAYMENT_WINDOW = timedelta(minutes=15)
OPEN_STATES = (CheckoutState.QUOTED, CheckoutState.AWAITING_PAYMENT)
FORWARDED_KEYS = frozenset({
    "slot_ids", "number_of_samples", "input_values", "notes", "selected_parameters", "sample_return_after_analysis",
    "atmosphere_sensitive_sample", "own_material", "print_analysis_id", "print_analysis_batch_id",
    "laser_cut_batch_id", "start_time", "end_time",
})
ZERO = Decimal("0.00")


class CheckoutError(Exception):
    def __init__(self, message: str, *, status: int = 400, code: str = "checkout_error", extra: dict | None = None):
        super().__init__(message)
        self.message, self.status, self.code, self.extra = message, status, code, extra or {}

    def payload(self) -> dict:
        return {"error": self.message, "code": self.code, **self.extra}


def _money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def _run_pipeline(user, equipment_id: int, body: dict, ctx: checkout_context.CheckoutContext) -> tuple[int, dict]:
    """The normal booking pipeline as ``user`` for ``body``, under the checkout context."""
    from iic_booking.equipment.api_views import _book_equipment_impl
    from iic_booking.research_copilot.services.v2.mutations.domain_bridge import _as_data, _drf_json_post

    safe = {k: v for k, v in (body or {}).items() if k in FORWARDED_KEYS}
    safe["waitlist_on_failure"] = False
    request = _drf_json_post(user=user, path=f"/api/equipments/{equipment_id}/book/", body=safe)
    with checkout_context.active(ctx):
        return _as_data(_book_equipment_impl(request, equipment_id))


def _quote(user, equipment_id: int, body: dict) -> dict:
    ctx = checkout_context.CheckoutContext(mode=checkout_context.QUOTE)
    code, data = _run_pipeline(user, equipment_id, body, ctx)
    if code != 200 or not ctx.quote:
        err = data.get("error") or "This booking cannot be made."
        raise CheckoutError(err, status=409 if code == 409 else 400, code=data.get("code") or "booking_not_possible",
                            extra={k: v for k, v in data.items() if k not in ("error", "code")})
    return ctx.quote


# --- wallet ----------------------------------------------------------------------------------------------------------

def _reserved_elsewhere(checkout: ExternalBookingCheckout) -> Decimal:
    """Wallet parts of the user's other checkouts still awaiting online payment on the same sub-wallet."""
    total = (
        ExternalBookingCheckout.objects.filter(
            subwallet_id=checkout.subwallet_id, state=CheckoutState.AWAITING_PAYMENT, expires_at__gt=timezone.now()
        )
        .exclude(pk=checkout.pk)
        .aggregate(s=Sum("wallet_amount"))["s"]
    )
    return _money(total)


def eligible_wallet(checkout: ExternalBookingCheckout, *, lock: bool = False) -> Decimal:
    from iic_booking.users.models import SubWallet
    from iic_booking.users.wallet_credit_facility import wallet_booking_block_message, wallet_max_spendable_on_subwallet

    if not checkout.subwallet_id:
        return ZERO
    qs = SubWallet.objects.select_for_update() if lock else SubWallet.objects
    sub = qs.get(pk=checkout.subwallet_id)
    if wallet_booking_block_message(sub):
        return ZERO
    return max(ZERO, _money(wallet_max_spendable_on_subwallet(sub)) - _reserved_elsewhere(checkout))


def options(total: Decimal, wallet: Decimal, *, internal: bool = False) -> list[dict]:
    """Payment options by wallet balance. Internal users reach a checkout only when their wallet falls short, and
    are offered the whole booking online (their wallet is left as it is)."""
    from iic_booking.payments.fees import compute_fee_breakup

    def option(choice, wallet_part: Decimal) -> dict:
        gateway = _money(total - wallet_part)
        fee = compute_fee_breakup(gateway) if gateway > 0 else None
        return {
            "choice": choice,
            "label": CheckoutChoice(choice).label,
            "wallet_amount": f"{wallet_part:.2f}",
            "gateway_amount": f"{gateway:.2f}",
            "gateway_fee": f"{(fee.convenience_fee + fee.fee_gst) if fee else ZERO:.2f}",
            "gateway_total": f"{fee.total_amount if fee else ZERO:.2f}",
            "wallet_after": f"{_money(wallet - wallet_part):.2f}",
        }

    out = [option(CheckoutChoice.FULL_GATEWAY, ZERO)]
    if internal:
        return out
    if ZERO < wallet < total:
        out.append(option(CheckoutChoice.WALLET_PLUS_GATEWAY, wallet))
    elif wallet >= total > ZERO:
        out.append(option(CheckoutChoice.FULL_WALLET, total))
    return out


# --- read ------------------------------------------------------------------------------------------------------------

def _expire_if_due(checkout: ExternalBookingCheckout) -> None:
    if checkout.state == CheckoutState.AWAITING_PAYMENT and checkout.expires_at and checkout.expires_at <= timezone.now():
        ExternalBookingCheckout.objects.filter(pk=checkout.pk, state=CheckoutState.AWAITING_PAYMENT).update(
            state=CheckoutState.EXPIRED, updated_at=timezone.now()
        )
        checkout.refresh_from_db()


def payload(checkout: ExternalBookingCheckout) -> dict[str, Any]:
    def iso(v):
        return timezone.localtime(v).isoformat() if v else None

    total = _money(checkout.total_amount)
    body: dict[str, Any] = {
        "id": str(checkout.public_id),
        "state": checkout.state,
        "state_label": checkout.get_state_display(),
        "summary": checkout.summary,
        "total_amount": f"{total:.2f}",
        "choice": checkout.choice or None,
        "wallet_amount": f"{_money(checkout.wallet_amount):.2f}",
        "gateway_amount": f"{_money(checkout.gateway_amount):.2f}",
        "expires_at": iso(checkout.expires_at),
        "created_at": iso(checkout.created_at),
        "confirmed_at": iso(checkout.confirmed_at),
        # Held for this user while the online payment is open (slot_holds), until expires_at.
        "slot_held": bool(checkout.state == CheckoutState.AWAITING_PAYMENT and checkout.expires_at
                          and checkout.expires_at > timezone.now()),
    }
    if checkout.state == CheckoutState.QUOTED:
        wallet = eligible_wallet(checkout)
        body["wallet_balance"] = f"{wallet:.2f}"
        body["options"] = options(total, wallet, internal=is_internal(checkout))
    order = checkout.payment_order
    if order is not None:
        from iic_booking.payments.models import PaymentStatus

        paid = order.payments.filter(status=PaymentStatus.CAPTURED).order_by("created_at").first()
        body["payment"] = {
            "order_status": order.status,
            "gateway_total": f"{order.total_amount:.2f}",
            "convenience_fee": f"{order.convenience_fee:.2f}",
            "fee_gst": f"{order.fee_gst:.2f}",
            "razorpay_order_id": order.razorpay_order_id,
            "razorpay_payment_id": paid.razorpay_payment_id if paid else None,
            "method": paid.method if paid else None,
            "paid_at": iso(paid.created_at) if paid else None,
        }
    if checkout.booking_id:
        b = checkout.booking
        body["booking"] = {
            "id": b.pk,
            "reference": getattr(b, "virtual_booking_id", "") or str(b.pk),
            "status": b.status,
        }
    if checkout.state == CheckoutState.PAID_NOT_CONFIRMED:
        body["message"] = (
            "Your payment was received, but the booking could not be confirmed (the slot is no longer available or "
            "the booking changed). The payment is recorded and will be refunded under the institute's refund rules."
        )
    return body


def get_for_user(user, public_id) -> ExternalBookingCheckout:
    try:
        checkout = ExternalBookingCheckout.objects.select_related("payment_order", "booking").get(
            public_id=public_id, user=user
        )
    except (ExternalBookingCheckout.DoesNotExist, ValueError, TypeError):
        raise CheckoutError("Checkout not found.", status=404, code="not_found") from None
    _expire_if_due(checkout)
    return checkout


# --- create / choose / cancel ----------------------------------------------------------------------------------------

def _summary(equipment, body: dict, quote: dict) -> dict:
    return {
        "equipment_id": equipment.pk,
        "equipment_name": equipment.name,
        "equipment_code": getattr(equipment, "code", ""),
        "slots": [
            {"start": timezone.localtime(s).isoformat(), "end": timezone.localtime(e).isoformat()}
            for s, e in quote["slots"]
        ],
        "number_of_samples": body.get("number_of_samples") or 1,
        "total_time_minutes": quote.get("total_time_minutes"),
        "charge_breakdown": [
            {"description": str(row.get("description", "")), "amount": f"{_money(row.get('amount')):.2f}"}
            for row in (quote.get("charge_breakdown") or []) if isinstance(row, dict)
        ],
        "total": f"{quote['total_charge']:.2f}",
    }


def is_internal(checkout_or_user) -> bool:
    from iic_booking.users.models.user_type import UserType

    user = getattr(checkout_or_user, "user", checkout_or_user)
    return not UserType.is_external_user(getattr(user, "user_type", "") or "")


def _internal_wallet_short(quote: dict) -> bool:
    """An internal user may pay online only when their wallet cannot cover the booking and is not blocked (a
    suspended account stays suspended: no online way around it)."""
    from iic_booking.users.models import SubWallet
    from iic_booking.users.wallet_credit_facility import wallet_booking_block_message, wallet_max_spendable_on_subwallet

    sub = SubWallet.objects.filter(pk=quote.get("subwallet_id")).first() if quote.get("subwallet_id") else None
    if sub is not None and wallet_booking_block_message(sub):
        return False
    spendable = _money(wallet_max_spendable_on_subwallet(sub)) if sub is not None else ZERO
    return spendable < _money(quote["total_charge"])


def create(user, equipment_id: int, body: dict) -> ExternalBookingCheckout:
    from iic_booking.equipment.models import Equipment
    from iic_booking.users.models.user_type import UserType

    internal = not UserType.is_external_user(getattr(user, "user_type", "") or "")
    equipment = Equipment.objects.filter(pk=equipment_id).first()
    if equipment is None:
        raise CheckoutError("Equipment not found.", status=404, code="not_found")
    clean = {k: v for k, v in (body or {}).items() if k in FORWARDED_KEYS}
    quote = _quote(user, equipment.pk, clean)
    if quote["total_charge"] <= 0:
        raise CheckoutError("This booking has no charge; book it directly.", code="no_charge")
    if internal and not _internal_wallet_short(quote):
        # Internal users book from their wallet; the online payment is only offered when it falls short.
        raise CheckoutError("Your wallet covers this booking; book it directly.", status=403, code="not_external")
    # Earlier unpaid quotes for the same equipment are superseded (they hold nothing).
    ExternalBookingCheckout.objects.filter(user=user, equipment=equipment, state=CheckoutState.QUOTED).update(
        state=CheckoutState.CANCELLED, outcome_note="Superseded by a newer checkout.", updated_at=timezone.now()
    )
    return ExternalBookingCheckout.objects.create(
        public_id=uuid.uuid4(),
        user=user,
        equipment=equipment,
        request_body=clean,
        slot_ids=quote["slot_ids"],
        summary=_summary(equipment, clean, quote),
        total_amount=quote["total_charge"],
        subwallet_id=quote.get("subwallet_id"),
    )


def _requote(checkout: ExternalBookingCheckout) -> None:
    """Re-check the slots and price right before taking money; nothing is held."""
    quote = _quote(checkout.user, checkout.equipment_id, checkout.request_body)
    if quote["total_charge"] != _money(checkout.total_amount):
        checkout.total_amount = quote["total_charge"]
        checkout.summary = _summary(checkout.equipment, checkout.request_body, quote)
        checkout.save(update_fields=["total_amount", "summary", "updated_at"])
        raise CheckoutError("The booking charge has changed. Please review the new amount.", status=409,
                            code="price_changed")


def choose(user, public_id, choice: str) -> dict:
    """Apply the user's payment choice. Returns ``{"checkout": ..., "order": ...}`` (order only for online payment)."""
    if choice not in CheckoutChoice.values:
        raise CheckoutError("Unknown payment option.", code="invalid_choice")
    current = get_for_user(user, public_id)  # 404 for other users, lazy expiry
    if current.state == CheckoutState.QUOTED:
        _requote(current)  # outside the transaction below: no slot lock is held while the gateway is called
    with transaction.atomic():
        checkout = ExternalBookingCheckout.objects.select_for_update().get(public_id=public_id, user=user)
        if checkout.state == CheckoutState.AWAITING_PAYMENT and checkout.choice == choice and checkout.payment_order_id:
            # Retry of the same choice (checkout closed, page reloaded): reuse the open order.
            return {"checkout": checkout, "order": _order_params(checkout)}
        if checkout.state == CheckoutState.CONFIRMED:
            return {"checkout": checkout, "order": None}
        if checkout.state != CheckoutState.QUOTED:
            raise CheckoutError("This checkout can no longer be paid. Please start the booking again.",
                                status=409, code="checkout_closed")
        wallet = eligible_wallet(checkout, lock=True)  # locks the sub-wallet: concurrent choices serialise
        total = _money(checkout.total_amount)
        allowed = {o["choice"]: o for o in options(total, wallet, internal=is_internal(checkout))}
        if choice not in allowed:
            raise CheckoutError("That payment option is not available for your wallet balance.", status=409,
                                code="option_unavailable", extra={"options": list(allowed.values())})
        wallet_part = _money(allowed[choice]["wallet_amount"])
        checkout.choice = choice
        checkout.wallet_amount = wallet_part
        checkout.gateway_amount = _money(total - wallet_part)
        if choice == CheckoutChoice.FULL_WALLET:
            checkout.save(update_fields=["choice", "wallet_amount", "gateway_amount", "updated_at"])
            _confirm(checkout, source="wallet")
            return {"checkout": checkout, "order": None}
        # The slots are held for this user from here (slot_holds): lock the slot rows so two users choosing at the
        # same moment serialise, and refuse if someone else's hold got there first.
        from iic_booking.equipment.models import DailySlot

        from .slot_holds import HELD_MESSAGE, held_slot_ids

        list(DailySlot.objects.select_for_update().filter(id__in=checkout.slot_ids).values_list("id", flat=True))
        if held_slot_ids(checkout.slot_ids, holder_user_id=checkout.user_id):
            raise CheckoutError(HELD_MESSAGE, status=409, code="slot_held")
        # The wallet part now counts against the balance offered to the user's other checkouts (until paid or expired).
        checkout.state = CheckoutState.AWAITING_PAYMENT
        checkout.expires_at = timezone.now() + PAYMENT_WINDOW
        checkout.save(update_fields=["choice", "wallet_amount", "gateway_amount", "state", "expires_at", "updated_at"])

    from iic_booking.payments.models import PaymentPurpose
    from iic_booking.payments.razorpay_service import create_order

    try:
        with transaction.atomic():
            checkout = ExternalBookingCheckout.objects.select_for_update().get(pk=checkout.pk)
            order, _response = create_order(user=user, purpose=PaymentPurpose.BOOKING_SHORTFALL, checkout=checkout)
            checkout.payment_order = order
            checkout.save(update_fields=["payment_order", "updated_at"])
    except Exception as exc:  # noqa: BLE001 - RazorpayServiceError, gateway or network failure
        # No order was created: back to the choice screen (nothing was charged or debited).
        ExternalBookingCheckout.objects.filter(pk=checkout.pk, payment_order__isnull=True).update(
            state=CheckoutState.QUOTED, choice="", wallet_amount=ZERO, gateway_amount=ZERO, expires_at=None,
            updated_at=timezone.now(),
        )
        logger.warning("external checkout %s: payment order not created (%s)", checkout.pk, type(exc).__name__)
        raise CheckoutError("Online payment could not be started. Please try again.", status=502,
                            code="gateway_unavailable") from exc
    return {"checkout": checkout, "order": _order_params(checkout)}


def _order_params(checkout: ExternalBookingCheckout) -> dict:
    from django.conf import settings

    order = checkout.payment_order
    key_id = getattr(settings, "RAZORPAY_KEY_ID", "") or ""
    return {
        "order_id": order.razorpay_order_id,
        "amount": int((order.total_amount * 100).quantize(Decimal("1"))),
        "currency": order.currency,
        "key": key_id,
        "breakup": {
            "base_amount": f"{order.base_amount:.2f}",
            "convenience_fee": f"{order.convenience_fee:.2f}",
            "fee_gst": f"{order.fee_gst:.2f}",
            "total_amount": f"{order.total_amount:.2f}",
        },
    }


def cancel(user, public_id) -> ExternalBookingCheckout:
    get_for_user(user, public_id)
    with transaction.atomic():
        checkout = ExternalBookingCheckout.objects.select_for_update().get(public_id=public_id, user=user)
        if checkout.state in OPEN_STATES:
            checkout.state = CheckoutState.CANCELLED
            checkout.outcome_note = "Cancelled by the user before payment."
            checkout.save(update_fields=["state", "outcome_note", "updated_at"])
            # The gateway order is left open on Razorpay's side; a payment that still arrives is applied (or recorded)
            # by settle_checkout_order like any other.
    return checkout


def expire_overdue(now=None) -> int:
    """Close checkouts whose online-payment window passed (frees the wallet part they counted against)."""
    now = now or timezone.now()
    return ExternalBookingCheckout.objects.filter(
        state=CheckoutState.AWAITING_PAYMENT, expires_at__lte=now
    ).update(state=CheckoutState.EXPIRED, updated_at=now)


# --- confirmation ----------------------------------------------------------------------------------------------------

def _confirm(checkout: ExternalBookingCheckout, *, source: str) -> Optional[str]:
    """Create the booking through the pipeline (caller holds the checkout row lock). ``None`` on success, else why not."""
    from iic_booking.equipment.models import Booking

    ctx = checkout_context.CheckoutContext(
        mode=checkout_context.CONFIRM, expected_total=_money(checkout.total_amount),
        wallet_amount=_money(checkout.wallet_amount),
    )
    body = dict(checkout.request_body)
    body["slot_ids"] = list(checkout.slot_ids)
    try:
        with transaction.atomic():
            code, data = _run_pipeline(checkout.user, checkout.equipment_id, body, ctx)
    except Exception:  # noqa: BLE001 - never lose track of a payment because the booking step failed
        logger.exception("external checkout %s: booking step failed", checkout.pk)
        code, data = 500, {"error": "The booking could not be created."}
    booking_pk = data.get("real_booking_id") if code == 201 else None
    if booking_pk is None:
        reason = str(data.get("error") or "The booking could not be created.")[:500]
        if source == "wallet":
            raise CheckoutError(reason, status=409, code="booking_not_possible")
        checkout.state = CheckoutState.PAID_NOT_CONFIRMED
        checkout.outcome_note = reason
        checkout.save(update_fields=["state", "outcome_note", "updated_at"])
        logger.warning("external checkout %s paid but not confirmed: %s", checkout.pk, reason)
        return reason
    booking = Booking.objects.select_for_update().get(pk=booking_pk)
    now = timezone.now()
    if checkout.gateway_amount > 0:
        booking.payment_settled_at = now
        booking.save(update_fields=["payment_settled_at", "updated_at"])
    checkout.booking = booking
    checkout.state = CheckoutState.CONFIRMED
    checkout.confirmed_at = now
    checkout.outcome_note = ""
    checkout.save(update_fields=["booking", "state", "confirmed_at", "outcome_note", "updated_at"])
    return None


def settle_checkout_order(order) -> Optional[str]:
    """Called by the Razorpay settlement path (order row locked) for an order with no booking.

    Returns ``None`` when the order belongs to no checkout, ``""`` when the booking is confirmed (now or earlier), and
    the reason otherwise (payment recorded, booking not confirmed).
    """
    checkout = ExternalBookingCheckout.objects.select_for_update().filter(payment_order=order).first()
    if checkout is None:
        return None
    if checkout.state == CheckoutState.CONFIRMED:
        return ""
    if checkout.state == CheckoutState.PAID_NOT_CONFIRMED:
        return checkout.outcome_note or "Paid; booking not confirmed."
    if _money(order.base_amount) != _money(checkout.gateway_amount):
        reason = "Gateway order amount does not match the checkout."
        checkout.state = CheckoutState.PAID_NOT_CONFIRMED
        checkout.outcome_note = reason
        checkout.save(update_fields=["state", "outcome_note", "updated_at"])
        return reason
    reason = _confirm(checkout, source="gateway")
    if reason is None:
        order.booking = checkout.booking
        order.save(update_fields=["booking", "updated_at"])
        return ""
    return reason

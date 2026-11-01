"""Module 4: refunds when the lab is at fault (equipment failure, analysis not possible, lab disruption).

Amounts used below (paid 1298 = 1000 analysis + 100 return shipping + 198 GST at 18%):
* no invoice requested: service + shipping + their GST                   -> 1000 + 100 + 198 = 1298
* invoice requested:    service + shipping base, no GST (credit note)    -> 1000 + 100       = 1100
* partial 50%, no invoice: half the service + its GST, shipping kept      -> 500 + 90         = 590
* partial 50%, invoice:    half the service, no GST                       -> 500
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from iic_booking.equipment.models import BookingStatus, DailySlot, EquipmentManager
from iic_booking.external_services import refunds
from iic_booking.external_services.models import ExternalRefund, RefundOrigin, SricInvoiceRequest
from iic_booking.users.models.user_type import UserType

from .conftest import client_for, make_user
from .test_refund_policy import WITH_SHIPPING, _booking, _wallet, policy, user  # noqa: F401

pytestmark = pytest.mark.django_db
API = "/api/v1/external-booking/"
REASON = "The detector failed during the run; no data could be recorded."


def _shipped(factory, user, hours=72, **fields):
    return _booking(factory, user, hours, breakdown=WITH_SHIPPING, total="1298.00", shipping="100.00", **fields)


def _oic(booking):
    oic = make_user(UserType.MANAGER, is_staff=True)
    EquipmentManager.objects.create(equipment=booking.equipment, manager=oic)
    return oic


def _invoice(booking):
    SricInvoiceRequest.objects.create(booking=booking, trigger="early", snapshot={})


def _refund(actor, booking, **body):
    payload = {"kind": "equipment_failure", "reason": REASON, "portion_percent": 100, **body}
    return client_for(actor).post(f"{API}bookings/{booking.pk}/lab-failure-refund/", payload, format="json")


# --- the calculation -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("invoice,portion,refund,gst,shipping_back", [
    (False, 100, "1298.00", "198.00", "118.00"),   # 4.1: everything back, shipping with its GST
    (True, 100, "1100.00", "0.00", "100.00"),      # 4.2: base amounts only, shipping base only
    (False, 50, "590.00", "90.00", "0.00"),        # partial: half the service charges, shipping kept
    (True, 50, "500.00", "0.00", "0.00"),
])
def test_lab_failure_quote(policy, factory, user, invoice, portion, refund, gst, shipping_back):
    b = _shipped(factory, user)
    if invoice:
        _invoice(b)
    q = client_for(_oic(b)).get(f"{API}bookings/{b.pk}/lab-failure-refund/?portion={portion}").json()
    assert (q["refund"], q["refund_gst"], q["shipping_refunded"]) == (refund, gst, shipping_back)
    assert q["gst_refunded"] is (not invoice) and q["invoice_requested"] is invoice
    assert q["service_refunded"] == ("1000.00" if portion == 100 else "500.00")


def test_a_user_cancelling_after_a_lab_disruption_gets_shipping_back(policy, factory, user):
    b = _shipped(factory, user, 2, status=BookingStatus.DISRUPTION_PENDING)  # within 48 hours (4.3)
    q = refunds.cancellation_quote(b)
    assert (q["tier"], q["refund"], q["shipping_refunded"]) == ("disruption", "1298.00", "118.00")
    _invoice(b)
    q = refunds.cancellation_quote(b)
    assert (q["refund"], q["refund_gst"], q["shipping_refunded"]) == ("1100.00", "0.00", "100.00")


def test_an_ordinary_cancellation_still_keeps_the_shipping(policy, factory, user):
    q = refunds.cancellation_quote(_shipped(factory, user, 72))
    assert (q["tier"], q["refund"], q["shipping_refunded"]) == ("free", "1180.00", "0.00")


# --- making the refund -----------------------------------------------------------------------------------------------

def test_full_lab_failure_refund_goes_to_the_wallet_and_closes_the_booking(policy, factory, user):
    b = _shipped(factory, user)
    r = _refund(_oic(b), b, expected_refund="1298.00")
    assert r.status_code == 200, r.content
    assert _wallet(user, factory) == Decimal("1298.00")
    refund = ExternalRefund.objects.get(booking=b)
    assert refund.origin == RefundOrigin.LAB_FAILURE and refund.tier == "lab_failure"
    assert refund.shipping_refunded == Decimal("118.00") and refund.portion_percent == 100
    assert refund.reason.startswith("Equipment failure: The detector failed") and refund.wallet_transaction_id
    b.refresh_from_db()
    assert b.status == BookingStatus.REFUNDED and not DailySlot.objects.filter(booking=b).exists()
    assert b.events.filter(event_type="REFUNDED").exists()


def test_partial_refund_keeps_the_booking_and_refunds_that_share(policy, factory, user):
    b = _shipped(factory, user)
    _invoice(b)
    r = _refund(_oic(b), b, kind="analysis_unsuccessful", portion_percent=50, expected_refund="500.00")
    assert r.status_code == 200, r.content
    assert _wallet(user, factory) == Decimal("500.00")
    b.refresh_from_db()
    assert b.status == BookingStatus.BOOKED and DailySlot.objects.filter(booking=b).exists()
    refund = ExternalRefund.objects.get(booking=b)
    assert (refund.refund_gst, refund.gst_refunded, refund.portion_percent) == (Decimal("0.00"), False, 50)


def test_a_booking_is_never_refunded_twice(policy, factory, user):
    b = _shipped(factory, user)
    oic = _oic(b)
    assert _refund(oic, b, portion_percent=50).status_code == 200
    again = _refund(oic, b)
    assert again.status_code == 409 and again.json()["code"] == "already_refunded"
    by_user = client_for(user).post(f"{API}bookings/{b.pk}/cancel/", {"destination": "wallet"}, format="json")
    assert by_user.status_code == 409
    assert _wallet(user, factory) == Decimal("590.00")


def test_a_changed_amount_is_never_applied_silently(policy, factory, user):
    b = _shipped(factory, user)
    r = _refund(_oic(b), b, expected_refund="1180.00")
    assert r.status_code == 409 and r.json()["code"] == "quote_changed"
    assert not ExternalRefund.objects.filter(booking=b).exists()


@pytest.mark.parametrize("body,code", [
    ({"kind": "whatever"}, "invalid_kind"),
    ({"reason": "broken"}, "reason_required"),
    ({"portion_percent": 0}, "invalid_portion"),
    ({"portion_percent": 120}, "invalid_portion"),
])
def test_kind_reason_and_share_are_checked(policy, factory, user, body, code):
    b = _shipped(factory, user)
    r = _refund(_oic(b), b, **body)
    assert r.status_code == 400 and r.json()["code"] == code


# --- who may refund ----------------------------------------------------------------------------------------------------

def test_only_the_oic_accounts_in_charge_or_main_admin_may_refund(policy, factory, user):
    b = _shipped(factory, user)
    other_oic = make_user(UserType.MANAGER, is_staff=True)
    EquipmentManager.objects.create(equipment=factory.equipment(), manager=other_oic)
    for outsider in (user, make_user(UserType.STUDENT), other_oic):
        assert _refund(outsider, b).status_code == 403
    accounts = make_user(UserType.FINANCE, department=b.equipment.internal_department)
    assert client_for(accounts).get(f"{API}bookings/{b.pk}/lab-failure-refund/").status_code == 200
    admin = make_user(UserType.ADMIN, is_staff=True)
    assert _refund(admin, b, portion_percent=50).status_code == 200


def test_not_available_while_the_refund_policy_is_off(factory, user):
    b = _shipped(factory, user)
    r = _refund(_oic(b), b)
    assert r.status_code == 403 and r.json()["code"] == "not_available"


# --- the generic staff refund must not credit GST that SRIC settles ------------------------------------------------------

def test_generic_refund_refuses_a_booking_with_an_invoice_request(policy, factory, user):
    from iic_booking.equipment.api_views import refund_booking_internal

    b = _shipped(factory, user)
    _invoice(b)
    with pytest.raises(ValueError, match="Refund for lab failure"):
        refund_booking_internal(b, "test", None)
    assert _wallet(user, factory) == Decimal("0.00")

"""31 Oct 2026 cancellation and refund policy for external users (``external_services.refunds``)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core import mail
from django.utils import timezone

from iic_booking.equipment.models import Booking, BookingStatus, DailySlot
from iic_booking.external_services import refunds, sric_dispatch
from iic_booking.external_services.models import (
    ExternalRefund,
    ExternalServicesSettings,
    RefundBankAccount,
    RefundStatus,
    SricInvoiceRequest,
)
from iic_booking.users.models import SubWallet
from iic_booking.users.models.user_type import UserType

from .conftest import client_for, give_wallet, make_user

pytestmark = pytest.mark.django_db
API = "/api/v1/external-booking/"
# 1180 paid = 1000 analysis + 0 shipping + 180 GST (18%)
BREAKDOWN = [{"description": "Analysis charge", "amount": 1000.0}, {"description": "GST (18%)", "amount": 180.0}]
# 1298 paid = 1000 analysis + 100 return shipping + 198 GST
WITH_SHIPPING = BREAKDOWN[:1] + [{"description": "Return shipping charges (non-refundable)", "amount": 100.0},
                                 {"description": "GST (18%)", "amount": 198.0}]
ACCOUNT = {"account_holder_name": "Asha Verma", "bank_name": "State Bank of India", "account_number": "123456789012",
           "account_number_confirm": "123456789012", "ifsc": "SBIN0001234", "branch": "Roorkee"}


@pytest.fixture
def policy(db):
    s = ExternalServicesSettings.load()
    s.cancellation_policy_enabled = True
    s.save()
    return s


@pytest.fixture
def user(db):
    return make_user()


def _booking(factory, user, hours_ahead, *, breakdown=BREAKDOWN, total="1180.00", shipping="0", **fields):
    eq = factory.equipment()
    give_wallet(user, factory.department, "0.00")
    return factory.booking(user, eq, timezone.now() + timedelta(hours=hours_ahead), total=total,
                           charge_breakdown=breakdown, return_shipping_fee_amount=Decimal(shipping), **fields)


def _wallet(user, factory) -> Decimal:
    return SubWallet.objects.get(wallet__user=user, department=factory.department).balance


@pytest.mark.parametrize("hours,tier,percent,refund", [
    (72, "free", 0, "1180.00"),
    (36, "half", 50, "590.00"),
    (12, "full", 100, "0.00"),
])
def test_penalty_tiers(policy, factory, user, hours, tier, percent, refund):
    b = _booking(factory, user, hours)
    q = refunds.cancellation_quote(b)
    assert (q["tier"], q["penalty_percent"], q["refund"]) == (tier, percent, refund)


def test_return_shipping_is_never_refunded_and_its_gst_neither(policy, factory, user):
    b = _booking(factory, user, 72, breakdown=WITH_SHIPPING, total="1298.00", shipping="100.00")
    q = refunds.cancellation_quote(b)
    assert q["return_shipping"] == "118.00" and q["refund"] == "1180.00"


def test_gst_is_not_refunded_once_an_invoice_was_requested(policy, factory, user):
    b = _booking(factory, user, 36)
    SricInvoiceRequest.objects.create(booking=b, trigger="early", snapshot={})
    q = refunds.cancellation_quote(b)
    assert q["gst_refunded"] is False and q["refund_base"] == "500.00" and q["refund"] == "500.00"


def test_user_cancel_inside_the_half_window_credits_half_to_the_wallet(policy, factory, user):
    b = _booking(factory, user, 36)
    r = client_for(user).post(f"{API}bookings/{b.pk}/cancel/", {"destination": "wallet", "expected_refund": "590.00"},
                              format="json")
    assert r.status_code == 200, r.content
    assert _wallet(user, factory) == Decimal("590.00")
    refund = ExternalRefund.objects.get(booking=b)
    assert refund.status == RefundStatus.CLOSED and refund.penalty_percent == 50 and refund.wallet_transaction_id
    assert not DailySlot.objects.filter(booking=b).exists()  # slots released
    # A second attempt never refunds twice.
    again = client_for(user).post(f"{API}bookings/{b.pk}/cancel/", {"destination": "wallet"}, format="json")
    assert again.status_code in (400, 409)
    assert _wallet(user, factory) == Decimal("590.00")


def test_less_than_24_hours_cancels_without_refund(policy, factory, user):
    b = _booking(factory, user, 10)
    r = client_for(user).post(f"{API}bookings/{b.pk}/cancel/", {"destination": "wallet"}, format="json")
    assert r.status_code == 200, r.content
    assert _wallet(user, factory) == Decimal("0.00")
    b.refresh_from_db()
    assert b.status == BookingStatus.CANCELLED


def test_a_changed_amount_is_never_applied_silently(policy, factory, user):
    b = _booking(factory, user, 36)
    r = client_for(user).post(f"{API}bookings/{b.pk}/cancel/", {"destination": "wallet", "expected_refund": "1180.00"},
                              format="json")
    assert r.status_code == 409 and r.json()["code"] == "quote_changed"
    assert Booking.objects.get(pk=b.pk).status == BookingStatus.BOOKED


def test_bank_refund_only_for_a_lab_disruption(policy, factory, user):
    b = _booking(factory, user, 72)
    acc = refunds.save_account(user, ACCOUNT)
    r = client_for(user).post(f"{API}bookings/{b.pk}/cancel/", {"destination": "bank", "bank_account_id": acc.pk},
                              format="json")
    assert r.status_code == 400 and r.json()["code"] == "bank_not_allowed"


def test_disruption_refunds_in_full_to_a_saved_bank_account_and_waits_for_sric(policy, factory, user):
    b = _booking(factory, user, 2, status=BookingStatus.DISRUPTION_PENDING)
    added = client_for(user).post(f"{API}refund-accounts/", ACCOUNT, format="json")
    assert added.status_code == 201 and added.json()["account_number"] == "XXXXXXXX9012"
    quote = client_for(user).get(f"{API}bookings/{b.pk}/cancellation-quote/").json()
    assert quote["tier"] == "disruption" and quote["refund"] == "1180.00" and quote["bank_refund_allowed"] is True
    assert quote["accounts"][0]["account_number"] == "XXXXXXXX9012"
    r = client_for(user).post(f"{API}bookings/{b.pk}/cancel/",
                              {"destination": "bank", "bank_account_id": added.json()["id"]}, format="json")
    assert r.status_code == 200, r.content
    refund = ExternalRefund.objects.get(booking=b)
    assert refund.status == RefundStatus.QUEUED and refund.refund_amount == Decimal("1180.00")
    assert refund.bank_snapshot["account_number"] == "123456789012"
    assert _wallet(user, factory) == Decimal("0.00")  # nothing to the wallet


def test_bank_account_validation(user):
    with pytest.raises(refunds.RefundPolicyError) as exc:
        refunds.save_account(user, {**ACCOUNT, "ifsc": "BAD", "account_number_confirm": "000"})
    assert set(exc.value.extra["fields"]) == {"ifsc", "account_number_confirm"}
    refunds.save_account(user, ACCOUNT)
    refunds.save_account(user, ACCOUNT)  # the same account again is updated, not duplicated
    assert RefundBankAccount.objects.filter(user=user).count() == 1


def test_weekly_batch_sends_bank_refunds_and_staff_close_them(policy, factory, user, settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    s = ExternalServicesSettings.load()
    s.sric_dispatch_mode, s.sric_test_recipient_email = "test", "sric.test@example.org"
    s.save()
    b = _booking(factory, user, 2, status=BookingStatus.DISRUPTION_PENDING)
    acc = refunds.save_account(user, ACCOUNT)
    refunds.cancel(user, b.pk, destination="bank", bank_account_id=acc.pk)
    refund = ExternalRefund.objects.get(booking=b)
    ExternalRefund.objects.filter(pk=refund.pk).update(created_at=timezone.now() - timedelta(days=3))
    # A Wednesday that is not the first working day of the week opens no batch.
    today = timezone.localdate()
    wednesday = today - timedelta(days=today.weekday()) + timedelta(days=2)
    at_wednesday = timezone.make_aware(timezone.datetime.combine(wednesday, sric_dispatch.BATCH_TIME)) + timedelta(minutes=5)
    sric_dispatch.run_batch(now=at_wednesday)
    assert ExternalRefund.objects.get(pk=refund.pk).status == RefundStatus.QUEUED
    monday = wednesday + timedelta(days=5)
    result = sric_dispatch.run_batch(now=timezone.make_aware(timezone.datetime.combine(monday, sric_dispatch.BATCH_TIME)) + timedelta(minutes=5))
    assert result.get("refunds_batched") == 1 and result.get("refunds_sent") == 1
    refund.refresh_from_db()
    assert refund.status == RefundStatus.SENT
    assert any("123456789012" in m.body and "[TEST]" in m.subject for m in mail.outbox)
    accounts = make_user(UserType.FINANCE)
    listing = client_for(accounts).get(f"{API}staff/refunds/").json()
    assert listing["count"] == 1 and listing["refunds"][0]["bank"]["account_number"] == "123456789012"
    assert client_for(user).get(f"{API}staff/refunds/").status_code == 403
    no_proof = client_for(accounts).post(f"{API}staff/refunds/{refund.pk}/close/", {}, format="json")
    assert no_proof.status_code == 400
    closed = client_for(accounts).post(f"{API}staff/refunds/{refund.pk}/close/", {"utr": "SBIN26110312345"}, format="json")
    assert closed.status_code == 200 and closed.json()["status"] == RefundStatus.CLOSED
    mine = client_for(user).get(f"{API}refunds/").json()["refunds"][0]
    assert mine["status"] == RefundStatus.CLOSED and mine["bank"]["account_number"] == "XXXXXXXX9012"


def test_is_batch_day_skips_a_monday_holiday(db):
    from iic_booking.equipment.models import Holiday

    today = timezone.localdate()
    monday = today - timedelta(days=today.weekday()) + timedelta(days=7)
    Holiday.objects.create(date=monday, reason="Test holiday", is_active=True)
    assert not sric_dispatch.is_batch_day(monday)
    assert sric_dispatch.is_batch_day(monday + timedelta(days=1))
    assert not sric_dispatch.is_batch_day(monday + timedelta(days=2))


def test_policy_off_leaves_cancellation_as_before(factory, user):
    b = _booking(factory, user, 72)
    assert client_for(user).get(f"{API}bookings/{b.pk}/cancellation-quote/").json() == {"applies": False}
    r = client_for(user).post(f"{API}bookings/{b.pk}/cancel/", {"destination": "wallet"}, format="json")
    assert r.status_code == 403


def test_old_cancel_endpoint_follows_the_policy(policy, factory, user):
    b = _booking(factory, user, 36)
    r = client_for(user).post(f"/api/bookings/{b.booking_id}/user-cancel/", {"notes": "plans changed"}, format="json")
    assert r.status_code == 200, r.content
    assert r.json()["refund_amount"] == "590.00"
    assert _wallet(user, factory) == Decimal("590.00")

"""Unit tests for Razorpay payment module (fees, signatures, idempotent settle)."""

from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings

from iic_booking.equipment.models import Booking, BookingStatus, BookingChargeSetting
from iic_booking.payments.fees import compute_fee_breakup, set_fee_percents
from iic_booking.payments.models import (
    Payment,
    PaymentOrder,
    PaymentOrderStatus,
    PaymentPurpose,
)
from iic_booking.payments.razorpay_service import (
    settle_order_success,
    verify_checkout_signature,
    verify_webhook_signature,
    handle_webhook,
)
from iic_booking.users.models import Department, UserType
from iic_booking.users.models.department import DepartmentType

User = get_user_model()


class FeeMathTests(SimpleTestCase):
    def test_zero_fee(self):
        with patch("iic_booking.payments.fees.get_fee_percents", return_value=(Decimal("0"), Decimal("18"))):
            b = compute_fee_breakup(Decimal("1000.00"))
        self.assertEqual(b.convenience_fee, Decimal("0.00"))
        self.assertEqual(b.fee_gst, Decimal("0.00"))
        self.assertEqual(b.total_amount, Decimal("1000.00"))

    def test_two_percent_plus_gst(self):
        with patch("iic_booking.payments.fees.get_fee_percents", return_value=(Decimal("2"), Decimal("18"))):
            b = compute_fee_breakup(Decimal("1000.00"))
        self.assertEqual(b.convenience_fee, Decimal("20.00"))
        self.assertEqual(b.fee_gst, Decimal("3.60"))
        self.assertEqual(b.total_amount, Decimal("1023.60"))


@override_settings(RAZORPAY_KEY_SECRET="test_secret", RAZORPAY_WEBHOOK_SECRET="whsec")
class SignatureTests(SimpleTestCase):
    def test_checkout_signature_ok(self):
        order_id = "order_abc"
        payment_id = "pay_xyz"
        sig = hmac.new(b"test_secret", f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
        self.assertTrue(verify_checkout_signature(order_id, payment_id, sig))

    def test_checkout_signature_fail(self):
        self.assertFalse(verify_checkout_signature("order_abc", "pay_xyz", "bad"))

    def test_webhook_signature_ok(self):
        body = b'{"event":"payment.captured"}'
        sig = hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_webhook_signature(body, sig))


class SettleIdempotencyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="paytest@example.com",
            password="x",
            user_type=UserType.FACULTY,
        )
        from iic_booking.equipment.models import Equipment

        self.dept = Department.objects.create(
            name="Test Dept",
            code="TD",
            department_type=DepartmentType.INTERNAL,
        )
        try:
            self.equipment = Equipment.objects.create(
                name="Test Eq",
                code="TEQ1",
                internal_department=self.dept,
            )
        except Exception:
            self.equipment = None

    def _make_order(self, booking=None, purpose=PaymentPurpose.WALLET_RECHARGE, order_id="order_test_1", idem="idem-1"):
        from iic_booking.users.repositories.wallet_repository import WalletRepository

        wallet, _ = WalletRepository.get_or_create(self.user)
        return PaymentOrder.objects.create(
            razorpay_order_id=order_id,
            receipt="rcpt1",
            purpose=purpose,
            user=self.user,
            booking=booking,
            wallet=wallet,
            department=self.dept,
            base_amount=Decimal("100.00"),
            convenience_fee=Decimal("0.00"),
            fee_gst=Decimal("0.00"),
            total_amount=Decimal("100.00"),
            status=PaymentOrderStatus.CREATED,
            idempotency_key=idem,
        )

    @patch("iic_booking.payments.razorpay_service.SubWalletRepository.get_or_create")
    def test_double_settle_credits_once(self, mock_get):
        sub = MagicMock()
        mock_get.return_value = sub
        order = self._make_order()
        o1, p1 = settle_order_success(
            razorpay_order_id=order.razorpay_order_id,
            razorpay_payment_id="pay_1",
            razorpay_signature="sig",
        )
        o2, p2 = settle_order_success(
            razorpay_order_id=order.razorpay_order_id,
            razorpay_payment_id="pay_1",
            razorpay_signature="sig",
        )
        self.assertEqual(p1.id, p2.id)
        self.assertEqual(o1.status, PaymentOrderStatus.PAID)
        self.assertEqual(sub.credit.call_count, 1)

    def test_booking_pending_to_booked(self):
        if not self.equipment:
            self.skipTest("Could not create equipment fixture")
        from iic_booking.equipment.models import ChargeProfile

        profile = ChargeProfile.objects.create(
            equipment=self.equipment,
            user_type=UserType.FACULTY,
            primary_unit_charge=Decimal("100.00"),
        )
        booking = Booking.objects.create(
            user=self.user,
            equipment=self.equipment,
            charge_profile=profile,
            status=BookingStatus.PENDING_PAYMENT,
            total_charge=Decimal("500.00"),
            total_time_minutes=60,
            wallet_amount_applied=Decimal("200.00"),
            amount_due=Decimal("300.00"),
            settlement_department=self.dept,
        )
        order = PaymentOrder.objects.create(
            razorpay_order_id="order_book_1",
            receipt="rcptb",
            purpose=PaymentPurpose.BOOKING_SHORTFALL,
            user=self.user,
            booking=booking,
            department=self.dept,
            base_amount=Decimal("300.00"),
            convenience_fee=Decimal("0"),
            fee_gst=Decimal("0"),
            total_amount=Decimal("300.00"),
            status=PaymentOrderStatus.CREATED,
            idempotency_key="idem-book-1",
        )
        settle_order_success(
            razorpay_order_id=order.razorpay_order_id,
            razorpay_payment_id="pay_book_1",
        )
        booking.refresh_from_db()
        self.assertEqual(booking.status, BookingStatus.BOOKED)
        self.assertIsNotNone(booking.payment_settled_at)
        self.assertEqual(booking.amount_due, Decimal("0.00"))

        # Idempotent second call
        settle_order_success(
            razorpay_order_id=order.razorpay_order_id,
            razorpay_payment_id="pay_book_1",
        )
        booking.refresh_from_db()
        self.assertEqual(booking.status, BookingStatus.BOOKED)

    @override_settings(RAZORPAY_WEBHOOK_SECRET="whsec")
    @patch("iic_booking.payments.razorpay_service.SubWalletRepository.get_or_create")
    def test_webhook_after_verify(self, mock_get):
        sub = MagicMock()
        mock_get.return_value = sub
        order = self._make_order()
        settle_order_success(
            razorpay_order_id=order.razorpay_order_id,
            razorpay_payment_id="pay_wh_1",
        )
        payload = {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_wh_1",
                        "order_id": order.razorpay_order_id,
                        "method": "upi",
                    }
                }
            },
        }
        body = json.dumps(payload).encode()
        sig = hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
        handle_webhook(payload, body, sig)
        self.assertEqual(sub.credit.call_count, 1)
        self.assertEqual(Payment.objects.filter(razorpay_payment_id="pay_wh_1").count(), 1)


class FeeSettingsDbTests(TestCase):
    def test_set_and_get_fee_percents(self):
        set_fee_percents(Decimal("2.5"), Decimal("18"))
        from iic_booking.payments.fees import get_fee_percents

        fee, gst = get_fee_percents()
        self.assertEqual(fee, Decimal("2.5"))
        self.assertEqual(gst, Decimal("18"))
        self.assertTrue(
            BookingChargeSetting.objects.filter(key="RAZORPAY_CONVENIENCE_FEE_PERCENT").exists()
        )

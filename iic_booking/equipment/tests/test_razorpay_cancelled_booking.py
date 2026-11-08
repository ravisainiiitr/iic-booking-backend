"""A gateway payment that arrives after its booking was cancelled is raised for a refund, never applied silently."""

from decimal import Decimal

import pytest

from iic_booking.equipment.models import Booking
from iic_booking.equipment.models import BookingStatus
from iic_booking.payments.models import PaymentIssue
from iic_booking.payments.models import PaymentIssueKind
from iic_booking.payments.models import PaymentOrder
from iic_booking.payments.models import PaymentPurpose
from iic_booking.payments.razorpay_service import settle_order_success
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def test_payment_for_a_cancelled_booking_is_raised(egs_factory):
    owner = UserFactory()
    booking = egs_factory.booking(owner, egs_factory.equipment(), egs_factory.future())
    Booking.objects.filter(pk=booking.pk).update(status=BookingStatus.CANCELLED, amount_due=Decimal("10.00"))
    PaymentOrder.objects.create(
        razorpay_order_id="order_CXL", receipt="r-cxl", purpose=PaymentPurpose.BOOKING_SHORTFALL, user=owner, booking=booking,
        base_amount=Decimal("10.00"), total_amount=Decimal("10.00"), idempotency_key="k-cxl",
    )
    settle_order_success(razorpay_order_id="order_CXL", razorpay_payment_id="pay_CXL")
    booking.refresh_from_db()
    assert booking.status == BookingStatus.CANCELLED and booking.payment_settled_at is None
    issue = PaymentIssue.objects.get()
    assert (issue.kind, issue.razorpay_payment_id) == (PaymentIssueKind.CANCELLED_BOOKING, "pay_CXL")

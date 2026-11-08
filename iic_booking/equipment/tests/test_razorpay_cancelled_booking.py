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


def test_booking_payment_preview_is_server_calculated_and_owner_only(egs_factory):
    from rest_framework.test import APIClient

    owner = UserFactory()
    booking = egs_factory.booking(owner, egs_factory.equipment(), egs_factory.future())
    Booking.objects.filter(pk=booking.pk).update(amount_due=Decimal("1000.00"), status=BookingStatus.PENDING_PAYMENT)
    client = APIClient()
    client.force_authenticate(owner)
    data = client.get(f"/api/payments/booking-preview/{booking.pk}/").json()
    assert data["amount_due"] == "1000.00" and data["breakup"]["base_amount"] == "1000.00"
    assert data["gateway"]["checkout_name"] == "DEAN SRIC IIT ROORKEE"
    stranger = APIClient()
    stranger.force_authenticate(UserFactory())
    assert stranger.get(f"/api/payments/booking-preview/{booking.pk}/").status_code == 404


def test_receipt_shows_the_booking_total_and_its_gst(egs_factory):
    from rest_framework.test import APIClient

    owner = UserFactory()
    booking = egs_factory.booking(owner, egs_factory.equipment(), egs_factory.future())
    Booking.objects.filter(pk=booking.pk).update(
        status=BookingStatus.PENDING_PAYMENT, amount_due=Decimal("1180.00"), total_charge=Decimal("1180.00"),
        charge_breakdown=[{"description": "Analysis", "amount": "1000.00"}, {"description": "GST @ 18%", "amount": "180.00"}],
    )
    PaymentOrder.objects.create(
        razorpay_order_id="order_GST", receipt="iic_book_gst", purpose=PaymentPurpose.BOOKING_SHORTFALL, user=owner, booking=booking,
        base_amount=Decimal("1180.00"), total_amount=Decimal("1180.00"), idempotency_key="k-gst",
    )
    settle_order_success(razorpay_order_id="order_GST", razorpay_payment_id="pay_GST")
    client = APIClient()
    client.force_authenticate(owner)
    data = client.get("/api/payments/receipts/pay_GST/").json()
    assert (data["booking_total"], data["booking_gst"]) == ("1180.00", "180.00")


def test_payment_pages_get_the_equipment_photo(egs_factory):
    from rest_framework.test import APIClient

    from iic_booking.equipment.models import Equipment
    from iic_booking.external_services.checkout import equipment_image_url

    owner = UserFactory()
    equipment = egs_factory.equipment()
    booking = egs_factory.booking(owner, equipment, egs_factory.future())
    Booking.objects.filter(pk=booking.pk).update(amount_due=Decimal("10.00"), status=BookingStatus.PENDING_PAYMENT)
    client = APIClient()
    client.force_authenticate(owner)
    assert client.get(f"/api/payments/booking-preview/{booking.pk}/").json()["equipment_image_url"] == ""
    Equipment.objects.filter(pk=equipment.pk).update(image="equipment_images/sample.jpg")
    equipment.refresh_from_db()
    url = equipment_image_url(equipment)
    assert url and str(equipment.pk) in url
    assert client.get(f"/api/payments/booking-preview/{booking.pk}/").json()["equipment_image_url"] == url
    assert equipment_image_url(None) == ""


def test_preview_and_receipt_name_the_payment_purpose(egs_factory):
    from rest_framework.test import APIClient

    from iic_booking.equipment.models import Equipment

    owner = UserFactory()
    equipment = egs_factory.equipment()
    Equipment.objects.filter(pk=equipment.pk).update(name="Sample Printer", profile_type="PRINT_3D")
    equipment.refresh_from_db()
    booking = egs_factory.booking(owner, equipment, egs_factory.future())
    Booking.objects.filter(pk=booking.pk).update(amount_due=Decimal("10.00"), status=BookingStatus.PENDING_PAYMENT)
    booking.refresh_from_db()
    client = APIClient()
    client.force_authenticate(owner)
    purpose = client.get(f"/api/payments/booking-preview/{booking.pk}/").json()["payment_purpose"]
    assert purpose.endswith("for 'Sample Printer'") and purpose.split(" for ")[0] in ("3D printing charges", "Analysis charges")
    PaymentOrder.objects.create(
        razorpay_order_id="order_PUR", receipt="iic_book_pur", purpose=PaymentPurpose.BOOKING_SHORTFALL, user=owner, booking=booking,
        base_amount=Decimal("10.00"), total_amount=Decimal("10.00"), idempotency_key="k-pur",
    )
    settle_order_success(razorpay_order_id="order_PUR", razorpay_payment_id="pay_PUR")
    assert client.get("/api/payments/receipts/pay_PUR/").json()["description"] == purpose

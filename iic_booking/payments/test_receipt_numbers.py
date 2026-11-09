"""Printed receipt numbers: IITR/<department>/<year>/<5-digit serial>, one series per year, test payments apart."""

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from iic_booking.payments.models import Payment
from iic_booking.payments.models import PaymentOrder
from iic_booking.payments.models import PaymentPurpose
from iic_booking.payments.razorpay_service import settle_order_success
from iic_booking.payments.receipt_numbers import ensure
from iic_booking.users.models import Department
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import Wallet
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db
IST = ZoneInfo("Asia/Kolkata")


def _paid(n: int, department: Department, *, mode: str = "live") -> Payment:
    user = UserFactory(user_type=UserType.FACULTY)
    wallet, _ = Wallet.objects.get_or_create(user=user)
    PaymentOrder.objects.create(
        razorpay_order_id=f"order_N{n}", receipt=f"iic_wall_{n}", purpose=PaymentPurpose.WALLET_RECHARGE, user=user,
        wallet=wallet, department=department, base_amount=Decimal("100.00"), convenience_fee=Decimal("0"),
        fee_gst=Decimal("0"), fee_gst_percent_snapshot=Decimal("18"), total_amount=Decimal("100.00"),
        idempotency_key=f"k-n{n}", mode=mode,
    )
    _order, payment = settle_order_success(razorpay_order_id=f"order_N{n}", razorpay_payment_id=f"pay_N{n}")
    return payment


@pytest.fixture
def departments():
    iic = Department.objects.create(name="Institute Instrumentation Centre", code="IIC", department_type=DepartmentType.INTERNAL)
    phy = Department.objects.create(name="Physics", code="phy-1", department_type=DepartmentType.INTERNAL)
    return iic, phy


def test_numbers_follow_payment_order_with_the_department_and_year(departments):
    iic, phy = departments
    year = datetime.now(IST).year
    first, second, third = _paid(1, iic), _paid(2, phy), _paid(3, iic)
    assert first.receipt_number == f"IITR/IIC/{year}/00001"
    assert second.receipt_number == f"IITR/PHY1/{year}/00002"  # one series for the portal; code made safe
    assert third.receipt_number == f"IITR/IIC/{year}/00003"
    # Given once: settling again or opening the receipt never changes it.
    settle_order_success(razorpay_order_id="order_N1", razorpay_payment_id="pay_N1")
    assert ensure(Payment.objects.get(pk=first.pk)) == f"IITR/IIC/{year}/00001"


def test_test_mode_payments_have_their_own_series(departments):
    iic, _ = departments
    year = datetime.now(IST).year
    assert _paid(1, iic, mode="test").receipt_number == f"IITR/IIC/{year}/TEST-00001"
    assert _paid(2, iic).receipt_number == f"IITR/IIC/{year}/00001"  # no gap left by the test payment


def test_the_serial_starts_again_each_year(departments):
    iic, _ = departments
    old = _paid(1, iic)
    Payment.objects.filter(pk=old.pk).update(receipt_number=None, created_at=datetime(2025, 12, 31, 23, 0, tzinfo=IST))
    assert ensure(Payment.objects.get(pk=old.pk)) == "IITR/IIC/2025/00001"
    assert _paid(2, iic).receipt_number.endswith("/00002")  # this year's series already had 00001


def test_receipt_contact_is_the_department_accounts_in_charge(departments):
    from iic_booking.payments.receipts import receipt_for

    iic, phy = departments
    from django.contrib.auth import get_user_model

    UserFactory(user_type=UserType.FINANCE, department=iic, email="iic.left@example.org")
    UserFactory(user_type=UserType.FINANCE, department=iic, email="iic.accounts@example.org")
    UserFactory(user_type=UserType.FINANCE, department=iic, email="iic.second@example.org")
    UserFactory(user_type=UserType.FINANCE, department=phy, email="phy.accounts@example.org")
    UserFactory(user_type=UserType.OPERATOR, department=phy, email="phy.operator@example.org")
    # Staff accounts start inactive until approved; approve all but the one who left.
    get_user_model().objects.exclude(email="iic.left@example.org").filter(email__endswith="@example.org").update(
        is_active=True
    )
    # Active only; with several, the first appointed (lowest id).
    assert receipt_for(_paid(1, iic))["template"]["contact_email"] == "iic.accounts@example.org"
    assert receipt_for(_paid(2, phy))["template"]["contact_email"] == "phy.accounts@example.org"


def test_receipt_contact_falls_back_to_the_template_email(departments):
    from iic_booking.payments.receipts import receipt_for

    _iic, phy = departments
    UserFactory(user_type=UserType.FINANCE, department=phy, email="gone@example.org", is_active=False)
    assert receipt_for(_paid(1, phy))["template"]["contact_email"] == "arsric@iitr.ac.in"


def test_receipt_shows_the_facility_department_and_its_grant_code(departments):
    from iic_booking.payments.receipts import receipt_for

    iic, phy = departments
    Department.objects.filter(pk=iic.pk).update(internal_grant_code=" IIC-GRANT-01 ")
    receipt = receipt_for(_paid(1, iic))
    assert receipt["department_name"] == "Institute Instrumentation Centre"
    assert receipt["grant_code"] == "IIC-GRANT-01"
    assert receipt_for(_paid(2, phy))["grant_code"] == ""  # none set: the receipt leaves the line out

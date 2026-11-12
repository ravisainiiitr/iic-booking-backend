"""Payment issues API for the Finance page: who may see and close issues, and what each issue shows."""

from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from iic_booking.payments.models import PaymentIssue, PaymentIssueKind, PaymentIssueStatus, PaymentOrder, PaymentPurpose
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def _client(user_type):
    client = APIClient()
    client.force_authenticate(UserFactory(user_type=user_type, name="Finance Officer"))
    return client


@pytest.fixture
def issue():
    order = PaymentOrder.objects.create(razorpay_order_id="order_pi1", receipt="r", purpose=PaymentPurpose.WALLET_RECHARGE,
                                        user=UserFactory(is_test_account=True), base_amount=Decimal("1"),
                                        total_amount=Decimal("1"), idempotency_key="k-pi1", mode="test")
    return PaymentIssue.objects.create(kind=PaymentIssueKind.NOT_APPLIED, key="pay_pi1", razorpay_payment_id="pay_pi1",
                                       razorpay_order_id="order_pi1", payment_order=order, amount=Decimal("1"), detail="x")


@pytest.mark.parametrize("user_type", [UserType.ADMIN, UserType.FINANCE])
def test_finance_and_the_main_administrator_list_and_close_issues(issue, user_type):
    client = _client(user_type)
    rows = client.get("/api/payments/issues/").json()["results"]
    assert [r["id"] for r in rows] == [issue.pk]
    assert (rows[0]["purpose"], rows[0]["test_account"], rows[0]["booking_reference"]) == ("WALLET_RECHARGE", True, "")
    assert client.post(f"/api/payments/issues/{issue.pk}/resolve/", {"note": "ok"}).status_code == 400  # note too short
    done = client.post(f"/api/payments/issues/{issue.pk}/resolve/", {"note": "Refunded on Razorpay"}).json()
    assert (done["status"], done["resolved_by"]) == (PaymentIssueStatus.RESOLVED, "Finance Officer")
    assert client.get("/api/payments/issues/", {"status": "RESOLVED"}).json()["results"][0]["resolution_note"] == "Refunded on Razorpay"


@pytest.mark.parametrize("user_type", [UserType.STUDENT, UserType.MANAGER, UserType.OPERATOR])
def test_no_one_else_sees_them(issue, user_type):
    client = _client(user_type)
    assert client.get("/api/payments/issues/").status_code == 403
    assert client.post(f"/api/payments/issues/{issue.pk}/resolve/", {"note": "closing it"}).status_code == 403

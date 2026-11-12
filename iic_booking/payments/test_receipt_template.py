"""Receipt template the Main Admin edits on the dashboard."""

import pytest

from iic_booking.payments.models import ReceiptTemplateChange
from iic_booking.payments.receipts import template_values
from iic_booking.payments.test_receipts import URL
from iic_booking.payments.test_receipts import _client
from iic_booking.payments.test_receipts import paid_recharge  # noqa: F401 (fixture)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db
ADMIN_URL = "/api/admin/payments/receipt-template/"


@pytest.fixture
def main_admin():
    return UserFactory(user_type=UserType.ADMIN, name="Main Admin")


def test_only_main_admin_can_read_or_change(main_admin):
    for user_type in (UserType.FINANCE, UserType.FACULTY):
        client = _client(UserFactory(user_type=user_type))
        assert client.get(ADMIN_URL).status_code == 403
        assert client.put(ADMIN_URL, {"office_name": "X"}, format="json").status_code == 403
    data = _client(main_admin).get(ADMIN_URL).json()
    assert data["stored"]["office_name"] == ""
    assert data["effective"]["office_name"] == data["defaults"]["office_name"]
    assert data["effective"]["brand_color"] == "#153F79"


def test_saved_values_reach_the_receipt_and_blank_restores_default(main_admin, paid_recharge):  # noqa: F811
    admin = _client(main_admin)
    res = admin.put(ADMIN_URL, {"office_name": "Office of Dean SRIC", "refund_note": "Refunds go to the wallet.",
                                "extra_note": "Keep this receipt.", "brand_color": "#0b3d2e", "show_logo": False},
                    format="json")
    assert res.status_code == 200, res.json()
    assert res.json()["effective"]["brand_color"] == "#0B3D2E"

    receipt = _client(paid_recharge).get(URL.format("pay_RCP1")).json()
    # The header names the department the money is for; the template office is only the fallback.
    assert receipt["office"] == "Receipt Dept"
    assert receipt["template"]["refund_note"] == "Refunds go to the wallet."
    assert receipt["template"]["extra_note"] == "Keep this receipt."
    assert receipt["template"]["show_logo"] is False
    # Untouched fields keep their defaults.
    assert receipt["institution"] == "Indian Institute of Technology Roorkee"



def test_hiding_the_gstin_blanks_it_on_receipts(main_admin, paid_recharge):  # noqa: F811
    _client(main_admin).patch(ADMIN_URL, {"show_gstin": False}, format="json")
    assert _client(paid_recharge).get(URL.format("pay_RCP1")).json()["institute_gstin"] == ""


def test_bad_values_are_refused_field_by_field(main_admin):
    res = _client(main_admin).put(ADMIN_URL, {"institute_gstin": "12345", "contact_email": "not-an-email",
                                              "brand_color": "#FFFF00", "accent_color": "gold",
                                              "receipt_heading": "x" * 61}, format="json")
    assert res.status_code == 400
    fields = {k: v for k, v in res.json().items() if k != "error"}
    assert set(fields) == {"institute_gstin", "contact_email", "brand_color", "accent_color", "receipt_heading"}
    assert "darker" in fields["brand_color"]
    assert template_values()["institute_gstin"] == "05AAALI0033R1Z5"
    assert not ReceiptTemplateChange.objects.exists()


def test_each_change_is_recorded_once(main_admin):
    admin = _client(main_admin)
    admin.patch(ADMIN_URL, {"institute_gstin": "05aaali0033r1z5", "website": "https://equip.iitr.ac.in/"}, format="json")
    admin.patch(ADMIN_URL, {"website": "equip.iitr.ac.in"}, format="json")  # same value: no new record
    changes = list(ReceiptTemplateChange.objects.all())
    assert len(changes) == 1
    assert changes[0].changed_by == main_admin
    assert changes[0].changes["institute_gstin"] == {"from": "", "to": "05AAALI0033R1Z5"}
    assert changes[0].changes["website"]["to"] == "equip.iitr.ac.in"
    history = admin.get(ADMIN_URL).json()["history"]
    assert history[0]["changed_by"] == "Main Admin"
    assert history[0]["fields"] == ["institute_gstin", "website"]

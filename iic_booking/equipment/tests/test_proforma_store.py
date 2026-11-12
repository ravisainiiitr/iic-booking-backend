"""Saved proforma invoices: saved on issue with server prices, one per department, cancel and revise rules, payment
state worked out from linked recharges, staff scope and the reconciliation export."""

import io
from decimal import Decimal

import pytest
from pypdf import PdfReader

from iic_booking.equipment.proforma_models import ProformaEvent, ProformaPayment, ProformaPaymentKind, SavedProformaInvoice
from iic_booking.equipment.proforma_store import state_of
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db
DOWNLOAD = "/api/proforma-invoice/download.pdf"


def _text(pdf: bytes) -> str:
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)


def _line(eq, *, fake_base="99999.00"):
    # The browser's amounts are deliberately wrong: a signed-in estimate is priced again on the server.
    return {"equipment_id": eq.pk, "equipment_code": eq.code, "equipment_name": eq.name, "input_values": {},
            "charge_breakdown": [], "base_charge": fake_base, "gst_amount": "0.00", "total_charge": fake_base}


def _issue(client, *equipment):
    lines = [_line(e) for e in equipment]
    resp = client.post(DOWNLOAD, {"line_items": lines, "subtotal": "0", "total_gst": "0", "total_amount": "0"}, format="json")
    assert resp.status_code == 200, getattr(resp, "data", resp.content[:300])
    return resp


@pytest.fixture
def setup(egs_factory):
    eq = egs_factory.equipment()
    user = egs_factory.student()
    return egs_factory, eq, user, egs_factory.client_for(user)


def test_issuer_is_the_equipment_department_with_its_accounts_email(setup):
    f, eq, user, client = setup
    UserFactory(user_type=UserType.FINANCE, department=f.department, admin_approved=True, email="accounts.egs@iitr.ac.in")
    text = _text(_issue(client, eq).content)
    assert f.department.name in text
    assert "accounts.egs@iitr.ac.in" in text
    assert "Sponsored Research" not in text and "arsric@" not in text


def test_estimate_is_saved_with_server_prices(setup):
    f, eq, user, client = setup
    resp = _issue(client, eq)
    number = resp["X-Proforma-Numbers"]
    record = SavedProformaInvoice.objects.get(number=number)
    assert record.user == user and record.department == f.department
    assert record.amount_source == "SERVER"
    assert record.total != Decimal("99999.00")  # the browser's figure is not used
    assert record.pdf and record.events.filter(action="issued").exists()
    assert number in _text(resp.content)
    listed = client.get("/api/proforma-invoices/").data["results"]
    assert [r["number"] for r in listed] == [number]
    assert listed[0]["state"]["key"] == "issued" and listed[0]["state"]["can_pay"]


def test_two_departments_give_two_proformas(setup):
    from iic_booking.users.models import Department

    f, eq, user, client = setup
    other_dept = Department.objects.create(name="Physics Department", code="PHY-T", equipment_booking_enabled=True,
                                           equipment_visibility_enabled=True)
    eq2 = f.equipment()
    eq2.internal_department = other_dept
    eq2.save(update_fields=["internal_department"])
    resp = _issue(client, eq, eq2)
    numbers = resp["X-Proforma-Numbers"].split(",")
    assert len(numbers) == 2
    assert {r.department_id for r in SavedProformaInvoice.objects.filter(number__in=numbers)} == {f.department.pk, other_dept.pk}
    assert len(PdfReader(io.BytesIO(resp.content)).pages) == 2


def test_cancel_stamps_the_pdf_and_only_the_owner_may(setup):
    f, eq, user, client = setup
    record = SavedProformaInvoice.objects.get(number=_issue(client, eq)["X-Proforma-Numbers"])
    stranger = f.client_for(f.student())
    assert stranger.post(f"/api/proforma-invoices/{record.pk}/cancel/").status_code == 404
    resp = client.post(f"/api/proforma-invoices/{record.pk}/cancel/", {"reason": "Not needed"}, format="json")
    assert resp.status_code == 200 and resp.data["state"]["key"] == "cancelled"
    assert client.post(f"/api/proforma-invoices/{record.pk}/cancel/").status_code == 409
    pdf = client.get(f"/api/proforma-invoices/{record.pk}/pdf/")
    assert "CANCELLED" in _text(pdf.content)
    assert not resp.data["state"]["can_pay"]


def test_revise_issues_a_new_number_and_replaces_the_old(setup):
    f, eq, user, client = setup
    old = SavedProformaInvoice.objects.get(number=_issue(client, eq)["X-Proforma-Numbers"])
    resp = client.post(f"/api/proforma-invoices/{old.pk}/revise/", {"items": [{"equipment_id": eq.pk, "input_values": {}}]},
                       format="json")
    assert resp.status_code == 201, resp.data
    assert resp.data["number"] == f"{old.number}-R1" and resp.data["replaces"] == old.number
    old.refresh_from_db()
    assert old.status == "REPLACED"
    assert client.post(f"/api/proforma-invoices/{old.pk}/revise/", {"items": [{"equipment_id": eq.pk}]},
                       format="json").status_code == 409
    assert "REPLACED" in _text(client.get(f"/api/proforma-invoices/{old.pk}/pdf/").content)
    assert list(ProformaEvent.objects.filter(proforma=old).values_list("action", flat=True)) == ["issued", "replaced"]


def _offline_link(record, user, amount, status="PENDING"):
    from iic_booking.users.models.wallet import WalletRechargeRequest
    from iic_booking.users.repositories.wallet_repository import WalletRepository

    wallet, _ = WalletRepository.get_or_create(user)
    req = WalletRechargeRequest.objects.create(user=user, wallet=wallet, department=record.department,
                                               amount=Decimal(amount), status=status, recharge_mode="offline_payment")
    ProformaPayment.objects.create(proforma=record, kind=ProformaPaymentKind.OFFLINE, recharge_request=req,
                                   amount=Decimal(amount), created_by=user)
    return req


def test_payment_state_follows_the_linked_recharges(setup):
    f, eq, user, client = setup
    record = SavedProformaInvoice.objects.get(number=_issue(client, eq)["X-Proforma-Numbers"])
    total = record.total
    payer = UserFactory(user_type=UserType.EXTERNAL)  # a user type with a wallet of its own
    req = _offline_link(record, payer, total)
    state = state_of(record)
    assert state["key"] == "payment_submitted" and state["outstanding"] == "0.00"
    assert not state["can_edit"] and not state["can_cancel"] and not state["can_pay"]
    assert client.post(f"/api/proforma-invoices/{record.pk}/cancel/").status_code == 409
    req.status = "APPROVED"
    req.save(update_fields=["status"])
    assert state_of(record)["key"] == "paid"
    req.status = "REJECTED"
    req.save(update_fields=["status"])
    state = state_of(record)
    assert state["key"] == "issued" and state["can_pay"] and state["outstanding"] == str(total)


def test_amount_cannot_exceed_what_is_left(setup):
    from iic_booking.equipment.proforma_store import ProformaError, _amount

    with pytest.raises(ProformaError):
        _amount("500.01", Decimal("500.00"))
    assert _amount(None, Decimal("500.00")) == Decimal("500.00")
    assert _amount("120", Decimal("500.00")) == Decimal("120.00")


def test_staff_see_their_department_and_export_csv(setup):
    f, eq, user, client = setup
    record = SavedProformaInvoice.objects.get(number=_issue(client, eq)["X-Proforma-Numbers"])
    from iic_booking.users.models import Department

    accounts = UserFactory(user_type=UserType.FINANCE, department=f.department, admin_approved=True)
    other = UserFactory(user_type=UserType.DEPT_ADMIN, admin_approved=True,
                        department=Department.objects.create(name="Other", code="OTH-T"))
    main = UserFactory(user_type=UserType.ADMIN, admin_approved=True)
    listed = f.client_for(accounts).get("/api/admin/proforma-invoices/")
    assert listed.status_code == 200, listed.data
    assert [r["number"] for r in listed.data["results"]] == [record.number]
    assert f.client_for(other).get("/api/admin/proforma-invoices/").data["results"] == []
    assert f.client_for(user).get("/api/admin/proforma-invoices/").status_code == 403
    csv_resp = f.client_for(main).get("/api/admin/proforma-invoices/", {"export": "csv"})
    assert csv_resp.status_code == 200 and record.number in csv_resp.content.decode()
    assert f.client_for(accounts).get(f"/api/proforma-invoices/{record.pk}/").status_code == 200
    assert f.client_for(other).get(f"/api/proforma-invoices/{record.pk}/").status_code == 404


def test_history_is_append_only(setup):
    f, eq, user, client = setup
    record = SavedProformaInvoice.objects.get(number=_issue(client, eq)["X-Proforma-Numbers"])
    event = record.events.first()
    event.action = "edited"
    with pytest.raises(RuntimeError):
        event.save()
    with pytest.raises(RuntimeError):
        event.delete()


def test_staff_estimates_keep_the_page_figures(setup):
    # Staff price estimates for other user types on Book Equipment, so their proforma is not re-priced for themselves.
    f, eq, user, client = setup
    staff = UserFactory(user_type=UserType.ADMIN, admin_approved=True)
    resp = _issue(f.client_for(staff), eq)
    record = SavedProformaInvoice.objects.get(number=resp["X-Proforma-Numbers"])
    assert record.amount_source == "CLIENT"
    assert record.total == Decimal("99999.00")

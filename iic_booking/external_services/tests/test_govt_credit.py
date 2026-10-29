"""Credit for government and R&D organisations (Head IIC Accept / Reject by email, wallet credit, invoices held until
the payment is realised) and the per-booking / consolidated invoice choice. Email goes to ``mail.outbox``."""

from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone

from iic_booking.external_services import govt_credit, invoices, sric_dispatch
from iic_booking.external_services.models import (
    ExternalServicesSettings,
    GovtCreditRequest,
    GovtCreditStatus,
    InvoiceRequestStatus,
    SricInvoiceRequest,
)
from iic_booking.users.models import SubWallet
from iic_booking.users.models.user_type import UserType

from .conftest import client_for, give_wallet, make_user
from .test_invoices import PDF, _billing, _working_day_at

pytestmark = pytest.mark.django_db
HEAD = "head.iic@example.org"
BASE = "/api/v1/external-booking/"


@pytest.fixture
def on():
    s = ExternalServicesSettings.load()
    s.invoice_requests_enabled = True
    s.govt_credit_enabled = True
    s.head_iic_emails = HEAD
    s.sric_dispatch_mode, s.sric_test_recipient_email = "test", "sric-test@example.org"
    s.save()
    return s


@pytest.fixture
def lab(factory):
    user = make_user(UserType.RND)
    _billing(user, billing_name="National Lab of Testing")
    give_wallet(user, factory.department, "0.00")
    return {"user": user, "dept": factory.department, "eq": factory.equipment(), "factory": factory}


def _proforma(user, total="5000.00"):
    r = client_for(user).post(f"{BASE}govt-credit/proforma/",
                              {"items": [{"description": "XRD, 20 samples", "amount": total}]}, format="json")
    assert r.status_code == 200 and r.content.startswith(b"%PDF"), r.content[:200]
    return r["X-Proforma-Reference"]


def _submit(lab, amount="5000.00", **extra):
    data = {
        "proforma_reference": _proforma(lab["user"]), "department_id": lab["dept"].pk,
        "organisation": "National Lab of Testing", "amount": amount,
        "credit_until": (timezone.localdate() + timedelta(days=60)).isoformat(),
        "settlement_date": (timezone.localdate() + timedelta(days=90)).isoformat(),
        "invoice_mode": "consolidated", "purpose": "XRD of thin films",
        "approval_letter": SimpleUploadedFile("letter.pdf", PDF, content_type="application/pdf"),
    }
    data.update(extra)
    return client_for(lab["user"]).post(f"{BASE}govt-credit/", data, format="multipart")


def _link(action):
    body = mail.outbox[-1].body
    return re.search(rf"{action.capitalize()}: (\S+)", body).group(1).split("/api/", 1)[1]


def _balance(lab) -> Decimal:
    return SubWallet.objects.get(wallet__user=lab["user"], department=lab["dept"]).balance


def test_request_is_emailed_to_head_iic_and_opening_the_link_decides_nothing(on, lab):
    r = _submit(lab)
    assert r.status_code == 201, r.content
    assert mail.outbox[-1].to == [HEAD] and "Accept:" in mail.outbox[-1].body
    page = Client().get("/api/" + _link("accept"))
    assert page.status_code == 200 and b"Accept this request" in page.content
    assert GovtCreditRequest.objects.get().status == GovtCreditStatus.PENDING
    assert _balance(lab) == Decimal("0.00")


def test_accept_credits_the_wallet_once_and_tells_the_user(on, lab):
    _submit(lab)
    url = "/api/" + _link("accept")
    assert Client().post(url, {"note": "Approved"}).status_code == 200
    req = GovtCreditRequest.objects.get()
    assert (req.status, req.decided_by_email) == (GovtCreditStatus.ACCEPTED, HEAD)
    assert _balance(lab) == Decimal("5000.00")
    again = Client().post(url)
    assert again.status_code == 409 and _balance(lab) == Decimal("5000.00")  # no second credit


def test_reject_leaves_the_wallet_alone(on, lab):
    _submit(lab)
    assert Client().post("/api/" + _link("reject")).status_code == 200
    assert GovtCreditRequest.objects.get().status == GovtCreditStatus.REJECTED
    assert _balance(lab) == Decimal("0.00")


def test_a_tampered_link_is_refused(on, lab):
    _submit(lab)
    assert Client().get("/api/" + _link("accept")[:-6] + "abcdef/").status_code == 404


@pytest.mark.parametrize("change,code", [
    ({"amount": "6000.00"}, "invalid_amount"),
    ({"proforma_reference": "IIC-CPI-NOT-MINE"}, "proforma_required"),
    ({"approval_letter": SimpleUploadedFile("letter.pdf", b"not a pdf", content_type="application/pdf")}, "letter_invalid"),
    ({"settlement_date": "2020-01-01"}, "invalid_dates"),
])
def test_invalid_requests_are_refused(on, lab, change, code):
    r = _submit(lab, **change)
    assert r.status_code == 400 and r.json()["code"] == code, r.content


def test_only_government_users_and_only_when_enabled(on, lab, factory):
    other = make_user(UserType.EXTERNAL)
    r = client_for(other).post(f"{BASE}govt-credit/", {}, format="multipart")
    assert r.status_code == 403
    on.govt_credit_enabled = False
    on.save()
    assert _submit(lab).json()["code"] == "disabled"


def _completed(lab, days_ago=2):
    f = lab["factory"]
    return f.booking(lab["user"], lab["eq"], f.at(timezone.localdate() - timedelta(days=days_ago)), status="COMPLETED",
                     completed_at=timezone.now() - timedelta(hours=3), amount_due=0,
                     charge_breakdown=[{"description": "Analysis", "amount": 100.0}])


def test_invoices_wait_for_settlement_then_go_as_one_consolidated_request(on, lab):
    on.auto_request_without_preference = True
    on.save()
    _submit(lab)
    Client().post("/api/" + _link("accept"))
    first, second = _completed(lab, 2), _completed(lab, 3)
    invoices.queue_completed()
    held = SricInvoiceRequest.objects.filter(booking__in=[first, second])
    assert set(held.values_list("status", flat=True)) == {InvoiceRequestStatus.HELD_CREDIT}
    mail.outbox.clear()
    assert sric_dispatch.run_batch(_working_day_at(9, 30))["sent"] == 0 and not mail.outbox  # left out of 09:30

    finance = make_user(UserType.FINANCE, is_staff=True)
    req = GovtCreditRequest.objects.get()
    assert client_for(lab["user"]).post(f"{BASE}govt-credit/{req.public_id}/settle/", {"reference": "x"}).status_code == 403
    r = client_for(finance).post(f"{BASE}govt-credit/{req.public_id}/settle/", {"reference": "SRIC/RCPT/77"})
    assert r.status_code == 200 and r.json()["status"] == GovtCreditStatus.SETTLED
    assert set(held.values_list("status", flat=True)) == {InvoiceRequestStatus.QUEUED}
    held.update(created_at=timezone.now() - timedelta(days=2))
    mail.outbox.clear()
    result = sric_dispatch.run_batch(_working_day_at(9, 30) + timedelta(days=7))
    assert result["sent"] == 2 and len(mail.outbox) == 1
    assert mail.outbox[0].subject.startswith("[TEST] Consolidated GST Invoice Request")


def test_early_invoice_is_refused_while_the_credit_is_open(on, lab):
    _submit(lab)
    Client().post("/api/" + _link("accept"))
    assert govt_credit.open_credit(lab["user"]) is not None
    booking = _completed(lab)
    with pytest.raises(invoices.InvoiceError):
        invoices.request_early(lab["user"], booking.pk)


def test_advance_payer_chooses_consolidated_and_releases_it(on, factory):
    on.auto_request_without_preference = True
    on.save()
    user = make_user(UserType.EXTERNAL)
    _billing(user)
    lab = {"user": user, "dept": factory.department, "eq": factory.equipment(), "factory": factory}
    r = client_for(user).post(f"{BASE}invoice-mode/", {"mode": "consolidated"}, format="json")
    assert r.status_code == 200 and r.json()["mode"] == "consolidated"
    b1 = _completed(lab)
    SricInvoiceRequest.objects.all().delete()
    type(b1).objects.filter(pk=b1.pk).update(payment_settled_at=timezone.now())
    invoices.queue_completed()
    assert SricInvoiceRequest.objects.get(booking=b1).status == InvoiceRequestStatus.HELD_CONSOLIDATION
    r = client_for(user).post(f"{BASE}invoice-mode/consolidated/release/")
    assert r.status_code == 200 and r.json()["released"] == 1
    req = SricInvoiceRequest.objects.get(booking=b1)
    assert req.status == InvoiceRequestStatus.QUEUED and req.consolidation_group


def test_per_booking_is_the_default_and_joins_the_batch(on, factory):
    on.auto_request_without_preference = True
    on.save()
    user = make_user(UserType.EXTERNAL)
    _billing(user)
    lab = {"user": user, "dept": factory.department, "eq": factory.equipment(), "factory": factory}
    booking = _completed(lab)
    type(booking).objects.filter(pk=booking.pk).update(payment_settled_at=timezone.now())
    invoices.queue_completed()
    req = SricInvoiceRequest.objects.get(booking=booking)
    assert req.status == InvoiceRequestStatus.QUEUED and req.consolidation_group == ""

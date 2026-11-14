"""Refunds to a bank account (14 Nov 2026): the user's confirmation, the SRIC request, the settlement
(``external_services.bank_refunds``)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core import mail
from django.utils import timezone

from iic_booking.external_services import bank_refunds, refunds
from iic_booking.external_services.models import (
    ExternalNotification,
    ExternalRefund,
    ExternalServicesSettings,
    RefundStatus,
    SricInvoiceRequest,
)
from iic_booking.users.models.user_type import UserType

from .conftest import client_for, give_wallet, main_admin, make_user

pytestmark = pytest.mark.django_db
API = "/api/v1/external-booking/"
ADMIN_API = "/api/v1/admin/external-booking/"
BREAKDOWN = [{"description": "Analysis charge", "amount": 1000.0}, {"description": "GST (18%)", "amount": 180.0}]
ACCOUNT = {"account_holder_name": "Asha Verma", "bank_name": "State Bank of India", "account_number": "123456789012",
           "account_number_confirm": "123456789012", "ifsc": "SBIN0001234", "branch": "Roorkee"}


@pytest.fixture(autouse=True)
def _mail(settings, monkeypatch):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    from iic_booking.external_services import tasks

    monkeypatch.setattr(tasks.send_bank_refund, "delay", lambda pk: bank_refunds.send(pk))


@pytest.fixture
def live(db):
    """Policy on, sending in test mode to the refund test address, immediate timing."""
    s = ExternalServicesSettings.load()
    s.cancellation_policy_enabled = True
    s.sric_dispatch_mode = "test"
    s.refund_sric_test_recipients = "refunds.test@example.org"
    s.refund_dispatch_timing = "immediate"
    s.save()
    return s


@pytest.fixture
def user(db):
    return make_user(name="Asha Verma", phone_number="+919800000000")


def _booking(factory, user, hours=72, **fields):
    eq = factory.equipment()
    give_wallet(user, factory.department, "0.00")
    return factory.booking(user, eq, timezone.now() + timedelta(hours=hours), total="1180.00",
                           charge_breakdown=BREAKDOWN, return_shipping_fee_amount=Decimal("0"), **fields)


def _cancel_to_bank(user, booking, *, captured):
    acc = refunds.save_account(user, ACCOUNT)
    with captured(execute=True):
        refunds.cancel(user, booking.pk, destination="bank", bank_account_id=acc.pk, account_confirmed=True)
    return ExternalRefund.objects.get(booking=booking)


def _refund_mails():
    return [m for m in mail.outbox if "refunds.test@example.org" in m.to]


def test_confirmed_bank_refund_is_emailed_to_sric_at_once_with_every_detail(live, factory, user,
                                                                           django_capture_on_commit_callbacks):
    b = _booking(factory, user)
    refund = _cancel_to_bank(user, b, captured=django_capture_on_commit_callbacks)
    refund.refresh_from_db()
    assert refund.status == RefundStatus.SENT and refund.sent_in_mode == "test"
    sent = _refund_mails()
    assert len(sent) == 1
    body = sent[0].body
    assert sent[0].subject.startswith("[TEST] Refund to bank account")
    for expected in ("123456789012", "SBIN0001234", "Asha Verma", factory.department.name, b.equipment.name,
                     "AMOUNT TO TRANSFER: Rs. 1,180.00", "Full refund", "ORIGINAL PAYMENT", "+919800000000"):
        assert expected in body, expected
    assert refund.sric_body == body  # what was sent is kept for the record


def test_base_only_entitlement_lists_the_gst_kept(live, factory, user, django_capture_on_commit_callbacks):
    b = _booking(factory, user)
    SricInvoiceRequest.objects.create(booking=b, trigger="early", snapshot={})
    refund = _cancel_to_bank(user, b, captured=django_capture_on_commit_callbacks)
    assert refund.refund_amount == Decimal("1000.00") and refund.gst_refunded is False
    assert bank_refunds.entitlement(refund)["code"] == "base_only"
    ded = bank_refunds.deductions(refund)
    assert [(d["code"], d["amount"]) for d in ded] == [("gst", "180.00")]
    assert "GST invoice was issued" in _refund_mails()[0].body


def test_penalty_and_gst_deductions_add_up_to_what_was_kept(live, factory, user, django_capture_on_commit_callbacks):
    b = _booking(factory, user, hours=36)  # 50% penalty
    refund = _cancel_to_bank(user, b, captured=django_capture_on_commit_callbacks)
    ded = {d["code"]: Decimal(d["amount"]) for d in bank_refunds.deductions(refund)}
    assert ded == {"service": Decimal("500.00"), "gst": Decimal("90.00")}
    assert sum(ded.values()) == refund.paid_amount - refund.refund_amount


def test_nothing_is_sent_while_sric_sending_is_off(live, factory, user, django_capture_on_commit_callbacks):
    live.sric_dispatch_mode = "off"
    live.save()
    refund = _cancel_to_bank(user, _booking(factory, user), captured=django_capture_on_commit_callbacks)
    refund.refresh_from_db()
    assert refund.status == RefundStatus.QUEUED and not _refund_mails()


def test_lab_failure_to_bank_waits_for_the_user_to_confirm_an_account(live, factory, user,
                                                                      django_capture_on_commit_callbacks):
    b = _booking(factory, user)
    admin = main_admin()
    out = refunds.refund_lab_failure(admin, b.pk, kind="equipment_failure", reason="Detector failed during the run.",
                                     destination="bank")
    refund = ExternalRefund.objects.get(pk=out["refund"]["id"])
    assert refund.status == RefundStatus.AWAITING_ACCOUNT and refund.bank_account_id is None
    assert refund.wallet_transaction_id is None and out["refund"]["needs_account"] is True
    assert not _refund_mails()
    acc = refunds.save_account(user, ACCOUNT)
    url = f"{API}refunds/{refund.pk}/bank-account/"
    no_tick = client_for(user).post(url, {"bank_account_id": acc.pk}, format="json")
    assert no_tick.status_code == 400 and no_tick.json()["code"] == "confirmation_required"
    other = make_user()
    assert client_for(other).post(url, {"bank_account_id": acc.pk, "account_confirmed": True},
                                  format="json").status_code == 404
    with django_capture_on_commit_callbacks(execute=True):
        ok = client_for(user).post(url, {"bank_account_id": acc.pk, "account_confirmed": True}, format="json")
    assert ok.status_code == 200, ok.content
    refund.refresh_from_db()
    assert refund.status == RefundStatus.SENT and refund.bank_snapshot["account_number"] == "123456789012"
    again = client_for(user).post(url, {"bank_account_id": acc.pk, "account_confirmed": True}, format="json")
    assert again.status_code == 409


def test_settlement_rules_and_the_user_is_told_the_deductions(live, factory, user, django_capture_on_commit_callbacks):
    b = _booking(factory, user, hours=36)
    refund = _cancel_to_bank(user, b, captured=django_capture_on_commit_callbacks)
    accounts = make_user(UserType.FINANCE, department=factory.department)
    stranger = make_user(UserType.FINANCE)  # another department's Accounts In Charge
    url = f"{API}staff/refunds/{refund.pk}/close/"
    assert client_for(stranger).post(url, {"utr": "X1"}, format="json").status_code == 403
    too_much = client_for(accounts).post(url, {"utr": "X1", "amount": "9999"}, format="json")
    assert too_much.status_code == 400 and too_much.json()["code"] == "invalid_amount"
    future = (timezone.localdate() + timedelta(days=2)).isoformat()
    assert client_for(accounts).post(url, {"utr": "X1", "settled_on": future}, format="json").status_code == 400
    ok = client_for(accounts).post(url, {"utr": "SBIN26111400001", "mode": "neft",
                                         "settled_on": timezone.localdate().isoformat()}, format="json")
    assert ok.status_code == 200, ok.content
    refund.refresh_from_db()
    assert refund.status == RefundStatus.CLOSED and refund.settled_amount == refund.refund_amount
    note = ExternalNotification.objects.filter(recipient=user, key=f"refund:{refund.pk}:closed").first()
    assert note is not None
    assert "Cancellation penalty" in note.message and "SBIN26111400001" in note.message
    assert note.link == f"/my-refunds?refund={refund.pk}"
    mine = client_for(user).get(f"{API}refunds/").json()["refunds"][0]
    assert mine["settled_amount"] == "590.00" and mine["utr"] == "SBIN26111400001"
    assert {d["code"] for d in mine["deductions"]} == {"service", "gst"}


def test_staff_listing_is_per_department_and_main_admin_sees_all(live, factory, user, django_capture_on_commit_callbacks):
    refund = _cancel_to_bank(user, _booking(factory, user), captured=django_capture_on_commit_callbacks)
    own = make_user(UserType.FINANCE, department=factory.department)
    stranger = make_user(UserType.FINANCE)
    assert client_for(own).get(f"{API}staff/refunds/").json()["count"] == 1
    assert client_for(stranger).get(f"{API}staff/refunds/").json()["count"] == 0
    detail = client_for(main_admin()).get(f"{API}staff/refunds/{refund.pk}/")
    assert detail.status_code == 200
    body = detail.json()
    assert body["context"]["department"] == factory.department.name and "123456789012" in body["sric_body"]


def test_interrupted_send_is_never_resent_automatically(live, factory, user, django_capture_on_commit_callbacks):
    live.sric_dispatch_mode = "off"
    live.save()
    refund = _cancel_to_bank(user, _booking(factory, user), captured=django_capture_on_commit_callbacks)
    ExternalRefund.objects.filter(pk=refund.pk).update(status=RefundStatus.SENDING,
                                                       claimed_at=timezone.now() - timedelta(hours=1))
    live.sric_dispatch_mode = "test"
    live.save()
    from iic_booking.external_services.sric_refunds import send_due

    result = send_due()
    assert result["refunds_uncertain"] == 1 and result["refunds_sent"] == 0
    assert ExternalRefund.objects.get(pk=refund.pk).status == RefundStatus.UNCERTAIN
    resent = client_for(main_admin()).post(f"{API}staff/refunds/{refund.pk}/resend/", {}, format="json")
    assert resent.status_code == 200 and resent.json()["status"] == RefundStatus.SENT


def test_main_admin_edits_the_template_with_checked_placeholders(live, factory, user,
                                                                 django_capture_on_commit_callbacks):
    admin = main_admin()
    bad = client_for(admin).patch(f"{ADMIN_API}settings/", {"refund_email_subject": "Refund {nonsense}",
                                                            "reason": "Testing the refund template"}, format="json")
    assert bad.status_code == 400
    bad_mail = client_for(admin).patch(f"{ADMIN_API}settings/", {"refund_sric_recipients": "not-an-address",
                                                                 "reason": "Testing the refund template"}, format="json")
    assert bad_mail.status_code == 400
    ok = client_for(admin).patch(f"{ADMIN_API}settings/", {
        "refund_email_subject": "SRIC refund {booking_reference} for {user_name}",
        "refund_email_body": "Dear SRIC,\n\nPlease pay Rs. {refund_amount} to {account_holder}.",
        "refund_sric_cc": "accounts@example.org, accounts@example.org",
        "reason": "Testing the refund template",
    }, format="json")
    assert ok.status_code == 200, ok.content
    s = ExternalServicesSettings.load()
    assert s.refund_sric_cc == "accounts@example.org"  # de-duplicated, one per line
    b = _booking(factory, user)
    _cancel_to_bank(user, b, captured=django_capture_on_commit_callbacks)
    sent = _refund_mails()[0]
    assert sent.subject.startswith("[TEST] SRIC refund ") and "Asha Verma" in sent.subject
    assert "Please pay Rs. 1,180.00 to Asha Verma." in sent.body
    assert "BANK ACCOUNT" in sent.body  # the details block is always added
    preview = client_for(admin).post(f"{ADMIN_API}refund-email/", {"refund_email_subject": "P {booking_reference}"},
                                     format="json")
    assert preview.status_code == 200 and preview.json()["subject"] == f"[TEST] P {b.virtual_booking_id or b.pk}"
    assert client_for(user).post(f"{ADMIN_API}refund-email/", {}, format="json").status_code == 403


def test_weekly_timing_holds_the_refund_until_the_batch(live, factory, user, django_capture_on_commit_callbacks):
    live.refund_dispatch_timing = "weekly"
    live.save()
    refund = _cancel_to_bank(user, _booking(factory, user), captured=django_capture_on_commit_callbacks)
    refund.refresh_from_db()
    assert refund.status == RefundStatus.QUEUED and not _refund_mails()
    from iic_booking.external_services.sric_refunds import send_due

    assert send_due()["refunds_sent"] == 0  # not in a batch yet

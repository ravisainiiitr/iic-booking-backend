"""Wallet emails show request / transfer times in IST, not the stored UTC."""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.core import mail
from django.test import RequestFactory
from django.urls import reverse

from iic_booking.communication.service import CommunicationService
from iic_booking.users.models import Department, DepartmentType, UserType, Wallet
from iic_booking.users.models.wallet import WalletRechargeMode, WalletRechargeRequest
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

# 19:00 UTC on 05 Oct is 00:30 IST on 06 Oct.
CREATED_UTC = datetime(2026, 10, 5, 19, 0, tzinfo=dt_timezone.utc)
CREATED_IST = "2026-10-06 00:30:00"


@pytest.fixture
def emails(monkeypatch):
    sent = []

    def _email(recipient=None, template=None, template_context=None, **kwargs):
        sent.append(SimpleNamespace(recipient=recipient, template=template, ctx=dict(template_context or {})))

    monkeypatch.setattr(CommunicationService, "send_email", staticmethod(_email))
    monkeypatch.setattr(CommunicationService, "send_push_notification", staticmethod(lambda *a, **k: None))
    return sent


@pytest.fixture
def recharge_request():
    dept = Department.objects.create(name="TZ Dept", code="TZD", department_type=DepartmentType.INTERNAL)
    faculty = UserFactory(user_type=UserType.FACULTY, name="Ravi Kumar", department=dept, admin_approved=True)
    wallet, _ = Wallet.objects.get_or_create(user=faculty)
    req = WalletRechargeRequest.objects.create(
        user=faculty, wallet=wallet, department=dept, amount=Decimal("500.00"), user_otp_verified=True,
        recharge_mode=WalletRechargeMode.PROJECT_GRANT, employee_number="E1",
        department_grant_code="IIC-000-002", project_grant_code="PRJ-1",
    )
    WalletRechargeRequest.objects.filter(pk=req.pk).update(created_at=CREATED_UTC)
    req.refresh_from_db()
    return req


def test_recharge_request_notification_date_in_ist(recharge_request, emails):
    from iic_booking.communication.wallet_notifications import send_wallet_recharge_request_notifications

    send_wallet_recharge_request_notifications(recharge_request, "PENDING")

    assert emails[0].ctx["request_date"] == CREATED_IST


def test_credit_facility_activated_email_date_in_ist(recharge_request, emails):
    from iic_booking.communication.wallet_notifications import send_wallet_credit_facility_activated_user_email

    send_wallet_credit_facility_activated_user_email(recharge_request)

    assert emails[0].ctx["request_date"] == CREATED_IST


def test_sric_faculty_recharge_context_date_in_ist(recharge_request):
    from iic_booking.users.wallet_recharge_ops import sric_faculty_recharge_email_context

    ctx = sric_faculty_recharge_email_context(RequestFactory().get("/"), recharge_request)

    assert ctx["request_date"] == CREATED_IST
    assert ctx["request_date_display"] == "6th October 2026 at 00:30:00"


def _admin_client(client):
    admin = UserFactory(user_type=UserType.ADMIN, is_staff=True, is_superuser=True, admin_approved=True)
    client.force_login(admin)
    return client


def test_admin_resend_view_email_date_in_ist(recharge_request, emails, client):
    url = reverse("admin:users_walletrechargerequest_resend_notification", args=[recharge_request.pk])

    _admin_client(client).get(url)

    bodies = [m.body for m in mail.outbox]
    assert any(f"- Request Date: {CREATED_IST}" in b for b in bodies), bodies


def test_admin_resend_action_email_date_in_ist(recharge_request, emails, client):
    url = reverse("admin:users_walletrechargerequest_changelist")

    _admin_client(client).post(url, {"action": "resend_notifications", "_selected_action": [recharge_request.pk]})

    bodies = [m.body for m in mail.outbox]
    assert any(f"- Request Date: {CREATED_IST}" in b for b in bodies), bodies


def test_peer_transfer_email_time_in_ist(monkeypatch):
    from iic_booking.users import wallet_peer_transfer

    monkeypatch.setattr(wallet_peer_transfer, "peer_transfer_staff_emails", lambda transfer: [])
    sender = UserFactory(user_type=UserType.STUDENT, email="sender@example.com")
    recipient = UserFactory(user_type=UserType.STUDENT, email="recipient@example.com")
    transfer = SimpleNamespace(
        completed_at=CREATED_UTC, transaction_id="WPT-1", amount=Decimal("10.00"), sender=sender,
        recipient=recipient, grant_code="", department_id=None, department=None, remarks="",
        initiated_by_id=None, initiated_by=None, sender_balance_after=Decimal("0"),
        recipient_balance_after=Decimal("10.00"),
    )

    wallet_peer_transfer.notify_peer_transfer_completed(transfer)

    assert mail.outbox
    assert all(f"Date and Time: {CREATED_IST}" in m.body for m in mail.outbox)

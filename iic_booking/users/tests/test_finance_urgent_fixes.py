"""Retired SBIePay return addresses and the SRIC email origin rule for a relay that records no address."""

from __future__ import annotations

import email
from decimal import Decimal
from types import SimpleNamespace

import pytest
from rest_framework.test import APIClient

from iic_booking.users.models import Department, UserType
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.payment import PaymentGatewayStatus, PaymentGatewayTransaction, PaymentPurpose
from iic_booking.users.models.wallet import SubWallet, SubWalletTransaction, Wallet
from iic_booking.users.sric_wallet_mail_auth import check_message
from iic_booking.users.tests.factories import UserFactory

SENDER = "no-reply@sric.iitr.ac.in"
CONFIG = SimpleNamespace(sender_email=SENDER, trusted_authserv_ids="", require_internal_relay=True, trusted_relay_ranges="",
                         gateway_marker_header="", gateway_marker_value="")


def _message(*hops: str):
    lines = [f"From: {SENDER}", f"Return-Path: <{SENDER}>", *[f"Received: {h}" for h in hops], "", "body"]
    return email.message_from_string("\n".join(lines))


def test_internal_relays_still_pass():
    assert check_message(_message("from a.iitr.ac.in (a.iitr.ac.in [10.1.2.3]) by mail.iitr.ac.in"), CONFIG).authenticated


def test_a_hop_made_on_the_machine_itself_still_passes():
    hops = ("from a.iitr.ac.in (a.iitr.ac.in [10.1.2.3]) by mail.iitr.ac.in", "by a.iitr.ac.in (Postfix, from userid 33)")
    assert check_message(_message(*hops), CONFIG).authenticated


def test_local_delivery_on_the_same_server_still_passes():
    hops = ("from store.iitr.ac.in (store.iitr.ac.in) by store.iitr.ac.in with LMTP", "from a.iitr.ac.in (a.iitr.ac.in [10.1.2.3]) by store.iitr.ac.in")
    assert check_message(_message(*hops), CONFIG).authenticated


def test_a_relay_that_records_no_address_is_not_trusted():
    result = check_message(_message("from outside.example.org by mail.iitr.ac.in"), CONFIG)
    assert not result.authenticated and "no address" in result.verdict


def test_one_such_relay_among_internal_ones_is_not_trusted():
    hops = ("from a.iitr.ac.in (a.iitr.ac.in [10.1.2.3]) by mail.iitr.ac.in", "from outside.example.org by a.iitr.ac.in")
    assert not check_message(_message(*hops), CONFIG).authenticated


@pytest.fixture
def sbiepay_row(db):
    user = UserFactory(user_type=UserType.FACULTY)
    department = Department.objects.create(name="Retired Gateway Lab", code="RGL", department_type=DepartmentType.INTERNAL)
    wallet = Wallet.objects.create(user=user)
    return PaymentGatewayTransaction.objects.create(
        merchant_order_ref="IIC-TEST-REF", amount=Decimal("500.00"), purpose=PaymentPurpose.WALLET_RECHARGE, user=user,
        wallet=wallet, department=department, status=PaymentGatewayStatus.PENDING)


@pytest.mark.parametrize("path", ["success", "failure"])
@pytest.mark.parametrize("status", [PaymentGatewayStatus.PENDING, PaymentGatewayStatus.SUCCESS, PaymentGatewayStatus.FAILED])
def test_return_addresses_change_nothing(sbiepay_row, path, status):
    PaymentGatewayTransaction.objects.filter(pk=sbiepay_row.pk).update(status=status)
    client = APIClient()
    url = f"/api/payments/sbiepay/{path}/"
    for answer in (client.get(url, {"merchantOrderRef": sbiepay_row.merchant_order_ref}),
                   client.post(url, {"merchant_order_ref": sbiepay_row.merchant_order_ref, "encData": "x|y"})):
        assert answer.status_code == 410
    sbiepay_row.refresh_from_db()
    assert sbiepay_row.status == status
    assert not SubWalletTransaction.objects.exists() and not SubWallet.objects.exists()

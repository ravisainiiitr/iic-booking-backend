"""Wallet-to-wallet transfers stay disabled until the Institute approves user-category combinations (C4)."""

from __future__ import annotations

from datetime import timedelta
import uuid
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.users.models import Department, Wallet, WalletSricSettings
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import SubWallet, WalletPeerTransfer, WalletPeerTransferStatus
from iic_booking.users.models.wallet_sric_settings import (
    approved_peer_transfer_category_pairs,
    peer_transfer_enabled,
)
from iic_booking.users.tests.factories import UserFactory
from iic_booking.users.wallet_peer_transfer import (
    PeerTransferError,
    confirm_transfer_with_otp,
    create_pending_transfer,
)

pytestmark = pytest.mark.django_db

SEND_OTP = "/api/wallet/peer-transfer/send-otp/"
SETTINGS = "/api/wallet/student-recharge/settings/"


@pytest.fixture
def switch_on():
    row = WalletSricSettings.get_singleton()
    row.peer_transfer_enabled = True
    row.save()
    return row


@pytest.fixture
def dept():
    tag = uuid.uuid4().hex[:6].upper()
    return Department.objects.create(name=f"Hotfix Transfer {tag}", code=f"HT{tag}", department_type=DepartmentType.INTERNAL)


def _faculty(dept, email):
    user = UserFactory(email=email, user_type=UserType.FACULTY, department=dept, email_verified=True, admin_approved=True)
    wallet, _ = Wallet.objects.get_or_create(user=user)
    SubWallet.objects.create(wallet=wallet, department=dept, balance=Decimal("1000.00"))
    return user


def test_no_combination_is_approved_by_default(settings):
    assert list(settings.WALLET_TRANSFER_APPROVED_CATEGORY_PAIRS) == []
    assert approved_peer_transfer_category_pairs() == frozenset()


def test_admin_switch_alone_does_not_enable_transfers(switch_on, dept):
    assert peer_transfer_enabled() is False
    assert peer_transfer_enabled(department=dept.id) is False
    sender = _faculty(dept, "sender.c4@test.iitr.ac.in")
    client = APIClient()
    client.force_authenticate(sender)
    assert client.get(SETTINGS).data["peer_transfer_enabled"] is False
    recipient = _faculty(dept, "recipient.c4@test.iitr.ac.in")
    response = client.post(SEND_OTP, {"department_id": dept.id, "recipient_id": recipient.id, "amount": "10"}, format="json")
    assert response.status_code >= 400  # wallet-to-wallet transfers were removed (31 Oct 2026)
    assert not WalletPeerTransfer.objects.exists()


def test_service_refuses_an_unapproved_combination_even_when_called_directly(switch_on, dept):
    sender = _faculty(dept, "sender2.c4@test.iitr.ac.in")
    recipient = _faculty(dept, "recipient2.c4@test.iitr.ac.in")
    with pytest.raises(PeerTransferError) as exc:
        create_pending_transfer(sender=sender, recipient=recipient, department=dept, amount="10")
    assert exc.value.code == "peer_transfer_category_not_approved"
    assert not WalletPeerTransfer.objects.exists()


def test_cross_category_transfer_needs_its_own_approval(settings, switch_on, dept):
    settings.WALLET_TRANSFER_APPROVED_CATEGORY_PAIRS = ["faculty>faculty"]
    sender = _faculty(dept, "sender3.c4@test.iitr.ac.in")
    external = UserFactory(email="ext.c4@example.org", user_type=UserType.EXTERNAL, email_verified=True, admin_approved=True)
    with pytest.raises(PeerTransferError) as exc:
        create_pending_transfer(sender=sender, recipient=external, department=dept, amount="10")
    assert exc.value.code == "peer_transfer_category_not_approved"


def test_transfers_stay_off_even_with_an_approved_combination(settings, switch_on, dept):
    # Wallet-to-wallet transfers between users were removed (31 Oct 2026 committee): no setting turns them back on.
    settings.WALLET_TRANSFER_APPROVED_CATEGORY_PAIRS = ["faculty>faculty"]
    assert peer_transfer_enabled(department=dept.id) is False

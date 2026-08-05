from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.utils import timezone

from iic_booking.communication.service import CommunicationService
from iic_booking.users.models import User
from iic_booking.users.tasks import delete_unverified_user_after_verification_expiry
from iic_booking.users.tasks import expire_wallet_credit_facilities
from iic_booking.users.tasks import send_wallet_low_balance_alerts
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def test_delete_unverified_user_after_verification_expiry_deletes_stale_user():
    user = UserFactory(email_verified=False)
    user.verification_email_sent_at = timezone.now() - timedelta(minutes=11)
    user.save(update_fields=["verification_email_sent_at"])

    deleted = delete_unverified_user_after_verification_expiry(user.id)
    assert deleted is True
    assert not User.objects.filter(pk=user.pk).exists()


def test_delete_unverified_user_after_verification_expiry_keeps_verified_user():
    user = UserFactory(email_verified=True)
    user.verification_email_sent_at = timezone.now() - timedelta(minutes=11)
    user.save(update_fields=["verification_email_sent_at"])

    deleted = delete_unverified_user_after_verification_expiry(user.id)
    assert deleted is False
    assert User.objects.filter(pk=user.pk).exists()


def test_send_wallet_low_balance_alerts_sends_email_when_below_threshold(monkeypatch):
    class FakeQuerySet(list):
        def select_related(self, *_args, **_kwargs):
            return self

    fake_user = SimpleNamespace(
        wallet_low_balance_alert_enabled=True,
        wallet_low_balance_alert_threshold=Decimal("100.00"),
        wallet=SimpleNamespace(total_balance=Decimal("50.00")),
        name="Test User",
        email="test@example.com",
    )
    captured = []

    monkeypatch.setattr(User.objects, "filter", lambda **_kwargs: FakeQuerySet([fake_user]))
    monkeypatch.setattr(CommunicationService, "send_email", lambda **kwargs: captured.append(kwargs))

    sent = send_wallet_low_balance_alerts()
    assert sent == 1
    assert len(captured) == 1
    assert captured[0]["template"] == "wallet_low_balance_email"


def test_expire_wallet_credit_facilities_delegates_to_domain_logic(monkeypatch):
    monkeypatch.setattr(
        "iic_booking.users.wallet_credit_facility.expire_due_wallet_credit_facilities",
        lambda: 3,
    )

    expired = expire_wallet_credit_facilities()
    assert expired == 3

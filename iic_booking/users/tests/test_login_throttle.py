"""Password login: failed attempts are limited per account and per client address."""

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from iic_booking.users import login_throttle
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

URL = "/api/auth/login/"


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _login(email, password, ip="203.0.113.7"):
    return APIClient().post(URL, {"email": email, "password": password}, format="json",
                            HTTP_X_FORWARDED_FOR=f"198.51.100.1, {ip}")


def test_unknown_emails_count_as_failures():
    for i in range(login_throttle.MAX_FAILURES_PER_ACCOUNT):
        assert _login("nobody@example.com", "guess", ip=f"203.0.113.{i + 10}").status_code == 401
    assert _login("nobody@example.com", "guess").status_code == 429


def test_too_many_failures_for_an_account_are_refused_even_with_the_right_password():
    UserFactory(email="target@example.com", password="Right-pass-123")
    for i in range(login_throttle.MAX_FAILURES_PER_ACCOUNT):
        assert _login("target@example.com", f"guess-{i}", ip=f"203.0.113.{i + 10}").status_code in (401, 403)
    blocked = _login("target@example.com", "Right-pass-123")
    assert blocked.status_code == 429


def test_failures_from_one_address_are_limited_across_accounts(settings):
    for i in range(login_throttle.MAX_FAILURES_PER_ADDRESS):
        _login(f"user{i}@example.com", "guess")
    assert _login("another@example.com", "guess").status_code == 429
    assert _login("another@example.com", "guess", ip="203.0.113.99").status_code == 401  # other address unaffected


def test_a_spoofed_forwarded_for_does_not_reset_the_address_count():
    for i in range(login_throttle.MAX_FAILURES_PER_ADDRESS):
        APIClient().post(URL, {"email": f"u{i}@example.com", "password": "x"}, format="json",
                         HTTP_X_FORWARDED_FOR=f"10.0.0.{i}, 203.0.113.7")
    assert _login("other@example.com", "x").status_code == 429


def test_wrong_passwords_on_an_expired_programme_also_count():
    from datetime import timedelta

    from django.utils import timezone

    from iic_booking.users.models.user_type import UserType

    user = UserFactory(email="expired@example.com", password="Right-pass-123", user_type=UserType.STUDENT)
    user.program_end_date = timezone.localdate() - timedelta(days=1)
    user.save(update_fields=["program_end_date"])
    for i in range(login_throttle.MAX_FAILURES_PER_ACCOUNT):
        _login("expired@example.com", f"guess-{i}", ip=f"203.0.113.{i + 10}")
    assert _login("expired@example.com", "Right-pass-123").status_code == 429

"""Personal notification addresses live only in the database or env, never as code defaults."""

from __future__ import annotations

import importlib

import pytest
from django.apps import apps

from iic_booking.users import test_accounts
from iic_booking.users.models.test_account_email_settings import TestAccountEmailSettings
from iic_booking.users.models.wallet_sric_settings import WalletSricSettings
from iic_booking.users.wallet_recharge_workflow import get_sric_bill_section_emails

FORCED = "test.student@iic-booking.test"


@pytest.fixture
def no_env_redirect(settings):
    settings.TEST_ACCOUNT_EMAIL_REDIRECT = ""


@pytest.mark.django_db
def test_test_account_singleton_starts_empty_and_is_not_refilled(no_env_redirect):
    cfg = TestAccountEmailSettings.get_singleton()
    assert cfg.recipient_emails == ""
    assert TestAccountEmailSettings.get_singleton().recipient_emails == ""
    assert test_accounts.email_redirects() == []


@pytest.mark.django_db
def test_stored_test_redirects_are_used_and_kept(no_env_redirect):
    TestAccountEmailSettings.objects.create(pk=1, recipient_emails="qa@example.org\nlead@example.org")
    assert test_accounts.email_redirects() == ["qa@example.org", "lead@example.org"]
    assert test_accounts.redirect_email_address(FORCED)[0] == ["qa@example.org", "lead@example.org"]
    assert TestAccountEmailSettings.get_singleton().recipient_emails == "qa@example.org\nlead@example.org"


@pytest.mark.django_db
def test_env_redirect_is_the_fallback_when_db_is_empty(settings):
    settings.TEST_ACCOUNT_EMAIL_REDIRECT = "env-qa@example.org; env-two@example.org"
    TestAccountEmailSettings.get_singleton()
    assert test_accounts.email_redirects() == ["env-qa@example.org", "env-two@example.org"]


@pytest.mark.django_db
def test_forced_test_login_mail_is_dropped_when_nothing_configured(no_env_redirect):
    assert test_accounts.redirect_email_address(FORCED, subject="Subj") == ([], "Subj")


@pytest.mark.django_db
def test_wallet_sric_singleton_starts_without_bill_section_and_is_not_refilled(settings):
    settings.ACCOUNTS_EMAIL = "accounts@example.org"
    cfg = WalletSricSettings.get_singleton()
    assert cfg.bill_section_emails == ""
    assert WalletSricSettings.get_singleton().bill_section_emails == ""
    assert get_sric_bill_section_emails() == ["accounts@example.org"]


@pytest.mark.django_db
def test_stored_bill_section_is_used_and_kept():
    cfg = WalletSricSettings.get_singleton()
    cfg.bill_section_emails = "bills@example.org"
    cfg.save()
    assert get_sric_bill_section_emails() == ["bills@example.org"]
    assert WalletSricSettings.get_singleton().bill_section_emails == "bills@example.org"


@pytest.mark.django_db
def test_data_migrations_seed_empty_rows_and_leave_existing_values():
    m94 = importlib.import_module("iic_booking.users.migrations.0094_test_account_email_settings")
    m112 = importlib.import_module("iic_booking.users.migrations.0112_seed_bill_section_routing_email")

    TestAccountEmailSettings.objects.all().delete()
    WalletSricSettings.objects.all().delete()
    m94.seed_default_redirect(apps, None)
    m112.seed_bill_section_email(apps, None)
    assert TestAccountEmailSettings.objects.get(pk=1).recipient_emails == ""
    assert WalletSricSettings.objects.get(pk=1).bill_section_emails == ""

    TestAccountEmailSettings.objects.filter(pk=1).update(recipient_emails="qa@example.org")
    WalletSricSettings.objects.filter(pk=1).update(bill_section_emails="bills@example.org")
    m94.seed_default_redirect(apps, None)
    m112.seed_bill_section_email(apps, None)
    assert TestAccountEmailSettings.objects.get(pk=1).recipient_emails == "qa@example.org"
    assert WalletSricSettings.objects.get(pk=1).bill_section_emails == "bills@example.org"


def test_no_address_defaults_in_code():
    import inspect
    import re

    from config.settings import base
    from iic_booking.users.models import test_account_email_settings, wallet_sric_settings

    for module in (test_accounts, test_account_email_settings, wallet_sric_settings):
        assert "@gmail.com" not in inspect.getsource(module)
    assert re.search(r'TEST_ACCOUNT_EMAIL_REDIRECT = env\("TEST_ACCOUNT_EMAIL_REDIRECT", default=""\)', inspect.getsource(base))

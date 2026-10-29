"""Deploys do not migrate (RELEASING.md): until this app's migrations run, the code must behave as before instead of
failing. The missing tables are simulated by making their queries raise a database error."""

from datetime import timedelta

import pytest
from django.db import DatabaseError
from django.utils import timezone

from iic_booking.external_services import special_payment, window
from iic_booking.external_services.models import ExternalBookingWindowConfig, SpecialAllocation

from .conftest import enable_window, make_user

pytestmark = pytest.mark.django_db


def _raise(*args, **kwargs):
    raise DatabaseError('relation "external_services_..." does not exist')


def test_window_reads_as_disabled_when_its_table_is_missing(monkeypatch):
    enable_window()
    monkeypatch.setattr(ExternalBookingWindowConfig.objects, "filter", _raise)
    assert window.external_window() is window.DISABLED


def test_quota_count_falls_back_without_special_allocations(factory, monkeypatch):
    from iic_booking.equipment.external_slot_quota import ExternalSlotQuotaService

    eq = factory.equipment()
    day = timezone.localdate() + timedelta(days=3)
    factory.booking(make_user(), eq, factory.at(day))
    week_start, week_end = ExternalSlotQuotaService.week_bounds_for_date(day)
    normal = ExternalSlotQuotaService.count_external_usage(eq, week_start, week_end)
    real_filter = type(SpecialAllocation.objects.none()).filter

    def failing_filter(self, *args, **kwargs):
        if any("external_special_allocation" in str(a) for a in args):
            raise DatabaseError("missing table")
        return real_filter(self, *args, **kwargs)

    monkeypatch.setattr(type(SpecialAllocation.objects.none()), "filter", failing_filter)
    assert ExternalSlotQuotaService.count_external_usage(eq, week_start, week_end) == normal == 1


def test_payment_hooks_treat_bookings_as_ordinary_when_the_table_is_missing(factory, monkeypatch):
    booking = factory.booking(make_user(), factory.equipment(), factory.at(timezone.localdate() + timedelta(days=3)))
    monkeypatch.setattr(SpecialAllocation.objects, "filter", _raise)
    assert special_payment.payment_refusal(booking) is None
    from django.db import transaction

    with transaction.atomic():
        assert special_payment.settlement_hook(booking) is True

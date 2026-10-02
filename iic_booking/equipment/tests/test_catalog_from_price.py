"""Catalog cards: "from ₹X" for the viewer's own user type."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.signals import request_started
from django.db import connection, reset_queries
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from iic_booking.equipment.models import ChargeProfile, EquipmentProfileType, MultiParamDefinition
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def _rows(client):
    resp = client.get("/api/equipments/")
    assert resp.status_code == 200
    return {row["equipment_id"]: row for row in resp.data["equipments"]}


def test_student_sees_own_rate_and_external_sees_theirs(egs_factory):
    eq = egs_factory.equipment(unit_charge="250.00")
    ChargeProfile.objects.create(
        equipment=eq,
        user_type=UserType.INSTITUTE,
        profile_type=EquipmentProfileType.SAMPLE,
        primary_unit_charge=Decimal("4000"),
    )

    student_row = _rows(egs_factory.client_for(egs_factory.student()))[eq.pk]
    assert student_row["from_price"] == "250.00"
    assert student_row["from_price_unit"] == "hour"

    industry = UserFactory(user_type=UserType.INSTITUTE)
    industry_row = _rows(egs_factory.client_for(industry))[eq.pk]
    assert industry_row["from_price"] == "4000.00"
    assert industry_row["from_price_unit"] == "sample"


def test_price_hidden_when_unknown(egs_factory):
    no_profile = egs_factory.equipment(with_profile=False)
    zero = egs_factory.equipment(unit_charge="0.00")
    generic = egs_factory.equipment(with_profile=False)
    ChargeProfile.objects.create(
        equipment=generic,
        user_type=UserType.STUDENT,
        profile_type=EquipmentProfileType.GENERIC,
        primary_unit_charge=Decimal("99"),
    )
    rows = _rows(egs_factory.client_for(egs_factory.student()))
    for eq in (no_profile, zero, generic):
        assert rows[eq.pk]["from_price"] is None
        assert rows[eq.pk]["from_price_unit"] is None

    anonymous = _rows(APIClient())
    assert all(row["from_price"] is None for row in anonymous.values())


def test_multi_param_uses_cheapest_option(egs_factory):
    eq = egs_factory.equipment(with_profile=False)
    ChargeProfile.objects.create(
        equipment=eq,
        user_type=UserType.STUDENT,
        profile_type=EquipmentProfileType.MULTI_PARAM,
        primary_unit_charge=Decimal("0"),
    )
    for code, charge in (("S1", "300"), ("S2", "120"), ("S3", "0")):
        MultiParamDefinition.objects.create(
            equipment=eq,
            user_type=UserType.STUDENT,
            param_name=code,
            param_code=code,
            unit_time_minutes=30,
            unit_charge=Decimal(charge),
        )
    row = _rows(egs_factory.client_for(egs_factory.student()))[eq.pk]
    assert row["from_price"] == "120.00"
    assert row["from_price_unit"] == "sample"


def _query_count(fn):
    request_started.disconnect(reset_queries)
    reset_queries()
    try:
        with CaptureQueriesContext(connection) as ctx:
            fn()
        return len(list(ctx.captured_queries))
    finally:
        request_started.connect(reset_queries)


def test_from_price_query_count_does_not_grow_with_catalog(egs_factory):
    client = egs_factory.client_for(egs_factory.student())
    egs_factory.equipment(unit_charge="50.00")
    client.get("/api/equipments/")
    one = _query_count(lambda: client.get("/api/equipments/"))
    for _ in range(6):
        egs_factory.equipment(unit_charge="50.00")
    client.get("/api/equipments/")  # equipment saves drop the cached peak schedules; re-warm
    many = _query_count(lambda: client.get("/api/equipments/"))
    assert one > 0
    assert many == one

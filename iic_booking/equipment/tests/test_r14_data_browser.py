"""R14 analysis data browser + selection APIs (authorization, virtual IDs)."""

from __future__ import annotations

from decimal import Decimal
import uuid

import pytest
from rest_framework.test import APIClient

from iic_booking.equipment.models import Booking, BookingStatus, ChargeProfile, Equipment
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


def _equipment(**kwargs):
    defaults = {
        "name": "R14 Browser SEM",
        "code": f"BR{uuid.uuid4().hex[:4].upper()}",
        "slot_duration_minutes": 60,
        "user_rating_enabled": False,
        "enable_remote_analysis": True,
    }
    defaults.update(kwargs)
    return Equipment.objects.create(**defaults)


def _booking(owner, equipment, *, virtual=None):
    profile, _ = ChargeProfile.objects.get_or_create(
        equipment=equipment,
        user_type=UserType.STUDENT,
        pricing_profile="standard",
        defaults={"primary_unit_charge": Decimal("10.00")},
    )
    return Booking.objects.create(
        user=owner,
        equipment=equipment,
        charge_profile=profile,
        status=BookingStatus.COMPLETED,
        total_charge=Decimal("10.00"),
        total_time_minutes=60,
        virtual_booking_id=virtual or f"IIC{equipment.code}2026{uuid.uuid4().hex[:5].upper()}",
    )


@pytest.mark.django_db
def test_data_browser_requires_auth(client):
    response = client.get("/api/v1/bookings/1/analysis/data-browser/")
    assert response.status_code in (401, 403)


@pytest.mark.django_db
def test_current_scope_returns_virtual_id_not_numeric_heading():
    owner = UserFactory()
    equipment = _equipment()
    booking = _booking(owner, equipment, virtual="IICAPREO202600005")
    client = APIClient()
    client.force_authenticate(user=owner)
    res = client.get(f"/api/v1/bookings/{booking.pk}/analysis/data-browser/?scope=current")
    assert res.status_code == 200
    body = res.json()
    assert body["datasets"]
    row = body["datasets"][0]
    assert row["virtual_booking_id"] == "IICAPREO202600005"
    assert row["booking_id"] == "IICAPREO202600005"
    assert row["is_current"] is True
    # Internal pk is present for selection but must not be the display id.
    assert row["booking_pk"] == booking.pk
    assert row["booking_id"] != str(booking.pk)


@pytest.mark.django_db
def test_other_user_cannot_browse_or_select():
    owner = UserFactory()
    stranger = UserFactory()
    equipment = _equipment()
    booking = _booking(owner, equipment, virtual="IICSECRET202600001")
    client = APIClient()
    client.force_authenticate(user=stranger)
    listed = client.get(f"/api/v1/bookings/{booking.pk}/analysis/data-browser/?scope=current")
    assert listed.status_code == 403
    selected = client.post(
        f"/api/v1/bookings/{booking.pk}/analysis/data-selection/",
        {"source_booking_id": booking.pk},
        format="json",
    )
    assert selected.status_code == 403


@pytest.mark.django_db
def test_cannot_select_another_users_booking_as_source():
    owner = UserFactory()
    other = UserFactory()
    equipment = _equipment()
    mine = _booking(owner, equipment, virtual="IICMINE202600001")
    theirs = _booking(other, equipment, virtual="IICTHEIRS202600001")
    client = APIClient()
    client.force_authenticate(user=owner)
    res = client.post(
        f"/api/v1/bookings/{mine.pk}/analysis/data-selection/",
        {"source_booking_id": theirs.pk},
        format="json",
    )
    assert res.status_code == 403
    mine.refresh_from_db()
    assert mine.analysis_data_selection in ({}, None)


@pytest.mark.django_db
def test_previous_scope_same_equipment_only():
    owner = UserFactory()
    eq_a = _equipment(name="SEM A")
    eq_b = _equipment(name="SEM B")
    previous = _booking(owner, eq_a, virtual="IICPREV202600008")
    other_eq = _booking(owner, eq_b, virtual="IICOTHEREQ202600007")
    current = _booking(owner, eq_a, virtual="IICCUR202600009")
    client = APIClient()
    client.force_authenticate(user=owner)
    res = client.get(f"/api/v1/bookings/{current.pk}/analysis/data-browser/?scope=previous")
    assert res.status_code == 200
    ids = {row["virtual_booking_id"] for row in res.json()["datasets"]}
    assert "IICPREV202600008" in ids
    assert "IICCUR202600009" not in ids
    assert "IICOTHEREQ202600007" not in ids
    assert previous.pk
    assert other_eq.pk


@pytest.mark.django_db
def test_search_matches_virtual_booking_id():
    owner = UserFactory()
    equipment = _equipment()
    previous = _booking(owner, equipment, virtual="IICNEEDLE202600222")
    current = _booking(owner, equipment, virtual="IICSEARCH202600111")
    client = APIClient()
    client.force_authenticate(user=owner)
    res = client.get(
        f"/api/v1/bookings/{current.pk}/analysis/data-browser/?scope=previous&q=NEEDLE"
    )
    assert res.status_code == 200
    ids = [row["virtual_booking_id"] for row in res.json()["datasets"]]
    assert ids == ["IICNEEDLE202600222"]
    assert previous.pk


@pytest.mark.django_db
def test_cannot_select_other_equipment_as_source():
    owner = UserFactory()
    eq_a = _equipment(name="SEM A")
    eq_b = _equipment(name="SEM B")
    mine = _booking(owner, eq_a, virtual="IICMINEA202600001")
    other_eq = _booking(owner, eq_b, virtual="IICMINEB202600002")
    client = APIClient()
    client.force_authenticate(user=owner)
    res = client.post(
        f"/api/v1/bookings/{mine.pk}/analysis/data-selection/",
        {"source_booking_id": other_eq.pk, "stage": False},
        format="json",
    )
    assert res.status_code == 403
    mine.refresh_from_db()
    assert mine.analysis_data_selection in ({}, None)


@pytest.mark.django_db
def test_selection_persists_before_allocation():
    owner = UserFactory()
    equipment = _equipment()
    booking = _booking(owner, equipment, virtual="IICSEL202600333")
    client = APIClient()
    client.force_authenticate(user=owner)
    res = client.post(
        f"/api/v1/bookings/{booking.pk}/analysis/data-selection/",
        {"source_booking_id": booking.pk, "stage": False},
        format="json",
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["selection"]["virtual_booking_id"] == "IICSEL202600333"
    booking.refresh_from_db()
    assert booking.analysis_data_selection["source_booking_id"] == booking.pk
    assert booking.analysis_reservation_id is None

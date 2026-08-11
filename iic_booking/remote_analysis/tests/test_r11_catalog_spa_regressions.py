"""Regression: catalog SPA APIs must not 500 on production models."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from iic_booking.remote_analysis.catalog_admin_views import _equipment_pk, _license_type_choices
from iic_booking.remote_analysis.catalog_models import AnalysisSoftwareCatalog
from iic_booking.users.tests.factories import UserFactory


@pytest.mark.django_db
def test_license_type_choices_from_field():
    choices = _license_type_choices()
    assert choices
    values = {c[0] for c in choices}
    assert "concurrent" in values or "unlimited" in values


@pytest.mark.django_db
def test_catalog_list_returns_200_for_admin():
    UserFactory(
        user_type="admin",
        is_staff=True,
        is_superuser=True,
        admin_approved=True,
        email_verified=True,
    )
    AnalysisSoftwareCatalog.objects.create(name="Notepad", slug="notepad", is_active=True)
    admin = UserFactory._meta.model.objects.filter(user_type="admin").order_by("-id").first()
    client = APIClient()
    client.force_authenticate(user=admin)
    res = client.get("/api/v1/analysis/catalog/software/?active=1&archived=0")
    assert res.status_code == 200, res.data
    assert res.data["count"] >= 1
    assert "license_types" in res.data


@pytest.mark.django_db
def test_equipment_pk_helper_uses_pk():
    from iic_booking.equipment.models import Equipment

    eq = Equipment(name="T", code="T-PK-1", status="ACTIVE")
    # Unsaved: pk may be None — helper should prefer equipment_id when set after save
    eq.equipment_id = 42
    assert _equipment_pk(eq) == 42

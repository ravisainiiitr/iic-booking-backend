"""Tests for End Analysis API, software hard-filter, and equipment paths."""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from iic_booking.remote_analysis.constants import WorkstationStatus
from iic_booking.remote_analysis.models import AnalysisWorkstation, InstalledSoftware
from iic_booking.remote_analysis.services.availability import AvailabilityEngine


@pytest.mark.django_db
def test_booking_analysis_end_route_registered():
    from django.urls import reverse

    url = reverse("api:booking-analysis-end", kwargs={"booking_id": 1})
    assert "/analysis/end/" in url
    assert "/api/" in url

@pytest.mark.django_db
def test_required_software_hard_filter_rejects_incomplete_pc(db):
    ws = AnalysisWorkstation.objects.create(
        hostname="PC-NO-SOFTWARE",
        display_name="PC-NO-SOFTWARE",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
        health_score=100,
        ip_address="10.0.0.9",
    )
    from django.utils import timezone
    from datetime import timedelta

    start = timezone.now()
    end = start + timedelta(hours=1)
    # Fake online token/heartbeat path: set last_heartbeat fresh
    ws.last_heartbeat = timezone.now()
    ws.save(update_fields=["last_heartbeat"])

    result = AvailabilityEngine().evaluate(
        ws,
        start,
        end,
        requested_capabilities={"required_software_names": ["CasaXPS", "Avantage"]},
    )
    assert result.available is False
    assert any("Missing required software" in r for r in result.reasons)


@pytest.mark.django_db
def test_required_software_hard_filter_accepts_full_coverage(db):
    ws = AnalysisWorkstation.objects.create(
        hostname="PC-FULL",
        display_name="PC-FULL",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
        health_score=100,
        ip_address="10.0.0.10",
    )
    from django.utils import timezone
    from datetime import timedelta

    ws.last_heartbeat = timezone.now()
    ws.save(update_fields=["last_heartbeat"])
    for name in ("CasaXPS", "Avantage"):
        InstalledSoftware.objects.create(
            workstation=ws,
            software_name=name,
            version="1.0",
            is_present=True,
        )
    # Need agent token for evaluate to pass token check
    from iic_booking.remote_analysis.models import AgentToken

    AgentToken.objects.create(
        workstation=ws,
        token_hash="x" * 64,
        is_active=True,
        expires_at=timezone.now() + timedelta(days=1),
    )

    start = timezone.now()
    end = start + timedelta(hours=1)
    result = AvailabilityEngine().evaluate(
        ws,
        start,
        end,
        requested_capabilities={"required_software_names": ["CasaXPS", "Avantage"]},
    )
    assert result.available is True, result.reasons


@pytest.mark.django_db
def test_equipment_analysis_path_fields_exist():
    from iic_booking.equipment.models import Equipment

    field_names = {f.name for f in Equipment._meta.fields}
    assert "analysis_default_session_minutes" in field_names
    assert "analysis_extension_minutes" in field_names
    assert "analysis_raw_data_directory" in field_names
    assert "analysis_results_directory" in field_names


@pytest.mark.django_db
def test_software_mapping_required_names(db):
    from iic_booking.equipment.models import Equipment
    from iic_booking.equipment.remote_analysis_integration.software import SoftwareMappingService
    from iic_booking.remote_analysis.catalog_models import AnalysisSoftwareCatalog, EquipmentAnalysisSoftware

    eq = Equipment.objects.create(name="XPS", code="XPS-TEST-SW-1", status="ACTIVE")
    for name in ("CasaXPS", "Avantage"):
        cat = AnalysisSoftwareCatalog.objects.create(name=name, slug=name.lower(), is_active=True)
        EquipmentAnalysisSoftware.objects.create(equipment=eq, catalog=cat, is_default=(name == "CasaXPS"))
    names = SoftwareMappingService().required_software_names(eq)
    assert names == ["CasaXPS", "Avantage"] or set(names) == {"CasaXPS", "Avantage"}


@pytest.mark.django_db
def test_resolve_selected_software_slug(db):
    """R6: equipment may map multiple apps; resolve by slug returns that catalog profile only."""
    from iic_booking.equipment.models import Equipment
    from iic_booking.equipment.remote_analysis_integration.software import SoftwareMappingService
    from iic_booking.remote_analysis.catalog_models import AnalysisSoftwareCatalog, EquipmentAnalysisSoftware

    eq = Equipment.objects.create(name="XPS", code="XPS-TEST-SW-2", status="ACTIVE")
    for name in ("CasaXPS", "Avantage"):
        cat = AnalysisSoftwareCatalog.objects.create(name=name, slug=name.lower(), is_active=True)
        EquipmentAnalysisSoftware.objects.create(equipment=eq, catalog=cat, is_default=(name == "CasaXPS"))
    row, req = SoftwareMappingService().resolve(eq, slug="avantage")
    assert row is not None
    assert row.catalog.slug == "avantage"
    assert req is not None
    assert req.software == "Avantage"

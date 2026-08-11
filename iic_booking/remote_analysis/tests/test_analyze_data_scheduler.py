"""Tests for Analyze Data catalog, allocation pool boost, and mapping helpers."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from iic_booking.remote_analysis.catalog_models import (
    AnalysisSoftwareCatalog,
    EquipmentAnalysisPool,
    EquipmentAnalysisSoftware,
)
from iic_booking.remote_analysis.constants import WorkstationStatus
from iic_booking.remote_analysis.models import AnalysisWorkstation
from iic_booking.remote_analysis.services.allocation import AllocationService
from iic_booking.remote_analysis.services.tokens import issue_agent_token
from iic_booking.equipment.remote_analysis_integration.software import SoftwareMappingService


@pytest.mark.django_db
def test_catalog_ensures_software_requirement():
    catalog = AnalysisSoftwareCatalog.objects.create(
        name="MATLAB",
        slug="matlab",
        version_constraint="R2023a",
        max_concurrent=2,
        is_active=True,
    )
    req = catalog.ensure_software_requirement()
    assert req.software == "MATLAB"
    assert req.minimum_version == "R2023a"
    assert catalog.software_requirement_id == req.id
    # idempotent
    req2 = catalog.ensure_software_requirement()
    assert req2.id == req.id


@pytest.mark.django_db
def test_software_mapping_resolve_default_for_equipment():
    from iic_booking.equipment.models import Equipment

    eq = Equipment.objects.create(
        code=f"RA-TEST-{timezone.now().timestamp():.0f}",
        name="RA Test Equipment",
        enable_remote_analysis=True,
    )
    catalog = AnalysisSoftwareCatalog.objects.create(name="OriginPro", slug="originpro-test", is_active=True)
    catalog.ensure_software_requirement()
    EquipmentAnalysisSoftware.objects.create(equipment=eq, catalog=catalog, is_default=True, sort_order=0)
    other = AnalysisSoftwareCatalog.objects.create(name="ImageJ", slug="imagej-test", is_active=True)
    other.ensure_software_requirement()
    EquipmentAnalysisSoftware.objects.create(equipment=eq, catalog=other, is_default=False, sort_order=1)

    row, req = SoftwareMappingService().resolve(eq)
    assert row is not None
    assert row.catalog.name == "OriginPro"
    assert req is not None
    options = SoftwareMappingService().serialize_options(eq)
    assert len(options) == 2
    assert options[0]["name"] in {"OriginPro", "ImageJ"}
    for row in options:
        assert "installed_count" in row
        assert "online_count" in row
        assert "available_count" in row
        assert "busy_count" in row
        assert "maintenance_count" in row
        assert "offline_count" in row


@pytest.mark.django_db
def test_equipment_pool_boosts_score():
    from iic_booking.equipment.models import Equipment

    eq = Equipment.objects.create(
        code=f"RA-POOL-{timezone.now().timestamp():.0f}",
        name="Pool Equip",
        enable_remote_analysis=True,
    )
    preferred = AnalysisWorkstation.objects.create(
        agent_id="pool-pref-1",
        hostname="PREF-PC",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
        health_score=70,
        last_heartbeat=timezone.now(),
    )
    other = AnalysisWorkstation.objects.create(
        agent_id="pool-other-1",
        hostname="OTHER-PC",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
        health_score=95,
        last_heartbeat=timezone.now(),
    )
    issue_agent_token(preferred)
    issue_agent_token(other)
    EquipmentAnalysisPool.objects.create(equipment=eq, workstation=preferred, priority_boost=50)

    start = timezone.now()
    end = start + timedelta(hours=1)
    ranked = AllocationService().rank_candidates(start, end, equipment=eq)
    assert ranked
    # Preferred should win despite lower health because of equipment_priority boost
    assert ranked[0].workstation.id == preferred.id
    assert "equipment_priority" in ranked[0].breakdown

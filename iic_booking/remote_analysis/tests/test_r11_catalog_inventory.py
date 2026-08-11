"""R11 inventory → catalog auto-promotion and allocation_enabled gating."""

from __future__ import annotations

import pytest
from django.utils import timezone

from iic_booking.remote_analysis.catalog_models import AnalysisSoftwareCatalog
from iic_booking.remote_analysis.constants import WorkstationStatus
from iic_booking.remote_analysis.models import AnalysisWorkstation, InstalledSoftware
from iic_booking.remote_analysis.services.inventory import InventoryService, ensure_catalog_for_install


@pytest.mark.django_db
def test_ensure_catalog_dedupes_by_slug():
    a = ensure_catalog_for_install(name="OriginPro", publisher="OriginLab", version="2024")
    b = ensure_catalog_for_install(name="OriginPro", publisher="OriginLab", version="2025")
    assert a is not None and b is not None
    assert a.id == b.id
    assert AnalysisSoftwareCatalog.objects.filter(name__iexact="OriginPro").count() == 1


@pytest.mark.django_db
def test_inventory_sync_promotes_catalog_and_links():
    ws = AnalysisWorkstation.objects.create(
        agent_id="raa-r11-1",
        hostname="RAVI-R11",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
        last_heartbeat=timezone.now(),
    )
    result = InventoryService().synchronize(
        ws,
        {
            "software": [
                {"displayName": "Notepad", "version": "10.0", "publisher": "Microsoft"},
                {"displayName": "Notepad", "version": "10.0", "publisher": "Microsoft"},
            ]
        },
    )
    assert result["accepted"] is True
    assert result["added"] >= 1
    assert AnalysisSoftwareCatalog.objects.filter(name__iexact="Notepad").count() == 1
    row = InstalledSoftware.objects.filter(workstation=ws, software_name="Notepad", is_present=True).first()
    assert row is not None
    assert row.catalog_id is not None
    assert row.allocation_enabled is True


@pytest.mark.django_db
def test_disabled_install_excluded_from_software_match():
    from iic_booking.remote_analysis.scheduler_models import SoftwareRequirement
    from iic_booking.remote_analysis.services.availability import AvailabilityEngine

    ws = AnalysisWorkstation.objects.create(
        agent_id="raa-r11-2",
        hostname="RAA-OFF",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
        last_heartbeat=timezone.now(),
        last_inventory_update=timezone.now(),
    )
    InstalledSoftware.objects.create(
        workstation=ws,
        software_name="HighScore",
        version="1",
        is_present=True,
        allocation_enabled=False,
    )
    req = SoftwareRequirement.objects.create(name="HS", software="HighScore", required=True)
    ok, reasons = AvailabilityEngine().software_matches(ws, req)
    assert ok is False
    assert any("HighScore" in r for r in reasons)

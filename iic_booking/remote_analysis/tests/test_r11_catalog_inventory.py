"""R11 inventory → catalog auto-promotion and allocation_enabled gating."""

from __future__ import annotations

import pytest
from django.utils import timezone

from iic_booking.remote_analysis.catalog_models import AnalysisSoftwareCatalog
from iic_booking.remote_analysis.constants import WorkstationStatus
from iic_booking.remote_analysis.models import AnalysisWorkstation, InstalledSoftware
from iic_booking.remote_analysis.services.catalog_sync import backfill_catalog_from_installed
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
def test_inventory_sync_promotes_all_titles_and_truncates_paths():
    ws = AnalysisWorkstation.objects.create(
        agent_id="raa-r11-multi",
        hostname="RAVI-MULTI",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
        last_heartbeat=timezone.now(),
    )
    long_path = "C:\\" + ("VeryLong\\" * 80) + "app.exe"
    result = InventoryService().synchronize(
        ws,
        {
            "software": [
                {"displayName": "OriginPro", "version": "2024", "publisher": "OriginLab"},
                {"displayName": "ImageJ", "version": "1.54", "publisher": "NIH", "installPath": long_path},
                {"displayName": "CasaXPS", "version": "2.3", "publisher": "Casa"},
            ]
        },
    )
    assert result["accepted"] is True
    assert result["added"] == 3
    assert result["catalog_linked"] >= 3
    names = set(
        InstalledSoftware.objects.filter(workstation=ws, is_present=True).values_list(
            "software_name", flat=True
        )
    )
    assert names == {"OriginPro", "ImageJ", "CasaXPS"}
    assert AnalysisSoftwareCatalog.objects.filter(is_active=True, is_archived=False).count() >= 3
    img = InstalledSoftware.objects.get(workstation=ws, software_name="ImageJ")
    assert len(img.install_path) <= 1024


@pytest.mark.django_db
def test_backfill_catalog_from_orphan_installs():
    ws = AnalysisWorkstation.objects.create(
        agent_id="raa-r11-bf",
        hostname="RAVI-BF",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
    )
    InstalledSoftware.objects.create(
        workstation=ws,
        software_name="Avantage",
        publisher="Thermo",
        version="5",
        is_present=True,
        catalog=None,
    )
    InstalledSoftware.objects.create(
        workstation=ws,
        software_name="Avantage",
        publisher="Thermo",
        version="6",
        is_present=True,
        catalog=None,
    )
    result = backfill_catalog_from_installed()
    assert result["catalog_links_updated"] >= 2
    assert AnalysisSoftwareCatalog.objects.filter(name__iexact="Avantage").count() == 1
    assert InstalledSoftware.objects.filter(workstation=ws, catalog__isnull=False).count() == 2


@pytest.mark.django_db
def test_installer_link_creates_catalog_for_unknown_slugs():
    from iic_booking.equipment.models import Equipment
    from iic_booking.remote_analysis.installer.services import link_workstation_to_equipment

    ws = AnalysisWorkstation.objects.create(
        agent_id="raa-r11-link",
        hostname="RAVI-LINK",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
    )
    eq = Equipment.objects.create(
        name="FE-SEM Test",
        code="FESEM-R11",
        status="ACTIVE",
        enable_remote_analysis=True,
    )
    result = link_workstation_to_equipment(
        workstation=ws,
        equipment=eq,
        software_slugs=["originpro", "imagej"],
        software_items=[
            {"displayName": "OriginPro", "slug": "originpro", "publisher": "OriginLab"},
            {"displayName": "ImageJ", "slug": "imagej", "publisher": "NIH"},
        ],
        map_selected_to_equipment=True,
    )
    assert result["catalog_created"] >= 2
    assert AnalysisSoftwareCatalog.objects.filter(slug="originpro").exists()
    assert AnalysisSoftwareCatalog.objects.filter(slug="imagej").exists()
    assert InstalledSoftware.objects.filter(workstation=ws, is_present=True).count() >= 2


@pytest.mark.django_db
def test_installer_seed_inventory_without_equipment():
    from iic_booking.remote_analysis.installer.services import seed_workstation_software_from_selection

    ws = AnalysisWorkstation.objects.create(
        agent_id="raa-r11-seed",
        hostname="DESKTOP-CSMH6BU",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
    )
    result = seed_workstation_software_from_selection(
        workstation=ws,
        software_slugs=["originpro", "imagej"],
        software_items=[
            {"displayName": "OriginPro", "slug": "originpro", "publisher": "OriginLab", "version": "2024"},
            {"displayName": "ImageJ", "slug": "imagej", "publisher": "NIH"},
        ],
    )
    assert result["inventory_seeded"] >= 2
    assert AnalysisSoftwareCatalog.objects.filter(slug="originpro").exists()
    assert InstalledSoftware.objects.filter(workstation=ws, is_present=True).count() >= 2
    ws.refresh_from_db()
    assert ws.last_inventory_update is not None


@pytest.mark.django_db
def test_installer_seed_inventory_api():
    from rest_framework.test import APIRequestFactory

    from iic_booking.remote_analysis.installer.views import seed_inventory

    ws = AnalysisWorkstation.objects.create(
        agent_id="raa-r11-seed-api",
        hostname="DESKTOP-SEED-API",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
    )
    factory = APIRequestFactory()
    req = factory.post(
        "/api/v1/analysis/installer/seed-inventory/",
        {
            "workstationId": str(ws.id),
            "agentId": ws.agent_id,
            "softwareSlugs": ["casaxps"],
            "softwareItems": [{"displayName": "CasaXPS", "slug": "casaxps", "publisher": "Casa"}],
        },
        format="json",
        HTTP_X_ENROLLMENT_KEY="",
    )
    # Dev mode: empty enrollment key env allows unauthenticated seed when key unset
    resp = seed_inventory(req)
    assert resp.status_code == 200
    assert resp.data["inventory_seeded"] >= 1
    assert InstalledSoftware.objects.filter(workstation=ws, software_name__iexact="CasaXPS").exists()


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

"""Equipment software mappings whose catalog entry is disabled/archived ("orphaned" in the mapping UI)."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from iic_booking.equipment.models import Equipment
from iic_booking.equipment.remote_analysis_integration.software import SoftwareMappingService
from iic_booking.remote_analysis.catalog_models import AnalysisSoftwareCatalog, EquipmentAnalysisSoftware
from iic_booking.remote_analysis.constants import WorkstationStatus
from iic_booking.remote_analysis.models import AnalysisWorkstation, InstalledSoftware
from iic_booking.remote_analysis.services.catalog_sync import (
    archive_infrastructure_catalog_entries,
    archive_unmanaged_auto_catalog_entries,
)
from iic_booking.users.tests.factories import UserFactory

URL = "/api/v1/analysis/catalog/equipment-software/matrix/"


@pytest.fixture
def admin_client(db):
    user = UserFactory(
        user_type="admin", is_staff=True, is_superuser=True, admin_approved=True, email_verified=True
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def setup(db):
    eq = Equipment.objects.create(name="DSA Test Equipment", code="DSATEST-T", status="ACTIVE", enable_remote_analysis=True)
    excel = AnalysisSoftwareCatalog.objects.create(name="Excel", slug="excel", is_active=True)
    multipak = AnalysisSoftwareCatalog.objects.create(
        name="MultiPak 9.6.0",
        slug="multipak-9-6-0",
        category="analysis",
        description="Auto-discovered from RAA inventory (version=9.6.0).",
        is_active=False,
        is_archived=True,
    )
    EquipmentAnalysisSoftware.objects.create(equipment=eq, catalog=excel, is_default=False)
    EquipmentAnalysisSoftware.objects.create(equipment=eq, catalog=multipak, is_default=True)
    return eq, excel, multipak


def _row(data, eq):
    return next(r for r in data["equipment"] if r["id"] == eq.pk)


@pytest.mark.django_db
def test_matrix_get_exposes_archived_mapping_with_name(admin_client, setup):
    eq, excel, multipak = setup
    res = admin_client.get(URL)
    assert res.status_code == 200, res.data
    assert str(multipak.id) not in {c["id"] for c in res.data["catalogs"]}
    row = _row(res.data, eq)
    assert set(row["catalog_ids"]) == {str(excel.id), str(multipak.id)}
    assert row["unavailable_catalogs"] == [{"id": str(multipak.id), "name": "MultiPak 9.6.0", "state": "archived"}]


@pytest.mark.django_db
def test_matrix_put_removes_archived_mapping_and_moves_default(admin_client, setup):
    eq, excel, multipak = setup
    res = admin_client.put(
        URL, {"equipment_id": eq.pk, "catalog_ids": [str(excel.id)], "default_catalog_id": str(excel.id)}, format="json"
    )
    assert res.status_code == 200, res.data
    rows = list(EquipmentAnalysisSoftware.objects.filter(equipment=eq))
    assert [(str(r.catalog_id), r.is_default) for r in rows] == [(str(excel.id), True)]
    assert _row(admin_client.get(URL).data, eq)["unavailable_catalogs"] == []


@pytest.mark.django_db
def test_matrix_put_tolerates_unknown_and_malformed_ids(admin_client, setup):
    eq, excel, multipak = setup
    missing = "00000000-0000-4000-8000-000000000000"
    res = admin_client.put(
        URL,
        {"equipment_id": eq.pk, "catalog_ids": [str(excel.id), missing, "not-a-uuid"], "default_catalog_id": "not-a-uuid"},
        format="json",
    )
    assert res.status_code == 200, res.data
    assert set(res.data["ignored_catalog_ids"]) == {missing, "not-a-uuid"}
    rows = {str(r.catalog_id): r.is_default for r in EquipmentAnalysisSoftware.objects.filter(equipment=eq)}
    # Excel kept and becomes the default because the archived default is not usable.
    assert rows == {str(excel.id): True}


@pytest.mark.django_db
def test_matrix_put_keeping_archived_id_never_makes_it_default(admin_client, setup):
    eq, excel, multipak = setup
    res = admin_client.put(
        URL,
        {"equipment_id": eq.pk, "catalog_ids": [str(multipak.id), str(excel.id)], "default_catalog_id": str(multipak.id)},
        format="json",
    )
    assert res.status_code == 200, res.data
    rows = {str(r.catalog_id): r.is_default for r in EquipmentAnalysisSoftware.objects.filter(equipment=eq)}
    assert rows == {str(excel.id): True, str(multipak.id): False}


@pytest.mark.django_db
def test_allocation_ignores_archived_mapping(setup):
    eq, excel, multipak = setup
    svc = SoftwareMappingService()
    assert svc.required_software_names(eq) == ["Excel"]
    row, _req = svc.resolve(eq)
    assert row is not None and row.catalog_id == excel.id
    row, _req = svc.resolve(eq, catalog_id=str(multipak.id))
    assert row is None


@pytest.mark.django_db
def test_sync_keeps_mapped_auto_entry_when_install_is_no_longer_analysis():
    eq = Equipment.objects.create(name="XPS", code="XPS-ORPH", status="ACTIVE", enable_remote_analysis=True)
    ws = AnalysisWorkstation.objects.create(
        agent_id="raa-orphan-1", hostname="RAA-ORPH", status=WorkstationStatus.AVAILABLE, enabled=True
    )
    mapped = AnalysisSoftwareCatalog.objects.create(
        name="MultiPak 9.6.0",
        slug="multipak-9-6-0",
        category="analysis",
        description="Auto-discovered from RAA inventory (version=9.6.0).",
        is_active=True,
    )
    unmapped = AnalysisSoftwareCatalog.objects.create(
        name="Old Tool",
        slug="old-tool",
        description="Auto-discovered from RAA inventory (version=1).",
        is_active=True,
    )
    EquipmentAnalysisSoftware.objects.create(equipment=eq, catalog=mapped, is_default=True)
    InstalledSoftware.objects.create(
        workstation=ws, software_name="MultiPak 9.6.0", category="general", is_present=True, catalog=mapped
    )
    result = archive_unmanaged_auto_catalog_entries()
    mapped.refresh_from_db()
    unmapped.refresh_from_db()
    assert mapped.is_active is True and mapped.is_archived is False
    assert unmapped.is_archived is True
    assert result["unmanaged_auto_kept_mapped"] == 1
    assert EquipmentAnalysisSoftware.objects.filter(equipment=eq, catalog=mapped).exists()


@pytest.mark.django_db
def test_sync_archiving_noise_drops_its_mappings():
    eq = Equipment.objects.create(name="SEM", code="SEM-ORPH", status="ACTIVE", enable_remote_analysis=True)
    notepad = AnalysisSoftwareCatalog.objects.create(
        name="Notepad", slug="notepad", description="Auto-discovered from RAA inventory (version=10).", is_active=True
    )
    dotnet = AnalysisSoftwareCatalog.objects.create(
        name="Microsoft .NET Runtime 8", slug="dotnet-8", is_active=True
    )
    EquipmentAnalysisSoftware.objects.create(equipment=eq, catalog=notepad)
    EquipmentAnalysisSoftware.objects.create(equipment=eq, catalog=dotnet)
    infra = archive_infrastructure_catalog_entries()
    unmanaged = archive_unmanaged_auto_catalog_entries()
    assert infra["infrastructure_mappings_removed"] + unmanaged["unmanaged_noise_mappings_removed"] == 2
    assert not EquipmentAnalysisSoftware.objects.filter(equipment=eq).exists()
    # Rows are archived, not deleted, so their ids stay stable if the software is re-promoted.
    assert AnalysisSoftwareCatalog.objects.filter(pk__in=[notepad.pk, dotnet.pk], is_archived=True).count() == 2

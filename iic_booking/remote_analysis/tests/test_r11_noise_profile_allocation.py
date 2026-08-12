"""Regression: legacy Notepad analysis_profile must not block allocation."""

from __future__ import annotations

import pytest

from iic_booking.equipment.remote_analysis_integration.software import SoftwareMappingService
from iic_booking.remote_analysis.catalog_models import AnalysisSoftwareCatalog, EquipmentAnalysisSoftware
from iic_booking.remote_analysis.services.inventory import is_consumer_desktop_noise


@pytest.mark.django_db
def test_required_software_ignores_notepad_legacy_profile(db):
    from iic_booking.equipment.models import Equipment

    assert is_consumer_desktop_noise(name="Notepad")
    eq = Equipment.objects.create(
        name="PXRD Noise Profile",
        code="PXRD-NOISE-1",
        status="ACTIVE",
        analysis_profile="Notepad",
    )
    assert SoftwareMappingService().required_software_names(eq) == []
    row, req = SoftwareMappingService().resolve(eq)
    assert row is None and req is None


@pytest.mark.django_db
def test_required_software_prefers_active_catalog_over_noise_profile(db):
    from iic_booking.equipment.models import Equipment

    eq = Equipment.objects.create(
        name="PXRD Catalog Mapped",
        code="PXRD-MAP-1",
        status="ACTIVE",
        analysis_profile="Notepad",
    )
    cat = AnalysisSoftwareCatalog.objects.create(
        name="Altium Designer 26",
        slug="altium-designer-26-test",
        category="analysis",
        is_active=True,
    )
    EquipmentAnalysisSoftware.objects.create(equipment=eq, catalog=cat, is_default=True)
    assert SoftwareMappingService().required_software_names(eq) == ["Altium Designer 26"]
    row, req = SoftwareMappingService().resolve(eq)
    assert row is not None
    assert req is not None
    assert "Altium" in (req.software or "")

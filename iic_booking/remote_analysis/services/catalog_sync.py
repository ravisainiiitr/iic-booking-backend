"""R11 helpers: promote InstalledSoftware → AnalysisSoftwareCatalog."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def backfill_catalog_from_installed(*, limit: int = 50_000) -> dict[str, Any]:
    """
    Create/link AnalysisSoftwareCatalog rows for every present InstalledSoftware
    missing a catalog FK. Dedupes by ensure_catalog_for_install (slug / name).
    """
    from iic_booking.remote_analysis.catalog_models import AnalysisSoftwareCatalog
    from iic_booking.remote_analysis.models import InstalledSoftware
    from iic_booking.remote_analysis.services.inventory import (
        ensure_catalog_for_install,
        is_infrastructure_inventory_noise,
    )

    qs = (
        InstalledSoftware.objects.filter(is_present=True)
        .exclude(software_name="")
        .order_by("id")[: max(1, int(limit))]
    )

    linked = already = skipped = 0
    names_promoted: set[str] = set()
    scanned = 0
    for row in qs:
        scanned += 1
        name = (row.software_name or "").strip()
        if not name:
            skipped += 1
            continue
        if is_infrastructure_inventory_noise(name=name, publisher=row.publisher or ""):
            skipped += 1
            continue
        catalog = ensure_catalog_for_install(
            name=name,
            publisher=row.publisher or "",
            version=row.version or "",
        )
        if catalog is None:
            skipped += 1
            continue
        names_promoted.add(name.lower())
        if getattr(row, "catalog_id", None) != catalog.id:
            row.catalog = catalog
            row.save(update_fields=["catalog", "last_updated"])
            linked += 1
        else:
            already += 1

    distinct_present = (
        InstalledSoftware.objects.filter(is_present=True)
        .exclude(software_name="")
        .values("software_name")
        .distinct()
        .count()
    )
    return {
        "install_rows_scanned": scanned,
        "catalog_links_updated": linked,
        "already_linked": already,
        "skipped": skipped,
        "distinct_names_promoted": len(names_promoted),
        "distinct_present_install_names": distinct_present,
        "active_catalog_count": AnalysisSoftwareCatalog.objects.filter(
            is_active=True, is_archived=False
        ).count(),
        "total_catalog_count": AnalysisSoftwareCatalog.objects.count(),
    }


def inventory_discovery_summary() -> dict[str, Any]:
    """Fleet inventory vs catalog coverage for SPA empty/partial UX."""
    from iic_booking.remote_analysis.catalog_models import AnalysisSoftwareCatalog
    from iic_booking.remote_analysis.models import AnalysisWorkstation, InstalledSoftware

    present = InstalledSoftware.objects.filter(is_present=True)
    distinct_names = (
        present.exclude(software_name="")
        .values_list("software_name", flat=True)
        .distinct()
        .count()
    )
    unlinked = present.filter(catalog__isnull=True).count()
    active_catalog = AnalysisSoftwareCatalog.objects.filter(
        is_active=True, is_archived=False
    ).count()
    ws_with_inventory = (
        AnalysisWorkstation.objects.filter(installed_software__is_present=True)
        .distinct()
        .count()
    )
    return {
        "present_install_rows": present.count(),
        "distinct_software_names": distinct_names,
        "unlinked_install_rows": unlinked,
        "active_catalog_count": active_catalog,
        "workstations_with_inventory": ws_with_inventory,
        "needs_backfill": unlinked > 0 or distinct_names > active_catalog,
        "catalog_empty_but_inventory_exists": active_catalog == 0 and distinct_names > 0,
    }

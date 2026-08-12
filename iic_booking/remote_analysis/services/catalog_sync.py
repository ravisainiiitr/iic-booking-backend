"""R11 helpers: promote InstalledSoftware → AnalysisSoftwareCatalog."""

from __future__ import annotations

import logging
from typing import Any

from django.db.models import Q

logger = logging.getLogger(__name__)

_ANALYSIS_CATEGORIES = ("analysis", "scientific", "catalog")


def _is_analysis_category(category: str | None) -> bool:
    return (category or "").strip().lower() in _ANALYSIS_CATEGORIES


def backfill_catalog_from_installed(*, limit: int = 50_000) -> dict[str, Any]:
    """
    Promote only analysis/selected InstalledSoftware into the Software Catalog.

    "Sync from RAA" must NOT dump every Windows registry title into the catalog.
    Eligible rows:
      - already linked AND analysis-category (re-activate if needed), or
      - category in analysis / scientific / catalog
    Desktop / infrastructure noise (Notepad, Chrome, .NET, …) is always skipped.
    """
    from iic_booking.remote_analysis.catalog_models import AnalysisSoftwareCatalog
    from iic_booking.remote_analysis.models import InstalledSoftware
    from iic_booking.remote_analysis.services.inventory import (
        ensure_catalog_for_install,
        is_catalog_auto_noise,
    )

    qs = (
        InstalledSoftware.objects.filter(is_present=True)
        .exclude(software_name="")
        .order_by("id")[: max(1, int(limit))]
    )

    linked = already = skipped = unlinked = 0
    names_promoted: set[str] = set()
    scanned = 0
    for row in qs:
        scanned += 1
        name = (row.software_name or "").strip()
        if not name:
            skipped += 1
            continue
        if is_catalog_auto_noise(name=name, publisher=row.publisher or ""):
            if getattr(row, "catalog_id", None):
                row.catalog = None
                row.allocation_enabled = False
                row.save(update_fields=["catalog", "allocation_enabled", "last_updated"])
                unlinked += 1
            skipped += 1
            continue

        # Keep existing catalog links only for analysis categories.
        if getattr(row, "catalog_id", None):
            if not _is_analysis_category(row.category):
                row.catalog = None
                row.allocation_enabled = False
                row.save(update_fields=["catalog", "allocation_enabled", "last_updated"])
                unlinked += 1
                skipped += 1
                continue
            catalog = row.catalog
            if catalog is not None and (catalog.is_archived or not catalog.is_active):
                catalog.is_archived = False
                catalog.is_active = True
                catalog.save(update_fields=["is_archived", "is_active", "updated_at"])
            names_promoted.add(name.lower())
            already += 1
            continue

        # New promotions: analysis / selected software only.
        if not _is_analysis_category(row.category):
            skipped += 1
            continue

        catalog = ensure_catalog_for_install(
            name=name,
            publisher=row.publisher or "",
            version=row.version or "",
            category=(row.category or "analysis"),
        )
        if catalog is None:
            skipped += 1
            continue
        names_promoted.add(name.lower())
        row.catalog = catalog
        if not row.allocation_enabled:
            row.allocation_enabled = True
            row.save(update_fields=["catalog", "allocation_enabled", "last_updated"])
        else:
            row.save(update_fields=["catalog", "last_updated"])
        linked += 1

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
        "unlinked_noise": unlinked,
        "skipped": skipped,
        "distinct_names_promoted": len(names_promoted),
        "distinct_present_install_names": distinct_present,
        "active_catalog_count": AnalysisSoftwareCatalog.objects.filter(
            is_active=True, is_archived=False
        ).count(),
        "total_catalog_count": AnalysisSoftwareCatalog.objects.count(),
    }


def archive_infrastructure_catalog_entries() -> dict[str, int]:
    """Archive auto-created catalog rows for Windows/.NET infrastructure noise."""
    from iic_booking.remote_analysis.catalog_models import AnalysisSoftwareCatalog
    from iic_booking.remote_analysis.services.inventory import is_catalog_auto_noise

    archived = 0
    for cat in AnalysisSoftwareCatalog.objects.filter(is_active=True, is_archived=False):
        if is_catalog_auto_noise(name=cat.name, publisher=cat.vendor or ""):
            cat.is_active = False
            cat.is_archived = True
            cat.save(update_fields=["is_active", "is_archived", "updated_at"])
            archived += 1
    return {"infrastructure_archived": archived}


def archive_unmanaged_auto_catalog_entries() -> dict[str, int]:
    """
    Archive catalog rows that are not analysis/selected software.

    Keeps:
      - admin-created entries that are not desktop/infrastructure noise
      - auto-discovered entries linked to a present analysis-category install
        (and not desktop noise like Notepad)
    """
    from iic_booking.remote_analysis.catalog_models import AnalysisSoftwareCatalog
    from iic_booking.remote_analysis.models import InstalledSoftware
    from iic_booking.remote_analysis.services.inventory import is_catalog_auto_noise

    analysis_q = (
        Q(category__iexact="analysis")
        | Q(category__iexact="scientific")
        | Q(category__iexact="catalog")
    )
    archived = 0
    for cat in AnalysisSoftwareCatalog.objects.filter(is_active=True, is_archived=False):
        if is_catalog_auto_noise(name=cat.name, publisher=cat.vendor or ""):
            cat.is_active = False
            cat.is_archived = True
            cat.save(update_fields=["is_active", "is_archived", "updated_at"])
            archived += 1
            continue

        desc = (cat.description or "").strip()
        auto = desc.startswith("Auto-discovered from RAA")
        keep = InstalledSoftware.objects.filter(is_present=True).filter(
            analysis_q
            & (Q(catalog_id=cat.id) | Q(software_name__iexact=cat.name))
        ).exists()

        if not auto:
            # Admin-created entry — keep unless it was somehow denylisted above.
            continue
        if keep:
            continue

        cat.is_active = False
        cat.is_archived = True
        cat.save(update_fields=["is_active", "is_archived", "updated_at"])
        archived += 1

    return {"unmanaged_auto_archived": archived}


def inventory_discovery_summary() -> dict[str, Any]:
    """Fleet inventory vs catalog coverage for SPA empty/partial UX."""
    from iic_booking.remote_analysis.catalog_models import AnalysisSoftwareCatalog
    from iic_booking.remote_analysis.models import AnalysisWorkstation, InstalledSoftware

    present = InstalledSoftware.objects.filter(is_present=True)
    analysis_present = present.filter(
        Q(category__iexact="analysis")
        | Q(category__iexact="scientific")
        | Q(category__iexact="catalog")
    )
    distinct_names = (
        present.exclude(software_name="")
        .values_list("software_name", flat=True)
        .distinct()
        .count()
    )
    distinct_analysis = (
        analysis_present.exclude(software_name="")
        .values_list("software_name", flat=True)
        .distinct()
        .count()
    )
    unlinked = analysis_present.filter(catalog__isnull=True).count()
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
        "distinct_analysis_software_names": distinct_analysis,
        "unlinked_install_rows": unlinked,
        "active_catalog_count": active_catalog,
        "workstations_with_inventory": ws_with_inventory,
        "needs_backfill": unlinked > 0,
        "catalog_empty_but_inventory_exists": active_catalog == 0 and distinct_analysis > 0,
    }

"""Installer services: enrollment gate, RDP secret write, pool linking."""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any

from django.db import transaction
from django.utils.text import slugify


def enrollment_key_expected() -> str:
    return (os.environ.get("RA_AGENT_ENROLLMENT_KEY") or "").strip()


def verify_enrollment_key(request) -> tuple[bool, str]:
    """Return (ok, error_message). When env key unset, allow (dev)."""
    expected = enrollment_key_expected()
    if not expected:
        return True, ""
    provided = (
        request.META.get("HTTP_X_ENROLLMENT_KEY")
        or request.headers.get("X-Enrollment-Key")
        or ""
    ).strip()
    if not provided and hasattr(request, "data"):
        data = request.data if isinstance(request.data, dict) else {}
        provided = str(data.get("enrollmentKey") or data.get("enrollment_key") or "").strip()
    if not provided or not hmac.compare_digest(provided, expected):
        return False, "Invalid or missing enrollment key."
    return True, ""


def sha256_filefield(file_field) -> str:
    if not file_field:
        return ""
    h = hashlib.sha256()
    file_field.open("rb")
    try:
        for chunk in file_field.chunks():
            h.update(chunk)
    finally:
        file_field.close()
    return h.hexdigest()


def _normalize_software_selection(
    software_slugs: list | None,
    software_items: list | None,
) -> list[dict[str, str]]:
    """Flatten installer slug list and/or rich software items into name/publisher/version/slug."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(*, name: str, publisher: str = "", version: str = "", slug: str = "") -> None:
        clean = (name or "").strip()
        if not clean:
            return
        key = clean.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(
            {
                "name": clean[:255],
                "publisher": (publisher or "").strip()[:255],
                "version": (version or "").strip()[:128],
                "slug": (slug or slugify(clean) or "software")[:200],
            }
        )

    for raw in software_items or []:
        if isinstance(raw, str):
            add(name=raw.replace("-", " "), slug=slugify(raw))
            continue
        if not isinstance(raw, dict):
            continue
        name = str(
            raw.get("displayName")
            or raw.get("software_name")
            or raw.get("name")
            or raw.get("Name")
            or ""
        ).strip()
        if not name and raw.get("slug"):
            name = str(raw.get("slug")).replace("-", " ").strip()
        add(
            name=name,
            publisher=str(raw.get("publisher") or raw.get("vendor") or ""),
            version=str(raw.get("version") or ""),
            slug=str(raw.get("slug") or ""),
        )

    for slug in software_slugs or []:
        if isinstance(slug, dict):
            name = str(slug.get("displayName") or slug.get("name") or slug.get("slug") or "").strip()
            add(
                name=name or str(slug.get("slug") or "").replace("-", " "),
                publisher=str(slug.get("publisher") or ""),
                version=str(slug.get("version") or ""),
                slug=str(slug.get("slug") or ""),
            )
            continue
        s = str(slug or "").strip()
        if not s:
            continue
        # Prefer matching an existing catalog by slug; otherwise humanize slug as name.
        add(name=s.replace("-", " ").strip(), slug=slugify(s) or s)

    return out


@transaction.atomic
def link_workstation_to_equipment(
    *,
    workstation,
    equipment,
    rdp_username: str = "",
    rdp_password: str = "",
    rdp_domain: str = "",
    rdp_port: int = 3389,
    software_slugs: list[str] | None = None,
    software_items: list | None = None,
    priority_boost: int = 10,
    map_selected_to_equipment: bool = True,
) -> dict[str, Any]:
    from iic_booking.remote_analysis.catalog_models import (
        AnalysisSoftwareCatalog,
        EquipmentAnalysisPool,
        EquipmentAnalysisSoftware,
    )
    from iic_booking.remote_analysis.guacamole.secrets import encrypt_password
    from iic_booking.remote_analysis.models import InstalledSoftware
    from iic_booking.remote_analysis.services.inventory import ensure_catalog_for_install
    from iic_booking.remote_analysis.session_models import WorkstationRdpSecret

    pool, pool_created = EquipmentAnalysisPool.objects.update_or_create(
        equipment=equipment,
        workstation=workstation,
        defaults={"priority_boost": priority_boost},
    )

    rdp_updated = False
    if rdp_username or rdp_password:
        existing = WorkstationRdpSecret.objects.filter(workstation=workstation).first()
        # Preserve existing ciphertext when installer re-links username without a new password.
        password_encrypted = (
            encrypt_password(rdp_password)
            if rdp_password
            else (existing.password_encrypted if existing else "")
        )
        WorkstationRdpSecret.objects.update_or_create(
            workstation=workstation,
            defaults={
                "username": rdp_username
                or getattr(workstation, "windows_username", "")
                or (existing.username if existing else "")
                or "",
                "password_encrypted": password_encrypted,
                "domain": rdp_domain if rdp_domain is not None else (existing.domain if existing else ""),
                "port": int(rdp_port or (existing.port if existing else 3389) or 3389),
                "security": (existing.security if existing and existing.security else "nla"),
            },
        )
        rdp_updated = True

    selection = _normalize_software_selection(software_slugs, software_items)
    linked_software: list[str] = []
    catalog_created = 0
    inventory_seeded = 0

    for item in selection:
        prior = (
            AnalysisSoftwareCatalog.objects.filter(slug=item["slug"]).first()
            or AnalysisSoftwareCatalog.objects.filter(name__iexact=item["name"]).first()
        )
        catalog = ensure_catalog_for_install(
            name=item["name"],
            publisher=item["publisher"],
            version=item["version"],
        )
        if catalog is None:
            continue
        if prior is None:
            catalog_created += 1

        linked_software.append(catalog.slug)

        if map_selected_to_equipment:
            EquipmentAnalysisSoftware.objects.get_or_create(
                equipment=equipment,
                catalog=catalog,
                defaults={"sort_order": 0},
            )

        # Seed workstation inventory so scheduler/catalog can use titles immediately.
        name = (catalog.name or item["name"] or "").strip()
        if name:
            existing_sw = InstalledSoftware.objects.filter(
                workstation=workstation,
                software_name__iexact=name,
                is_present=True,
            ).first()
            if existing_sw is None:
                InstalledSoftware.objects.create(
                    workstation=workstation,
                    software_name=name[:512],
                    publisher=(catalog.vendor or item["publisher"] or "")[:255],
                    version=(item["version"] or "")[:128],
                    category=(getattr(catalog, "category", None) or "catalog")[:64],
                    is_present=True,
                    allocation_enabled=True,
                    catalog=catalog,
                )
                inventory_seeded += 1
            else:
                changed = False
                if getattr(existing_sw, "catalog_id", None) != catalog.id:
                    existing_sw.catalog = catalog
                    changed = True
                if not existing_sw.publisher and (catalog.vendor or item["publisher"]):
                    existing_sw.publisher = (catalog.vendor or item["publisher"])[:255]
                    changed = True
                if changed:
                    existing_sw.save()

    return {
        "pool_id": str(pool.id),
        "pool_created": pool_created,
        "rdp_secret_updated": rdp_updated,
        "software_slugs": linked_software,
        "catalog_created": catalog_created,
        "inventory_seeded": inventory_seeded,
        "workstation_id": str(workstation.id),
        "equipment_id": equipment.pk,
    }

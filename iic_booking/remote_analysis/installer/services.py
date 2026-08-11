"""Installer services: enrollment gate, RDP secret write, pool linking."""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any

from django.db import transaction


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
    priority_boost: int = 10,
) -> dict[str, Any]:
    from iic_booking.remote_analysis.catalog_models import (
        AnalysisSoftwareCatalog,
        EquipmentAnalysisPool,
        EquipmentAnalysisSoftware,
    )
    from iic_booking.remote_analysis.guacamole.secrets import encrypt_password
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

    linked_software: list[str] = []
    for slug in software_slugs or []:
        slug = (slug or "").strip()
        if not slug:
            continue
        catalog = AnalysisSoftwareCatalog.objects.filter(slug=slug, is_active=True).first()
        if not catalog:
            continue
        EquipmentAnalysisSoftware.objects.get_or_create(
            equipment=equipment,
            catalog=catalog,
            defaults={"sort_order": 0},
        )
        linked_software.append(catalog.slug)
        # Seed workstation inventory so the scheduler can match required software
        # immediately after installer link (inventory POST may lag or be empty).
        from iic_booking.remote_analysis.models import InstalledSoftware

        name = (catalog.name or "").strip()
        if name:
            existing_sw = InstalledSoftware.objects.filter(
                workstation=workstation,
                software_name__iexact=name,
                is_present=True,
            ).first()
            if existing_sw is None:
                InstalledSoftware.objects.create(
                    workstation=workstation,
                    software_name=name,
                    publisher=(catalog.vendor or "")[:255],
                    version="",
                    category=(getattr(catalog, "category", None) or "catalog")[:64],
                    is_present=True,
                )

    return {
        "pool_id": str(pool.id),
        "pool_created": pool_created,
        "rdp_secret_updated": rdp_updated,
        "software_slugs": linked_software,
        "workstation_id": str(workstation.id),
        "equipment_id": equipment.pk,
    }

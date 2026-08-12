"""Inventory synchronization — hardware, software, licenses, capabilities."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.text import slugify

from iic_booking.remote_analysis.constants import AuditCategory, InventoryChangeType
from iic_booking.remote_analysis.models import (
    AnalysisWorkstation,
    InstalledSoftware,
    SoftwareInventoryHistory,
    SoftwareLicense,
    WorkstationCapability,
    WorkstationInventory,
)
from iic_booking.remote_analysis.services.audit import record_event
from iic_booking.remote_analysis.services.health import update_workstation_health

logger = logging.getLogger(__name__)


def _software_key(item: dict[str, Any]) -> str:
    name = str(item.get("displayName") or item.get("software_name") or item.get("Software") or "").strip().lower()
    version = str(item.get("version") or item.get("Version") or "").strip().lower()
    publisher = str(item.get("publisher") or item.get("Publisher") or "").strip().lower()
    return f"{name}|{version}|{publisher}"


def _clip(value: Any, max_len: int) -> str:
    """Truncate strings to model field limits so one oversized path cannot abort sync."""
    text = str(value or "").strip()
    if len(text) <= max_len:
        return text
    return text[:max_len]


def is_infrastructure_inventory_noise(*, name: str, publisher: str = "") -> bool:
    """
    Windows/registry titles that must not auto-enter the Analysis Software Catalog
    (runtimes, SDKs, hosting packs). Installer explicit selection may still seed these.
    """
    import re

    text = f"{name} {publisher}".strip().lower()
    if not text:
        return False
    patterns = (
        r"microsoft\s+\.net",
        r"microsoft\s+asp\.net",
        r"windows desktop runtime",
        r"windows server hosting",
        r"\.net sdk",
        r"\.net runtime",
        r"visual c\+\+",
        r"windows sdk",
        r"microsoft visual studio setup",
        r"vs_\d{4}",
    )
    return any(re.search(p, text) for p in patterns)


def resolve_catalog_for_inventory(
    *,
    name: str,
    publisher: str = "",
    version: str = "",
    category: str = "",
):
    """
    Agent inventory sync: link existing catalog rows; auto-create only for analysis/scientific
    titles. Never auto-create infrastructure noise (.NET runtimes, etc.).
    """
    from iic_booking.remote_analysis.catalog_models import AnalysisSoftwareCatalog

    clean = _clip(name, 255)
    if not clean:
        return None

    existing = (
        AnalysisSoftwareCatalog.objects.filter(name__iexact=clean).first()
        or AnalysisSoftwareCatalog.objects.filter(slug=slugify(clean)[:200]).first()
    )
    if existing:
        return existing

    if is_infrastructure_inventory_noise(name=clean, publisher=publisher):
        return None

    cat = (category or "").strip().lower()
    if cat not in {"analysis", "scientific", "catalog"}:
        return None

    return ensure_catalog_for_install(name=clean, publisher=publisher, version=version)


def _content_hash(item: dict[str, Any]) -> str:
    raw = "|".join(
        [
            str(item.get("displayName") or item.get("software_name") or ""),
            str(item.get("version") or ""),
            str(item.get("publisher") or ""),
            str(item.get("installPath") or item.get("install_path") or ""),
            str(item.get("executable") or ""),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def ensure_catalog_for_install(*, name: str, publisher: str = "", version: str = "", category: str = "analysis"):
    """R11: auto-populate global Software Catalog from RAA inventory (one row per slug)."""
    from iic_booking.remote_analysis.catalog_models import AnalysisSoftwareCatalog

    clean = _clip(name, 255)
    if not clean:
        return None
    base_slug = slugify(clean)[:200] or "software"
    existing = (
        AnalysisSoftwareCatalog.objects.filter(slug=base_slug).first()
        or AnalysisSoftwareCatalog.objects.filter(name__iexact=clean).first()
    )
    if existing:
        changed = False
        if publisher and not (existing.vendor or "").strip():
            existing.vendor = _clip(publisher, 255)
            changed = True
        if existing.is_archived or not existing.is_active:
            existing.is_archived = False
            existing.is_active = True
            changed = True
        if changed:
            existing.save(update_fields=["vendor", "is_archived", "is_active", "updated_at"])
        return existing
    # Unique slug races / collisions: append numeric suffix then retry.
    for attempt in range(8):
        slug = base_slug if attempt == 0 else f"{base_slug[:190]}-{attempt}"
        try:
            return AnalysisSoftwareCatalog.objects.create(
                name=clean,
                slug=slug,
                vendor=_clip(publisher, 255),
                category=_clip(category or "analysis", 128),
                is_active=True,
                is_archived=False,
                description=f"Auto-discovered from RAA inventory (version={_clip(version, 64) or 'unknown'}).",
            )
        except Exception:  # noqa: BLE001
            hit = (
                AnalysisSoftwareCatalog.objects.filter(slug=slug).first()
                or AnalysisSoftwareCatalog.objects.filter(name__iexact=clean).first()
            )
            if hit:
                return hit
            if attempt == 7:
                logger.exception("ensure_catalog_for_install failed for %s", clean)
    return AnalysisSoftwareCatalog.objects.filter(name__iexact=clean).first()


class InventoryService:
    @transaction.atomic
    def synchronize(self, workstation: AnalysisWorkstation, payload: dict[str, Any]) -> dict[str, Any]:
        software_list = payload.get("software") or payload.get("installedSoftware") or []
        if not isinstance(software_list, list):
            software_list = []
        # R11: honor delta sync payloads from the agent (top-level or nested under "delta").
        sync_mode = str(payload.get("syncMode") or payload.get("sync_mode") or "full").lower()
        is_delta = sync_mode == "delta"
        delta_blob = payload.get("delta") if isinstance(payload.get("delta"), dict) else {}
        removed_items: list[dict[str, Any]] = []
        if is_delta:
            merged: list[dict[str, Any]] = []
            for key in ("added", "updated", "software"):
                for source in (payload, delta_blob):
                    chunk = source.get(key) if isinstance(source, dict) else None
                    if isinstance(chunk, list):
                        merged.extend(x for x in chunk if isinstance(x, dict))
            if not software_list:
                software_list = merged
            else:
                software_list = list(software_list) + merged
            for source in (payload, delta_blob):
                chunk = source.get("removed") if isinstance(source, dict) else None
                if isinstance(chunk, list):
                    removed_items.extend(x for x in chunk if isinstance(x, dict))
        licenses = payload.get("licenses") or payload.get("softwareLicenses") or []
        hardware = payload.get("hardware") or payload.get("workstation") or {}
        now = timezone.now()

        inventory, _ = WorkstationInventory.objects.get_or_create(workstation=workstation)
        if hardware:
            inventory.hardware_json = hardware

        existing = {
            (s.software_name.lower(), s.version.lower(), s.publisher.lower()): s
            for s in InstalledSoftware.objects.filter(workstation=workstation, is_present=True)
        }
        seen_keys: set[tuple[str, str, str]] = set()
        added = removed = version_changed = catalog_linked = 0

        for item in software_list:
            if not isinstance(item, dict):
                continue
            name = _clip(item.get("displayName") or item.get("software_name") or "", 512)
            if not name:
                continue
            version = _clip(item.get("version") or "", 128)
            publisher = _clip(item.get("publisher") or "", 512)
            key = (name.lower(), version.lower(), publisher.lower())
            if key in seen_keys:
                continue
            seen_keys.add(key)
            install_date = item.get("installDate") or item.get("install_date")
            if isinstance(install_date, str):
                install_date = parse_datetime(install_date)

            content_hash = _clip(
                item.get("contentHash") or item.get("ContentHash") or _content_hash(item),
                128,
            )
            try:
                promote = bool(
                    item.get("promoteToCatalog")
                    or item.get("promote_to_catalog")
                    or item.get("installerSelection")
                    or item.get("installer_selection")
                )
                if promote:
                    catalog = ensure_catalog_for_install(
                        name=name,
                        publisher=publisher,
                        version=version,
                    )
                else:
                    catalog = resolve_catalog_for_inventory(
                        name=name,
                        publisher=publisher,
                        version=version,
                        category=category,
                    )
            except Exception:  # noqa: BLE001
                logger.exception("catalog promote failed for %s", name)
                catalog = None

            # Detect version change by name+publisher ignoring version
            prior_same_name = [
                s
                for (n, _v, p), s in existing.items()
                if n == name.lower() and p == publisher.lower() and _v != version.lower()
            ]

            executable = _clip(item.get("executable") or "", 1024)
            install_path = _clip(item.get("installPath") or item.get("install_path") or "", 1024)
            category = _clip(item.get("category") or "", 128)
            license_type = _clip(item.get("licenseType") or item.get("license_type") or "", 128)
            licensed = bool(item.get("isLicensed") or item.get("licensed") or False)

            try:
                with transaction.atomic():
                    if key in existing:
                        row = existing[key]
                        changed = False
                        for field, value in (
                            ("executable", executable),
                            ("install_path", install_path),
                            ("category", category),
                            ("licensed", licensed),
                            ("license_type", license_type),
                            ("content_hash", content_hash),
                        ):
                            if getattr(row, field) != value:
                                setattr(row, field, value)
                                changed = True
                        if install_date and row.install_date != install_date:
                            row.install_date = install_date
                            changed = True
                        if catalog is not None and getattr(row, "catalog_id", None) != catalog.id:
                            row.catalog = catalog
                            changed = True
                            catalog_linked += 1
                        if changed:
                            row.save()
                    else:
                        InstalledSoftware.objects.create(
                            workstation=workstation,
                            software_name=name,
                            publisher=publisher,
                            version=version,
                            executable=executable,
                            install_path=install_path,
                            install_date=install_date,
                            licensed=licensed,
                            license_type=license_type,
                            category=category,
                            content_hash=content_hash,
                            is_present=True,
                            allocation_enabled=catalog is not None,
                            catalog=catalog,
                        )
                        added += 1
                        if catalog:
                            catalog_linked += 1
                        SoftwareInventoryHistory.objects.create(
                            workstation=workstation,
                            software_name=name,
                            change_type=InventoryChangeType.ADDED,
                            new_version=version,
                            details=f"Publisher={publisher}",
                        )
                        if prior_same_name:
                            version_changed += 1
                            old = prior_same_name[0]
                            SoftwareInventoryHistory.objects.create(
                                workstation=workstation,
                                software_name=name,
                                change_type=InventoryChangeType.VERSION_CHANGED,
                                old_version=old.version,
                                new_version=version,
                            )
            except Exception:  # noqa: BLE001
                # One bad title must not abort the whole inventory transaction.
                logger.exception(
                    "InstalledSoftware upsert failed workstation=%s name=%s",
                    workstation.pk,
                    name,
                )
                continue

        # Explicit removals from delta payloads.
        for item in removed_items:
            name = str(item.get("displayName") or item.get("software_name") or "").strip()
            if not name:
                continue
            version = str(item.get("version") or "").strip()
            publisher = str(item.get("publisher") or "").strip()
            key = (name.lower(), version.lower(), publisher.lower())
            row = existing.get(key)
            if row is None:
                # Fall back to name(+publisher) match when version omitted.
                candidates = [
                    s
                    for (n, _v, p), s in existing.items()
                    if n == name.lower() and (not publisher or p == publisher.lower())
                ]
                row = candidates[0] if candidates else None
            if row is not None and row.is_present:
                row.is_present = False
                row.save(update_fields=["is_present", "last_updated"])
                removed += 1
                SoftwareInventoryHistory.objects.create(
                    workstation=workstation,
                    software_name=row.software_name,
                    change_type=InventoryChangeType.REMOVED,
                    old_version=row.version,
                )

        # Full sync only: mark missing titles absent. Delta must never wipe unrelated installs.
        if not is_delta:
            for key, row in existing.items():
                if key not in seen_keys:
                    # Do not wipe inventory when the agent posts an empty software list
                    # (heartbeat-only payloads / discovery failures).
                    if not software_list:
                        break
                    row.is_present = False
                    row.save(update_fields=["is_present", "last_updated"])
                    removed += 1
                    SoftwareInventoryHistory.objects.create(
                        workstation=workstation,
                        software_name=row.software_name,
                        change_type=InventoryChangeType.REMOVED,
                        old_version=row.version,
                    )

        for lic in licenses:
            if not isinstance(lic, dict):
                continue
            software = str(lic.get("software") or lic.get("Software") or "").strip()
            if not software:
                continue
            obj, _ = SoftwareLicense.objects.get_or_create(
                workstation=workstation,
                software=software,
            )
            obj.expiry = parse_datetime(str(lic["expiry"])) if lic.get("expiry") else obj.expiry
            obj.seats = lic.get("seats", obj.seats)
            obj.license_server = str(lic.get("licenseServer") or lic.get("license_server") or obj.license_server)
            obj.license_key_hash = str(
                lic.get("licenseKeyHash") or lic.get("license_key_hash") or obj.license_key_hash
            )
            obj.status = str(lic.get("status") or obj.status or "Unknown")
            obj.save()

        caps, _ = WorkstationCapability.objects.get_or_create(workstation=workstation)
        caps.supports_rdp = workstation.supports_rdp
        caps.supports_clipboard = workstation.supports_clipboard
        caps.supports_file_transfer = workstation.supports_file_transfer
        caps.supports_audio = workstation.supports_audio
        caps.supports_multi_monitor = workstation.supports_multi_monitor
        caps.gpu_available = bool(workstation.gpu)
        caps.ram_gb = workstation.memory_gb
        caps.cpu_cores = workstation.cpu_cores
        caps.disk_space_gb = workstation.storage_gb
        caps.save()

        present_count = InstalledSoftware.objects.filter(workstation=workstation, is_present=True).count()
        inventory.software_count = present_count
        inventory.license_count = SoftwareLicense.objects.filter(workstation=workstation).count()
        inventory.last_synced_at = now
        inventory.content_hash = hashlib.sha256(
            f"{present_count}:{added}:{removed}:{version_changed}".encode()
        ).hexdigest()
        inventory.save()

        workstation.last_inventory_update = now
        workstation.save(update_fields=["last_inventory_update", "updated_at"])
        update_workstation_health(workstation)

        record_event(
            category=AuditCategory.INVENTORY,
            action="Synchronized",
            details=(
                f"added={added} removed={removed} version_changed={version_changed} "
                f"catalog_linked={catalog_linked} total={present_count} mode={sync_mode}"
            ),
            workstation=workstation,
        )

        return {
            "accepted": True,
            "added": added,
            "removed": removed,
            "version_changed": version_changed,
            "catalog_linked": catalog_linked,
            "software_count": present_count,
        }

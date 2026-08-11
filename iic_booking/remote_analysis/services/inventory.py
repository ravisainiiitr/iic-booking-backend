"""Inventory synchronization — hardware, software, licenses, capabilities."""

from __future__ import annotations

import hashlib
from typing import Any

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

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


def _software_key(item: dict[str, Any]) -> str:
    name = str(item.get("displayName") or item.get("software_name") or item.get("Software") or "").strip().lower()
    version = str(item.get("version") or item.get("Version") or "").strip().lower()
    publisher = str(item.get("publisher") or item.get("Publisher") or "").strip().lower()
    return f"{name}|{version}|{publisher}"


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


class InventoryService:
    @transaction.atomic
    def synchronize(self, workstation: AnalysisWorkstation, payload: dict[str, Any]) -> dict[str, Any]:
        software_list = payload.get("software") or payload.get("installedSoftware") or []
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
        added = removed = version_changed = 0

        for item in software_list:
            if not isinstance(item, dict):
                continue
            name = str(item.get("displayName") or item.get("software_name") or "").strip()
            if not name:
                continue
            version = str(item.get("version") or "").strip()
            publisher = str(item.get("publisher") or "").strip()
            key = (name.lower(), version.lower(), publisher.lower())
            seen_keys.add(key)
            install_date = item.get("installDate") or item.get("install_date")
            if isinstance(install_date, str):
                install_date = parse_datetime(install_date)

            content_hash = item.get("contentHash") or item.get("ContentHash") or _content_hash(item)

            # Detect version change by name+publisher ignoring version
            prior_same_name = [
                s
                for (n, _v, p), s in existing.items()
                if n == name.lower() and p == publisher.lower() and _v != version.lower()
            ]

            if key in existing:
                row = existing[key]
                changed = False
                for field, value in (
                    ("executable", str(item.get("executable") or "")),
                    ("install_path", str(item.get("installPath") or item.get("install_path") or "")),
                    ("category", str(item.get("category") or "")),
                    ("licensed", bool(item.get("isLicensed") or item.get("licensed") or False)),
                    ("license_type", str(item.get("licenseType") or item.get("license_type") or "")),
                    ("content_hash", content_hash),
                ):
                    if getattr(row, field) != value:
                        setattr(row, field, value)
                        changed = True
                if install_date and row.install_date != install_date:
                    row.install_date = install_date
                    changed = True
                if changed:
                    row.save()
            else:
                InstalledSoftware.objects.create(
                    workstation=workstation,
                    software_name=name,
                    publisher=publisher,
                    version=version,
                    executable=str(item.get("executable") or ""),
                    install_path=str(item.get("installPath") or item.get("install_path") or ""),
                    install_date=install_date,
                    licensed=bool(item.get("isLicensed") or item.get("licensed") or False),
                    license_type=str(item.get("licenseType") or item.get("license_type") or ""),
                    category=str(item.get("category") or ""),
                    content_hash=content_hash,
                    is_present=True,
                )
                added += 1
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
            details=f"added={added} removed={removed} version_changed={version_changed} total={present_count}",
            workstation=workstation,
        )

        return {
            "accepted": True,
            "added": added,
            "removed": removed,
            "version_changed": version_changed,
            "software_count": present_count,
        }

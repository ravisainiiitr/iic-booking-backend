"""Resolve equipment analysis software mappings for Analyze Data."""

from __future__ import annotations

from django.db.models import Max

from iic_booking.remote_analysis.catalog_models import AnalysisSoftwareCatalog, EquipmentAnalysisSoftware
from iic_booking.remote_analysis.constants import WorkstationStatus
from iic_booking.remote_analysis.models import AnalysisWorkstation, InstalledSoftware
from iic_booking.remote_analysis.scheduler_models import SoftwareRequirement
from iic_booking.remote_analysis.services.availability import AvailabilityEngine
from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings


class SoftwareMappingService:
    def list_for_equipment(self, equipment) -> list[EquipmentAnalysisSoftware]:
        return list(
            EquipmentAnalysisSoftware.objects.filter(equipment=equipment, catalog__is_active=True)
            .select_related("catalog", "catalog__software_requirement")
            .order_by("sort_order", "catalog__name")
        )

    def required_software_names(self, equipment) -> list[str]:
        """All active catalog software names mapped to this equipment (deduped, ordered)."""
        names: list[str] = []
        seen: set[str] = set()
        for row in self.list_for_equipment(equipment):
            name = (row.catalog.name or "").strip()
            if not name:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            names.append(name)
        # Legacy free-text profile when no catalog mappings exist
        if not names:
            profile = (getattr(equipment, "analysis_profile", None) or "").strip()
            if profile:
                names.append(profile)
        return names

    def serialize_options(self, equipment, *, settings_obj: RemoteAnalysisSettings | None = None) -> list[dict]:
        settings_obj = settings_obj or RemoteAnalysisSettings.get_solo()
        default_label = (settings_obj.analyze_data_button_label or "Analyze Data").strip() or "Analyze Data"
        availability = AvailabilityEngine()
        options = []
        for row in self.list_for_equipment(equipment):
            cat = row.catalog
            stats = self._catalog_availability_stats(cat.name, availability=availability)
            options.append(
                {
                    "id": str(row.id),
                    "catalog_id": str(cat.id),
                    "slug": cat.slug,
                    "name": cat.name,
                    "vendor": cat.vendor,
                    "version_constraint": cat.version_constraint,
                    "is_default": bool(row.is_default),
                    "button_label": (row.button_label_override or default_label).strip() or default_label,
                    "default_session_duration_hours": cat.default_session_duration_hours,
                    "installed_count": stats["installed_count"],
                    "online_count": stats["online_count"],
                    "available_count": stats["available_count"],
                    "busy_count": stats["busy_count"],
                    "maintenance_count": stats["maintenance_count"],
                    "offline_count": stats["offline_count"],
                    "last_inventory_update": stats["last_inventory_update"],
                    "availability_status": stats["availability_status"],
                }
            )
        return options

    def _catalog_availability_stats(self, software_name: str, *, availability: AvailabilityEngine) -> dict:
        name = (software_name or "").strip()
        if not name:
            return {
                "installed_count": 0,
                "online_count": 0,
                "available_count": 0,
                "busy_count": 0,
                "maintenance_count": 0,
                "offline_count": 0,
                "last_inventory_update": None,
                "availability_status": "unconfigured",
            }

        ws_ids = list(
            InstalledSoftware.objects.filter(
                is_present=True,
                allocation_enabled=True,
                software_name__icontains=name,
            )
            .values_list("workstation_id", flat=True)
            .distinct()
        )
        if not ws_ids:
            return {
                "installed_count": 0,
                "online_count": 0,
                "available_count": 0,
                "busy_count": 0,
                "maintenance_count": 0,
                "offline_count": 0,
                "last_inventory_update": None,
                "availability_status": "none_installed",
            }

        workstations = list(AnalysisWorkstation.objects.filter(id__in=ws_ids, enabled=True))
        installed_count = len(workstations)
        online_count = sum(1 for ws in workstations if availability.heartbeat_fresh(ws))
        available_count = sum(
            1
            for ws in workstations
            if availability.heartbeat_fresh(ws)
            and ws.status in {WorkstationStatus.AVAILABLE, WorkstationStatus.ONLINE}
        )
        busy_count = sum(
            1
            for ws in workstations
            if ws.status in {WorkstationStatus.BUSY, WorkstationStatus.PREPARING, WorkstationStatus.RESERVED}
        )
        maintenance_count = sum(
            1
            for ws in workstations
            if ws.status
            in {
                WorkstationStatus.MAINTENANCE,
                WorkstationStatus.CALIBRATION,
                WorkstationStatus.SOFTWARE_UPDATE,
                WorkstationStatus.HARDWARE_FAULT,
                WorkstationStatus.CLEANING,
                WorkstationStatus.DISABLED,
                WorkstationStatus.ERROR,
                WorkstationStatus.UNKNOWN,
            }
        )
        offline_count = max(installed_count - online_count, 0)
        last_inventory_update = (
            AnalysisWorkstation.objects.filter(id__in=[ws.id for ws in workstations]).aggregate(
                last=Max("last_inventory_update")
            )["last"]
            if workstations
            else None
        )
        status = "available" if available_count > 0 else ("busy" if busy_count > 0 else "offline")
        return {
            "installed_count": installed_count,
            "online_count": online_count,
            "available_count": available_count,
            "busy_count": busy_count,
            "maintenance_count": maintenance_count,
            "offline_count": offline_count,
            "last_inventory_update": last_inventory_update,
            "availability_status": status,
        }

    def resolve(
        self,
        equipment,
        *,
        mapping_id: str | None = None,
        catalog_id: str | None = None,
        slug: str | None = None,
    ) -> tuple[EquipmentAnalysisSoftware | None, SoftwareRequirement | None]:
        qs = (
            EquipmentAnalysisSoftware.objects.filter(equipment=equipment, catalog__is_active=True)
            .select_related("catalog", "catalog__software_requirement")
        )
        row = None
        if mapping_id:
            row = qs.filter(id=mapping_id).first()
        elif catalog_id:
            row = qs.filter(catalog_id=catalog_id).first()
        elif slug:
            row = qs.filter(catalog__slug=slug).first()
        else:
            row = qs.filter(is_default=True).first() or qs.first()

        if row is None:
            # Legacy fallback: free-text analysis_profile
            profile = (getattr(equipment, "analysis_profile", None) or "").strip()
            if not profile:
                return None, None
            req = SoftwareRequirement.objects.filter(software__iexact=profile).first()
            if req is None:
                req = SoftwareRequirement.objects.create(
                    name=f"Legacy: {profile}",
                    software=profile,
                    required=True,
                )
            return None, req

        catalog: AnalysisSoftwareCatalog = row.catalog
        # Prefer live ensure for non-historical instances
        if hasattr(catalog, "ensure_software_requirement"):
            req = catalog.ensure_software_requirement()
        else:
            req = catalog.software_requirement
        return row, req

    def button_label(self, equipment, *, settings_obj: RemoteAnalysisSettings | None = None) -> str:
        settings_obj = settings_obj or RemoteAnalysisSettings.get_solo()
        default_label = (settings_obj.analyze_data_button_label or "Analyze Data").strip() or "Analyze Data"
        rows = self.list_for_equipment(equipment)
        if len(rows) == 1 and rows[0].button_label_override:
            return rows[0].button_label_override.strip() or default_label
        default_row = next((r for r in rows if r.is_default), None)
        if default_row and default_row.button_label_override:
            return default_row.button_label_override.strip() or default_label
        return default_label

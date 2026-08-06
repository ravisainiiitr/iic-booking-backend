"""Resolve equipment analysis software mappings for Analyze Data."""

from __future__ import annotations

from iic_booking.remote_analysis.catalog_models import AnalysisSoftwareCatalog, EquipmentAnalysisSoftware
from iic_booking.remote_analysis.scheduler_models import SoftwareRequirement
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
        options = []
        for row in self.list_for_equipment(equipment):
            cat = row.catalog
            options.append(
                {
                    "id": str(row.id),
                    "mapping_id": str(row.id),
                    "catalog_id": str(cat.id),
                    "slug": cat.slug,
                    "name": cat.name,
                    "display_name": cat.name,
                    "vendor": cat.vendor,
                    "version": cat.version_constraint,
                    "version_constraint": cat.version_constraint,
                    "description": cat.description,
                    "typical_usage": getattr(cat, "typical_usage", "") or "",
                    "accepted_file_types": getattr(cat, "accepted_file_types", None) or [],
                    "file_types": getattr(cat, "accepted_file_types", None) or [],
                    "license_type": cat.license_type,
                    "ai_tags": cat.ai_tags or [],
                    "ai_metadata": cat.ai_metadata or {},
                    "category": cat.category,
                    "icon_url": cat.icon_url,
                    "is_default": bool(row.is_default),
                    "button_label": (row.button_label_override or default_label).strip() or default_label,
                    "default_session_duration_hours": cat.default_session_duration_hours,
                }
            )
        return options

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

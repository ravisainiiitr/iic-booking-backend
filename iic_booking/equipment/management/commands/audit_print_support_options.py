"""
Report what users see in the 3D print booking "Supports" step on every 3D print equipment: the printer type the
estimate uses (OIC-chosen preset, else detected from make / model / name), whether the supports choice is shown,
whether Auto adds supports, and the support types and bed adhesion offered (with nothing configured by the OIC:
every type of the printer's technology, all bed adhesion options on FDM).

Read-only: no data step is needed for the support types, the defaults are applied when the equipment is read.
Use it after a deploy to confirm the options are visible; equipment flagged hidden=powder is detected as a
powder (SLS) printer, which prints without supports - pick the right preset on the OIC Fabrication Materials page
if that is wrong. Output: equipment ids, codes, presets and option keys only.

  python manage.py audit_print_support_options
  python manage.py audit_print_support_options --code IICTEST-3DP-01
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from iic_booking.equipment.models import Equipment, EquipmentProfileType
from iic_booking.equipment.print_estimate_model import (
    PRESETS,
    resolve_profile,
    resolved_support_options,
    stored_profile,
    supports_offered,
)


class Command(BaseCommand):
    help = "Report the support types and bed adhesion offered on each 3D print equipment (read-only)."

    def add_arguments(self, parser):
        parser.add_argument("--code", default="", help="Only equipment whose code contains this text.")

    def handle(self, *args, **options):
        qs = Equipment.objects.filter(profile_type=EquipmentProfileType.PRINT_3D).order_by("equipment_id")
        if options["code"]:
            qs = qs.filter(code__icontains=options["code"].strip())
        shown = hidden = 0
        for equipment in qs:
            stored = stored_profile(equipment)
            profile = resolve_profile(equipment, stored)
            tech = profile.get("technology", "")
            support = resolved_support_options(stored, tech)
            visible = supports_offered(tech)
            shown += visible
            hidden += not visible
            types = [t["key"] for t in support["types"] if t["enabled"]]
            adhesion = [a["key"] for a in support["adhesion"] if a["enabled"]]
            preset_source = "oic" if stored.get("preset") in PRESETS else "detected"
            self.stdout.write(
                f"EQUIPMENT id={equipment.equipment_id} code={equipment.code} preset={profile['preset']}"
                f"({preset_source}) technology={tech} supports_shown={'yes' if visible else 'no'}"
                f"{'' if visible else ' hidden=powder'} auto_adds_supports={'yes' if profile.get('supports', True) else 'no'}"
                f" types={','.join(types) or '-'} default_type={support['default_type'] or '-'}"
                f" adhesion={','.join(adhesion) or '-'}"
                f" oic_configured={'yes' if stored.get('support_options') else 'no'}"
            )
        self.stdout.write(f"SUMMARY print_3d_equipment={shown + hidden} supports_shown={shown} hidden_powder={hidden}")

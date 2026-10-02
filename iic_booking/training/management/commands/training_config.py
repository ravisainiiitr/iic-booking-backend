"""Main Admin Training settings from the command line (used by the Training Admin Config workflow).

Read-only unless a change is requested, and changes need ``--confirm TRAINING``. Writes the same rows and
audit entries as Admin Settings → Training Policy:

    training_config                                   # status
    training_config --find XRF                        # list equipment whose name/code matches (read-only)
    training_config --module on --audience TEST_ACCOUNTS --enable XRF01 --confirm TRAINING
    training_config --disable APREO --confirm TRAINING

All codes are checked before anything is written; an unknown code changes nothing.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from iic_booking.training.models import TrainingAudience

CONFIRM = "TRAINING"
FIND_LIMIT = 50


def split_codes(raw: str | None) -> list[str]:
    out: list[str] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


def find_equipment(text: str) -> list[dict]:
    from iic_booking.equipment.models import Equipment
    from iic_booking.training import access

    db_ids = access.db_enabled_equipment_ids()
    qs = Equipment.objects.select_related("internal_department").filter(
        Q(name__icontains=text) | Q(code__icontains=text)
    )
    return [
        {
            "code": e.code,
            "name": e.name,
            "status": e.status,
            "department": getattr(e.internal_department, "name", "") or "",
            "training_enabled": e.equipment_id in db_ids,
        }
        for e in qs.order_by("code")[:FIND_LIMIT]
    ]


class Command(BaseCommand):
    help = "Show or change the Main Admin Training module switch, audience and enabled equipment."

    def add_arguments(self, parser):
        parser.add_argument("--module", choices=["on", "off"], default=None)
        parser.add_argument("--audience", choices=list(TrainingAudience.values), default=None)
        parser.add_argument("--enable", default="", help="Comma-separated equipment codes to enable")
        parser.add_argument("--disable", default="", help="Comma-separated equipment codes to disable")
        parser.add_argument("--find", default="", help="Read-only: list equipment whose name or code contains this")
        parser.add_argument("--confirm", default="", help=f"Type {CONFIRM} to apply changes")
        parser.add_argument("--json", action="store_true")

    def handle(self, *args, **opts):
        from iic_booking.equipment.models import Equipment
        from iic_booking.training import module_config

        enable, disable = split_codes(opts["enable"]), split_codes(opts["disable"])
        both = set(enable) & set(disable)
        if both:
            raise CommandError(f"Codes cannot be enabled and disabled at once: {', '.join(sorted(both))}.")
        wants_change = opts["module"] is not None or opts["audience"] is not None or enable or disable
        changes: list[str] = []
        if wants_change:
            if (opts["confirm"] or "").strip() != CONFIRM:
                raise CommandError(f"Type --confirm {CONFIRM} to change Training settings.")
            found = {e.code: e for e in Equipment.objects.filter(code__in=enable + disable)}
            missing = [c for c in enable + disable if c not in found]
            if missing:
                raise CommandError(f"No equipment with code(s) {', '.join(missing)}; nothing was changed.")
            with transaction.atomic():
                if opts["module"] is not None or opts["audience"] is not None:
                    module_config.update_module(
                        None,
                        module_enabled=None if opts["module"] is None else opts["module"] == "on",
                        audience=opts["audience"],
                    )
                    if opts["module"] is not None:
                        changes.append(f"module={opts['module']}")
                    if opts["audience"] is not None:
                        changes.append(f"audience={opts['audience']}")
                for code in enable:
                    module_config.set_equipment_enabled(None, found[code], True)
                    changes.append(f"enabled {code}")
                for code in disable:
                    module_config.set_equipment_enabled(None, found[code], False)
                    changes.append(f"disabled {code}")

        state = module_config.module_state()
        report = {"changes": changes, "state": state}
        if opts["find"].strip():
            report["find"] = {"text": opts["find"].strip(), "results": find_equipment(opts["find"].strip())}
        if opts["json"]:
            self.stdout.write(json.dumps(report, indent=2, default=str))
            return
        lines = [f"change: {c}" for c in changes]
        lines += [
            f"module_enabled={state['module_enabled']} (env={state['env_module_enabled']} admin_switch={state['db_module_enabled']})",
            f"audience={state['audience']}",
            "all_equipment_in_scope=" + str(state["all_equipment_in_scope"]),
            "env_pilot_codes=" + (",".join(state["env_pilot_equipment_codes"]) or "-"),
        ]
        for row in state["enabled_equipment"]:
            lines.append(
                f"enabled_equipment {row['code']}: {row['name']} ({row['status']}) admin={row['enabled']} env={row['env_pilot']}"
            )
        for row in report.get("find", {}).get("results", []):
            lines.append(
                f"find {row['code']}: {row['name']} ({row['status']}, {row['department'] or '-'}) training_enabled={row['training_enabled']}"
            )
        self.stdout.write("\n".join(lines))

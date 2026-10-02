"""Read-only Training & Certification pilot status (used by the Apply Training Pilot Config workflow).

Reports the env switch and pilot list next to the Main Admin switch, audience and enabled equipment.

Never prints a full email address. With --codes / --oic-emails it validates proposed pilot values
instead of the running settings and exits non-zero when a code or email is not usable.
"""

from __future__ import annotations

import json
import re

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from iic_booking.training import access

CODE_RE = re.compile(r"^[A-Z0-9_-]+$")
EMAIL_RE = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")
HOUSEKEEPING_TASK_NAME = "training-housekeeping"


def mask_email(email: str) -> str:
    local, _, domain = (email or "").partition("@")
    return f"{local[:1]}***@{domain}" if local and domain else "***"


def split_csv(raw: str | None) -> list[str]:
    out: list[str] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


def migration_state() -> dict:
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(connection, ignore_no_migrations=True)
    on_disk = sorted(name for app, name in loader.disk_migrations if app == "training")
    applied = sorted(name for app, name in loader.applied_migrations if app == "training")
    return {"on_disk": on_disk, "applied": applied, "pending": [n for n in on_disk if n not in applied]}


def housekeeping_state() -> dict:
    from django_celery_beat.models import PeriodicTask

    task = PeriodicTask.objects.filter(name=HOUSEKEEPING_TASK_NAME).first()
    return {"name": HOUSEKEEPING_TASK_NAME, "exists": task is not None, "enabled": bool(task and task.enabled)}


def build_report(codes: list[str], emails: list[str], *, validate: bool) -> dict:
    from django.contrib.auth import get_user_model

    from iic_booking.equipment.models import Equipment
    from iic_booking.equipment.reports import get_equipment_ids_managed_by_oic
    from iic_booking.users.models.user_type import UserType

    errors: list[str] = []
    warnings: list[str] = []
    if validate and not codes:
        errors.append("Give at least one equipment code.")

    found = {e.code: e for e in Equipment.objects.filter(code__in=codes)}
    equipment = []
    for code in codes:
        eq = found.get(code)
        if not CODE_RE.match(code):
            errors.append(f"Equipment code '{code}' is not valid: use uppercase letters, digits, '-' or '_'.")
        elif eq is None:
            errors.append(f"No equipment with code {code} exists.")
        equipment.append(
            {"code": code, "found": eq is not None, "name": eq.name if eq else "", "status": eq.status if eq else ""}
        )
    pilot_ids = {e.equipment_id for e in found.values()}

    User = get_user_model()
    oics = []
    for email in emails:
        masked = mask_email(email)
        row = {"email": masked, "active_user": False, "is_pilot_oic": False}
        oics.append(row)
        if not EMAIL_RE.match(email):
            errors.append(f"OIC email {masked} is not a valid email address.")
            continue
        user = User.objects.filter(email__iexact=email).order_by("-is_active").first()
        if user is None or not user.is_active:
            errors.append(f"OIC email {masked} does not belong to an active user.")
            continue
        row["active_user"] = True
        managed = set(get_equipment_ids_managed_by_oic(user.id))
        in_scope = managed & pilot_ids if codes else managed
        row["is_pilot_oic"] = user.user_type == UserType.MANAGER and bool(in_scope)
        if not row["is_pilot_oic"]:
            warnings.append(
                f"{masked} is not an OIC or temporary OIC of the pilot equipment, "
                "so they will not see the Training workspace."
            )

    scope_ids = access.pilot_equipment_ids()
    db_ids = access.db_enabled_equipment_ids()
    db_equipment = [
        {"code": e.code, "name": e.name, "status": e.status}
        for e in Equipment.objects.filter(equipment_id__in=db_ids).order_by("code")
    ]
    effective = (
        None
        if scope_ids is None
        else sorted(Equipment.objects.filter(equipment_id__in=scope_ids).values_list("code", flat=True))
    )
    return {
        "mode": "validate" if validate else "status",
        "module_enabled": access.module_enabled(),
        "env_module_enabled": access.env_module_enabled(),
        "db_module_enabled": access.db_module_enabled(),
        "audience": access.audience(),
        "db_enabled_equipment": db_equipment,
        "effective_equipment_codes": effective,
        "pilot_scope": "listed equipment" if codes else "all equipment",
        "pilot_equipment": equipment,
        "pilot_oic_email_count": len(emails),
        "pilot_oics": oics,
        "migrations": migration_state(),
        "housekeeping_task": housekeeping_state(),
        "errors": errors,
        "warnings": warnings,
    }


def format_human(report: dict) -> str:
    effective = report["effective_equipment_codes"]
    lines = [
        f"training_module_enabled={report['module_enabled']} "
        f"(env={report['env_module_enabled']} admin_switch={report['db_module_enabled']})",
        f"audience={report['audience']}",
        "admin_enabled_equipment=" + (",".join(e["code"] for e in report["db_enabled_equipment"]) or "-"),
        "effective_equipment=" + ("ALL" if effective is None else ",".join(effective) or "-"),
        f"pilot_scope={report['pilot_scope']}",
    ]
    for eq in report["pilot_equipment"]:
        state = f"found ({eq['name']}, {eq['status']})" if eq["found"] else "NOT FOUND"
        lines.append(f"pilot_equipment {eq['code']}: {state}")
    lines.append(f"pilot_oic_email_count={report['pilot_oic_email_count']}")
    for oic in report["pilot_oics"]:
        lines.append(f"pilot_oic {oic['email']}: active_user={oic['active_user']} is_pilot_oic={oic['is_pilot_oic']}")
    mig = report["migrations"]
    lines.append(f"migrations applied={mig['applied']} pending={mig['pending']}")
    task = report["housekeeping_task"]
    lines.append(f"periodic_task {task['name']}: exists={task['exists']} enabled={task['enabled']}")
    lines += [f"WARNING: {w}" for w in report["warnings"]]
    lines += [f"ERROR: {e}" for e in report["errors"]]
    return "\n".join(lines)


class Command(BaseCommand):
    help = "Report the Training pilot configuration (read-only), or validate proposed pilot codes/emails."

    def add_arguments(self, parser):
        parser.add_argument("--codes", default=None, help="Comma-separated equipment codes to validate")
        parser.add_argument("--oic-emails", default=None, help="Comma-separated OIC emails to validate")
        parser.add_argument("--json", action="store_true", help="Emit JSON only")

    def handle(self, *args, **options):
        validate = options["codes"] is not None or options["oic_emails"] is not None
        codes = split_csv(options["codes"]) if options["codes"] is not None else sorted(access.pilot_equipment_codes())
        if options["oic_emails"] is not None:
            emails = [e.lower() for e in split_csv(options["oic_emails"])]
        else:
            emails = sorted(access.pilot_oic_emails())

        report = build_report(codes, emails, validate=validate)
        self.stdout.write(json.dumps(report, indent=2) if options["json"] else format_human(report))
        if validate and report["errors"]:
            raise CommandError("Pilot values are not valid: " + " ".join(report["errors"]))

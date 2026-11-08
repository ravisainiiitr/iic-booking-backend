"""I-STEM Equipment Create for ONE portal instrument, run from the "I-STEM Equipment Create" workflow (8 Nov 2026).

Same code as the Main Admin's screen (``iic_booking.istem.equipment_create``), so a run behaves exactly like the button.

Inputs (environment):
  ACTION     sync-lists | show | set | dry-run | create
             sync-lists  read I-STEM's funding agencies, agency types, categories and facilities into the portal
             show        the instrument's I-STEM details, what is missing, and the choices (read-only)
             set         save DETAILS on the portal (nothing is sent to I-STEM)
             dry-run     build the Equipment Create form and print it (nothing is sent)
             create      send Equipment Create; I-STEM's new id becomes the instrument's mapping
  EQUIPMENT  equipment code or numeric ID (show/set/dry-run/create)
  DETAILS    set: "field=value;field=value" (fields as in equipment_create.EDITABLE)
  ACTOR_EMAIL  Main Admin recorded as the actor (default: the first active Main Admin)

Prints field names, I-STEM ids and instrument data only; email addresses are masked.
"""

import os
import re
import sys

import django

django.setup()

from django.contrib.auth import get_user_model  # noqa: E402

from iic_booking.equipment.models import Equipment  # noqa: E402
from iic_booking.istem import equipment_create as ec  # noqa: E402
from iic_booking.istem.equipment_mapping import MappingRefused  # noqa: E402
from iic_booking.users.models import UserType  # noqa: E402

ACTION = os.environ.get("ACTION", "show").strip()
EQUIPMENT = os.environ.get("EQUIPMENT", "").strip()


def out(*parts):
    print(*parts, flush=True)


def masked(name, value):
    value = str(value or "")
    if name in ec.EMAIL_FIELDS and "@" in value:
        local, _, domain = value.partition("@")
        return f"{local[:1]}***@{domain}"
    return value


def actor():
    User = get_user_model()
    email = os.environ.get("ACTOR_EMAIL", "").strip()
    qs = User.objects.filter(is_active=True, user_type=UserType.ADMIN)
    user = qs.filter(email__iexact=email).first() if email else qs.order_by("pk").first()
    if user is None:
        sys.exit("no active Main Admin found")
    return user


def equipment():
    if not EQUIPMENT:
        sys.exit("EQUIPMENT is required")
    qs = Equipment.objects.all()
    found = qs.filter(pk=int(EQUIPMENT)).first() if EQUIPMENT.isdigit() else qs.filter(code__iexact=EQUIPMENT).first()
    if found is None:
        sys.exit("equipment not found")
    return found


def show(body):
    e = body["equipment"]
    out(f"instrument {e['id']} {e['code']} | {e['name']} | department: {e['department'] or '-'}")
    out("mapping:", body["mapping"] or "none", "| created on I-STEM:", body["created"] or "no")
    for f in body["fields"]:
        value = body["details"].get(f["name"]) or ""
        flag = "REQUIRED" if f["required"] else "optional"
        out(f"  {f['name']:<26} {flag:<8} {masked(f['name'], value)[:100] or '-'}")
    if body["problems"]:
        out("MISSING / WRONG:")
        for name, message in body["problems"].items():
            out(f"  {name}: {message}")
    else:
        out("ready to send")
    for kind, rows in body["choices"].items():
        out(f"choices {kind}: {len(rows)}")
        for row in rows[:40]:
            out(f"    {row.get('id') or '-'} | {row['name']}")
    if body.get("last_error"):
        out("last error:", body["last_error"][:300])


def main():
    user = actor()
    try:
        if ACTION == "sync-lists":
            result = ec.sync_reference_lists(user)
            out("synced:", result["counts"], "| failed:", result["failed"] or "none")
            return
        e = equipment()
        if ACTION == "show":
            show(ec.get_details(user, e.pk))
        elif ACTION == "set":
            data = {}
            for part in os.environ.get("DETAILS", "").split(";"):
                if "=" in part:
                    key, _, value = part.partition("=")
                    key = key.strip()
                    if key not in ec.EDITABLE:
                        sys.exit(f"unknown field {key}")
                    data[key] = value.strip()
            if not data:
                sys.exit("DETAILS is empty")
            show(ec.save_details(user, e.pk, data))
        elif ACTION == "dry-run":
            result = ec.create_on_istem(user, e.pk, dry_run=True)
            out("Equipment Create form (NOT sent):")
            for key, value in result["payload"].items():
                out(f"  {key} = {masked(key, value)[:120]}")
        elif ACTION == "create":
            result = ec.create_on_istem(user, e.pk)
            out("CREATED on I-STEM: equipment_id", result["istem_equipment_id"], "| mapped to instrument", e.pk)
            for warning in result["warnings"]:
                out("  note:", warning)
        else:
            sys.exit(f"unknown ACTION {ACTION}")
    except MappingRefused as exc:
        out("REFUSED |", re.sub(r"\S+@\S+", "***", exc.message))
        sys.exit(1)


main()

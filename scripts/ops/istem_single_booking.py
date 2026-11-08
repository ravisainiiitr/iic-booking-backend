"""I-STEM sync for ONE booking, run by hand from the "I-STEM Single Booking" workflow (piped into the django container).

Nothing schedules the I-STEM sync on production and ``istem_sync --enqueue`` would pick up every eligible booking, so
this script works on a single booking (or a single instrument) only, and processes only that booking's outbox jobs.

Inputs (environment):
  ACTION        inspect | map-equipment | set-codes | create | confirm | execute | status
  BOOKING       booking virtual ID (e.g. IIC-...) or numeric primary key  (create/confirm/execute/status, optional for inspect)
  EQUIPMENT     equipment code or primary key                              (inspect/map-equipment/set-codes)
  ISTEM_EQUIPMENT_ID  I-STEM equipment_id                                  (map-equipment)
  REMOTE_NAME   exact I-STEM equipment name to look up (read-only)         (inspect, map-equipment)
  CODES         "TYPE:local_key=value;..." equipment-specific code mappings (set-codes)
  ACTOR_EMAIL   administrator recorded as the actor (default: the first active superuser)
  WAIVE_FBR_FLAG  "yes": this booking is sent even though its charge profile does not require an I-STEM FBR. The
                  waiver lives only in this process and only for this booking; the charge profile is not changed.

Prints identifiers, states and field names only: no tokens, passwords or personal details.
"""

import os
import sys

import django

django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.db import transaction  # noqa: E402
from django.utils import timezone  # noqa: E402

from iic_booking.equipment.models import Booking, Equipment  # noqa: E402
from iic_booking.istem import conf, eligibility, mapping, outbox, service  # noqa: E402
from iic_booking.istem.choices import CodeType, SyncState  # noqa: E402
from iic_booking.istem.models import IstemBookingLink, IstemCodeMapping, IstemEquipmentMapping, IstemOutboxJob, IstemUserLink  # noqa: E402

ACTION = os.environ.get("ACTION", "inspect").strip()
BOOKING = os.environ.get("BOOKING", "").strip()
EQUIPMENT = os.environ.get("EQUIPMENT", "").strip()
WAIVE = os.environ.get("WAIVE_FBR_FLAG", "").strip().lower() == "yes"
FAILED = []


def out(*parts):
    print(*parts, flush=True)


def fail(message):
    FAILED.append(message)
    out("FAIL |", message)


def actor():
    User = get_user_model()
    email = os.environ.get("ACTOR_EMAIL", "").strip()
    qs = User.objects.filter(is_active=True, is_superuser=True)
    user = qs.filter(email__iexact=email).first() if email else qs.order_by("pk").first()
    if user is None:
        sys.exit("no active superuser found for ACTOR_EMAIL")
    return user


def find_booking():
    if not BOOKING:
        return None
    qs = Booking.objects.select_related("equipment", "charge_profile", "user")
    booking = qs.filter(pk=int(BOOKING)).first() if BOOKING.isdigit() else qs.filter(virtual_booking_id__iexact=BOOKING).first()
    if booking is None:
        sys.exit(f"booking {BOOKING} not found")
    return booking


def find_equipment(booking=None):
    if EQUIPMENT:
        qs = Equipment.objects.all()
        eq = qs.filter(pk=int(EQUIPMENT)).first() if EQUIPMENT.isdigit() else qs.filter(code__iexact=EQUIPMENT).first()
        if eq is None:
            matches = list(qs.filter(name__icontains=EQUIPMENT).values_list("pk", "code", "name")[:15])
            out("equipment matches for", repr(EQUIPMENT), ":", matches or "none")
            return None
        return eq
    return booking.equipment if booking else None


_original_evaluate = eligibility.evaluate


def install_waiver(booking):
    """Skip only the charge-profile FBR requirement, only for this booking, only in this process."""
    def evaluate(b):
        ok, reason = _original_evaluate(b)
        if not ok and b.pk == booking.pk and reason == "charge profile does not require an I-STEM FBR":
            profile = b.charge_profile
            try:
                b.charge_profile = type("P", (), {"require_istem_fbr": True, "profile_type": getattr(profile, "profile_type", "")})()
                return _original_evaluate(b)
            finally:
                b.charge_profile = profile
        return ok, reason
    for module in (eligibility, outbox, service):
        module.evaluate = evaluate
    out("INFO | FBR-flag waiver active for booking", booking.pk, "only")


def remote_lookup(name):
    client = service.default_client()
    # The institute is the one I-STEM names in the token (no longer a portal setting).
    page = client.list_equipment(institute_id=client.session_info().institute_id, equipment_name=name, limit=10)
    return page.rows


def show_switches():
    out("switches | mode=", conf.istem_mode(), "confirm=", conf.confirm_enabled(), "execute=", conf.execute_enabled(),
        "approved_by_set=", bool(conf.confirm_approved_by()), "sync_start_at=", eligibility.sync_start_at())


def show_equipment(eq):
    out("equipment |", eq.pk, eq.code, "|", eq.name)
    m = IstemEquipmentMapping.objects.filter(equipment=eq).first()
    if m:
        out("  mapping | istem_equipment_id=", m.istem_equipment_id, "active=", m.is_active, "verified=", bool(m.verified_at),
            "sample_key=", m.sample_count_input_key or "-", "remote_found=", m.remote_found, "remote_name=", m.remote_equipment_name or "-")
    else:
        out("  mapping | none")
    for cp in eq.charge_profiles.all().order_by("pk"):
        out("  charge profile |", cp.pk, "user_type=", getattr(cp, "user_type", ""), "profile_type=", getattr(cp, "profile_type", ""),
            "require_istem_fbr=", getattr(cp, "require_istem_fbr", False))
    for row in IstemCodeMapping.objects.filter(is_active=True).filter(equipment=eq).order_by("code_type", "local_key"):
        out("  code (equipment) |", row.code_type, row.local_key, "->", row.istem_value)
    for row in IstemCodeMapping.objects.filter(is_active=True, equipment__isnull=True).order_by("code_type", "local_key"):
        out("  code (global) |", row.code_type, row.local_key, "->", row.istem_value)


def show_booking(booking):
    out("booking |", booking.pk, booking.virtual_booking_id, "status=", booking.status, "user=", booking.user_id,
        "user_type_snapshot=", booking.user_type_snapshot, "charge_profile=", booking.charge_profile_id,
        "profile_type=", getattr(booking.charge_profile, "profile_type", ""), "minutes=", booking.total_time_minutes,
        "completed_at=", booking.completed_at)
    ok, reason = eligibility.evaluate(booking)
    out("  eligible for Create Booking:", ok, reason)
    ul = IstemUserLink.objects.filter(user=booking.user).first()
    out("  user link |", (ul.state, ul.public_user_id) if ul else "none")
    built = mapping.build_create_booking_payload(booking, ul)
    out("  create payload |", "ready" if built.ok else "missing: " + "; ".join(f"{k}: {v}" for k, v in sorted(built.errors.items())))
    link = IstemBookingLink.objects.filter(booking=booking).first()
    if link:
        out("  link |", link.sync_state, "request_id=", link.request_id, "fbr_no=", getattr(link, "fbr_no", None),
            "confirmation_id=", link.confirmation_id, "payment_id=", getattr(link, "payment_id", None),
            "remote_status=", getattr(link, "remote_status", ""), "review=", (link.review_reason or "")[:300])
        for job in IstemOutboxJob.objects.filter(booking_link=link).order_by("pk"):
            out("  job |", job.pk, job.operation, job.state, (job.last_error_message or "")[:300] if hasattr(job, "last_error_message") else "")


def run_jobs(link):
    """This booking's open jobs, including one queued by an earlier job (Confirm Booking follows Create Booking)."""
    seen = set()
    for _ in range(5):
        jobs = list(IstemOutboxJob.objects.filter(booking_link=link).exclude(state__in=["DONE", "CANCELLED"])
                    .exclude(pk__in=seen).order_by("pk"))
        if not jobs:
            return
        for job in jobs:
            seen.add(job.pk)
            out("process job", job.pk, job.operation, "->", service.process_job(job.pk))


def main():
    out("action:", ACTION)
    show_switches()
    booking = find_booking()
    if booking and WAIVE:
        install_waiver(booking)
    eq = find_equipment(booking)

    if ACTION == "inspect":
        if eq:
            show_equipment(eq)
        name = os.environ.get("REMOTE_NAME", "").strip()
        if name:
            out("I-STEM equipment named", repr(name), ":", remote_lookup(name) or "none")
        if booking:
            show_booking(booking)
        return

    if ACTION == "map-equipment":
        if eq is None:
            sys.exit("EQUIPMENT not found")
        istem_id = os.environ.get("ISTEM_EQUIPMENT_ID", "").strip()
        if not istem_id.isdigit():
            sys.exit("ISTEM_EQUIPMENT_ID must be numeric")
        name = os.environ.get("REMOTE_NAME", "").strip()
        rows = remote_lookup(name) if name else []
        if name and not any(str(r.get("equipment_id")) == istem_id for r in rows):
            sys.exit(f"I-STEM equipment {istem_id} is not listed under the name {name!r}: {rows}")
        with transaction.atomic():
            m, created = IstemEquipmentMapping.objects.update_or_create(equipment=eq, defaults={
                "istem_equipment_id": istem_id, "is_active": True, "verified_by": actor(), "verified_at": timezone.now(),
                "remote_checked_at": timezone.now() if rows else None, "remote_found": bool(rows),
                "remote_equipment_name": (rows[0]["equipment_name"] if rows else "")[:255],
                "notes": "Mapped for the I-STEM end-to-end demo (I-STEM staging).",
            })
            sample_key = os.environ.get("SAMPLE_KEY", "").strip()
            if sample_key:
                m.sample_count_input_key = sample_key
                m.save(update_fields=["sample_count_input_key", "updated_at"])
        out("mapping", "created" if created else "updated")
        show_equipment(eq)
        return

    if ACTION == "unmap-equipment":
        # Switch off the named instrument's I-STEM mapping (kept, not deleted, so the change can be undone and audited).
        if eq is None:
            sys.exit("EQUIPMENT not found")
        m = IstemEquipmentMapping.objects.filter(equipment=eq).first()
        if m is None:
            out("mapping | none to switch off")
        elif not m.is_active:
            out("mapping | already off")
        else:
            m.is_active = False
            m.notes = ((m.notes or "") + f" Switched off {timezone.now():%Y-%m-%d}: wrong I-STEM instrument.").strip()[:2000]
            m.save(update_fields=["is_active", "notes", "updated_at"])
            out("mapping | switched off (was istem_equipment_id=", m.istem_equipment_id, ")")
        show_equipment(eq)
        return

    if ACTION == "set-codes":
        if eq is None:
            sys.exit("EQUIPMENT not found")
        valid = {c.value for c in CodeType}
        for item in [x.strip() for x in os.environ.get("CODES", "").split(";") if x.strip()]:
            left, value = item.split("=", 1)
            code_type, local_key = left.split(":", 1)
            if code_type not in valid:
                sys.exit(f"unknown code type {code_type}")
            IstemCodeMapping.objects.update_or_create(code_type=code_type, local_key=local_key.strip(), equipment=eq,
                                                      defaults={"istem_value": value.strip(), "is_active": True,
                                                                "notes": "I-STEM end-to-end demo"})
            out("code set |", code_type, local_key, "->", value)
        show_equipment(eq)
        return

    if booking is None:
        sys.exit("BOOKING is required")

    if ACTION == "create":
        link = outbox.enqueue_booking_sync(booking, trigger="single-booking workflow")
        if link is None:
            sys.exit("integration off")
        out("link after enqueue:", link.sync_state, link.eligibility_reason)
        run_jobs(link)
        # Create may park the booking for the user mapping: process that and the re-queued create once.
        ul = IstemUserLink.objects.filter(user=booking.user).first()
        if ul:
            for job in IstemOutboxJob.objects.filter(user_link=ul).exclude(state__in=["DONE", "CANCELLED"]).order_by("pk"):
                out("process user job", job.pk, job.operation, "->", service.process_job(job.pk))
            run_jobs(link)
    elif ACTION == "confirm":
        link = IstemBookingLink.objects.get(booking=booking)
        service.request_confirmation(link, actor=actor(), reason="Slot confirmed by the Officer in Charge (single-booking workflow)")
        run_jobs(link)
    elif ACTION == "execute":
        link = outbox.enqueue_fbr_execute(booking, trigger="single-booking workflow")
        if link is None:
            sys.exit("FBR Execute is off, or the booking was never sent to I-STEM")
        run_jobs(link)
    elif ACTION == "status":
        link = IstemBookingLink.objects.get(booking=booking)
        out("remote status:", service.refresh_remote_status(link))
    else:
        sys.exit(f"unknown ACTION {ACTION}")
    booking.refresh_from_db()
    show_booking(booking)
    link = IstemBookingLink.objects.filter(booking=booking).first()
    # With confirmation on, Confirm Booking follows Create Booking automatically.
    expected = {"create": (SyncState.CREATED, SyncState.CONFIRMED), "confirm": (SyncState.CONFIRMED,),
                "execute": (SyncState.EXECUTED, SyncState.EXECUTED_PENDING_VERIFY)}.get(ACTION)
    if expected and (link is None or link.sync_state not in expected):
        fail(f"link is {link.sync_state if link else 'missing'}, expected {expected}")


main()
if FAILED:
    sys.exit(1)
out("OK")

"""Email the lab inboxes with design files (STL / DXF) when a fabrication booking is confirmed or its files change.

Covers 3D printing (STL) and 2D laser cutting (DXF). Recipients come from
``Equipment.fabrication_notification_emails``. Files are attached up to
``FABRICATION_EMAIL_MAX_ATTACHMENT_BYTES`` in total; the rest are sent as download links.
"""

import logging
import re
from typing import Optional

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import transaction
from django.utils import timezone

from iic_booking.communication.email_branding import strftime_slot_end
from iic_booking.communication.utils import booking_display_id_for_email

logger = logging.getLogger(__name__)

REASON_CONFIRMED = "confirmed"
REASON_FILES_UPDATED = "files_updated"
REASON_FILES_RESTORED = "files_restored"
REASON_FILES_REPLACED_AFTER_REJECTION = "files_replaced_after_rejection"
UPDATED_FILE_REASONS = (REASON_FILES_UPDATED, REASON_FILES_RESTORED, REASON_FILES_REPLACED_AFTER_REJECTION)


def notification_recipients(equipment) -> list[str]:
    emails = []
    seen = set()
    for raw in getattr(equipment, "fabrication_notification_emails", None) or []:
        email = str(raw or "").strip()
        if email and email.lower() not in seen:
            seen.add(email.lower())
            emails.append(email)
    return emails


def lab_team_recipients(equipment) -> list[str]:
    """Notification list plus the equipment's Lab Operators and Officer(s) In Charge, deduplicated."""
    from iic_booking.users.test_accounts import redirect_email_for_user

    from .reports import get_equipment_staff_notify_users

    emails = notification_recipients(equipment)
    seen = {e.lower() for e in emails}
    for staff in get_equipment_staff_notify_users(equipment):
        original = (getattr(staff, "email", "") or "").strip()
        if not original:
            continue
        delivery, _subject = redirect_email_for_user(staff, original_email=original)
        for email in delivery or [original]:
            email = (email or "").strip()
            if email and email.lower() not in seen:
                seen.add(email.lower())
                emails.append(email)
    return emails


def recipients_for_reason(equipment, reason: str) -> list[str]:
    if reason == REASON_FILES_REPLACED_AFTER_REJECTION:
        return lab_team_recipients(equipment)
    return notification_recipients(equipment)


def should_send_print_3d_stl_notification(
    event_type: str,
    *,
    previous_status: Optional[str] = None,
    new_status: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> bool:
    from .models import BookingEventType, BookingStatus

    if event_type in (BookingEventType.CREATED, BookingEventType.CONFIRMED):
        return True
    metadata = metadata or {}
    if event_type == BookingEventType.STATUS_CHANGED:
        if previous_status == BookingStatus.HOLD and new_status == BookingStatus.BOOKED:
            return True
        if metadata.get("urgent_hold_converted"):
            return True
    return False


def dispatch_fabrication_file_email(booking, *, reason: str = REASON_CONFIRMED) -> None:
    """Queue the design-file email (after commit). Never raises."""
    from .fabrication import is_fabrication_equipment

    equipment = getattr(booking, "equipment", None)
    if not is_fabrication_equipment(equipment) or not recipients_for_reason(equipment, reason):
        return

    booking_id = booking.booking_id

    def _dispatch():
        import threading

        def _run():
            try:
                try:
                    from iic_booking.equipment.tasks import send_print_3d_stl_booking_email_task

                    send_print_3d_stl_booking_email_task.delay(booking_id, reason=reason)
                    return
                except Exception:
                    logger.warning(
                        "Failed to queue fabrication file email for booking_id=%s; sending inline",
                        booking_id,
                        exc_info=True,
                    )
                try:
                    send_print_3d_stl_booking_email(booking_id, reason=reason)
                except Exception:
                    # Never break booking flow for email/SMTP issues (e.g. SMTP 535 auth errors)
                    logger.exception("fabrication_file_email: send failed for booking_id=%s (ignored)", booking_id)
            except Exception:
                logger.exception(
                    "fabrication_file_email: unexpected dispatch failure for booking_id=%s (ignored)", booking_id
                )

        threading.Thread(target=_run, name=f"fabrication-notify-{booking_id}", daemon=True).start()

    if transaction.get_connection().in_atomic_block:
        transaction.on_commit(_dispatch)
    else:
        _dispatch()


def maybe_dispatch_print_3d_stl_notification(
    booking,
    event_type: str,
    *,
    previous_status: Optional[str] = None,
    new_status: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> None:
    """Queue the design-file notification after booking confirmation (on transaction commit)."""
    if not should_send_print_3d_stl_notification(
        event_type,
        previous_status=previous_status,
        new_status=new_status,
        metadata=metadata,
    ):
        return
    dispatch_fabrication_file_email(booking, reason=REASON_CONFIRMED)


def should_cleanup_print_3d_stl_files(
    event_type: str,
    *,
    new_status: Optional[str] = None,
) -> bool:
    from .models import BookingEventType, BookingStatus

    if event_type == BookingEventType.COMPLETED:
        return True
    if event_type == BookingEventType.STATUS_CHANGED and new_status == BookingStatus.COMPLETED:
        return True
    return False


def maybe_dispatch_print_3d_stl_cleanup(
    booking,
    event_type: str,
    *,
    new_status: Optional[str] = None,
) -> None:
    """Delete STL/DXF files from storage once a fabrication booking is completed."""
    from .fabrication import is_fabrication_equipment

    if not should_cleanup_print_3d_stl_files(event_type, new_status=new_status):
        return

    equipment = getattr(booking, "equipment", None)
    if not is_fabrication_equipment(equipment):
        return

    booking_id = booking.booking_id

    def _dispatch():
        try:
            try:
                from iic_booking.equipment.tasks import delete_print_3d_booking_stl_files_task

                delete_print_3d_booking_stl_files_task.delay(booking_id)
                return
            except Exception:
                logger.warning(
                    "Failed to queue fabrication file cleanup for booking_id=%s; running inline",
                    booking_id,
                    exc_info=True,
                )
            try:
                delete_print_3d_booking_stl_files(booking_id)
            except Exception:
                logger.exception("fabrication_file_cleanup: failed for booking_id=%s (ignored)", booking_id)
        except Exception:
            logger.exception(
                "fabrication_file_cleanup: unexpected dispatch failure for booking_id=%s (ignored)", booking_id
            )

    if transaction.get_connection().in_atomic_block:
        transaction.on_commit(_dispatch)
    else:
        _dispatch()


def _delete_stored_file(record, field_name: str) -> int:
    from django.core.files.storage import default_storage

    file_field = getattr(record, field_name, None)
    if not file_field or not file_field.name:
        return 0
    storage = getattr(file_field, "storage", None) or default_storage
    name = file_field.name
    candidate_names = [name]
    if name.startswith("media/"):
        candidate_names.append(name[len("media/"):])
    else:
        candidate_names.append(f"media/{name}")
    deleted = 0
    try:
        resolved = next((n for n in candidate_names if storage.exists(n)), None)
        if resolved:
            storage.delete(resolved)
            deleted = 1
    except Exception:
        logger.exception("Failed deleting %s for %s", field_name, record.pk)
    # Clear the DB field regardless (so we don't keep pointing at a deleted object)
    try:
        setattr(record, field_name, "")
        record.save(update_fields=[field_name])
    except Exception:
        logger.exception("Failed clearing %s for %s", field_name, record.pk)
    return deleted


def delete_print_3d_booking_stl_files(booking_id: int) -> int:
    """
    Delete STL/DXF objects from the configured storage (S3/local) for a completed booking,
    including files replaced by a re-upload, and clear the DB FileFields.

    Returns: number of deleted objects.
    """
    from django.db.models import Q

    from .models import Booking, BookingStatus, LaserCutAnalysis, PrintAnalysis

    try:
        booking = Booking.objects.select_related("equipment").get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return 0
    if booking.status != BookingStatus.COMPLETED:
        return 0

    deleted = 0
    for analysis in PrintAnalysis.objects.filter(Q(booking=booking) | Q(superseded_booking=booking)).only(
        "id", "stl_file"
    ):
        deleted += _delete_stored_file(analysis, "stl_file")
    for analysis in LaserCutAnalysis.objects.filter(Q(booking=booking) | Q(superseded_booking=booking)).only(
        "id", "dxf_file"
    ):
        deleted += _delete_stored_file(analysis, "dxf_file")
    return deleted


def _design_files_for_booking(booking):
    """[(analysis, file_field, default_name)] for the booking's active design files."""
    from .fabrication import active_laser_analyses_for_booking, active_print_analyses_for_booking
    from .models import EquipmentProfileType

    if booking.equipment.profile_type == EquipmentProfileType.LASER_CUT_2D:
        return [(a, a.dxf_file, "part.dxf") for a in active_laser_analyses_for_booking(booking)]
    return [(a, a.stl_file, "model.stl") for a in active_print_analyses_for_booking(booking)]


def send_print_3d_stl_booking_email(booking_id: int, reason: str = REASON_CONFIRMED) -> bool:
    """Send booking details and design file(s) to the equipment's fabrication notification inboxes."""
    from .fabrication import is_fabrication_equipment
    from .models import Booking, EquipmentProfileType

    try:
        booking = (
            Booking.objects.select_related("user", "equipment", "print_analysis", "print_analysis_batch")
            .prefetch_related("daily_slots__slot_master")
            .get(booking_id=booking_id)
        )
    except Booking.DoesNotExist:
        logger.warning("fabrication_file_email: booking_id=%s not found", booking_id)
        return False

    equipment = booking.equipment
    if not is_fabrication_equipment(equipment):
        return False
    recipients = recipients_for_reason(equipment, reason)
    if not recipients:
        return False

    files = _design_files_for_booking(booking)
    attachments, links = _collect_attachments_and_links(files)
    is_laser = equipment.profile_type == EquipmentProfileType.LASER_CUT_2D
    body = _build_email_body(booking, files, attachments, links, reason=reason, is_laser=is_laser)
    kind = "Laser cutting" if is_laser else "3D print"
    if reason == REASON_FILES_REPLACED_AFTER_REJECTION:
        prefix = "NEW FILES AFTER REJECTION — "
    else:
        prefix = "UPDATED FILES — " if reason in UPDATED_FILE_REASONS else ""
    subject = f"{prefix}{kind} booking {booking_display_id_for_email(booking)} — {equipment.code or equipment.name}"

    email = EmailMessage(
        subject=subject,
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=recipients,
    )
    for filename, content, mime in attachments:
        email.attach(filename, content, mime)
    email.send(fail_silently=False)

    logger.info(
        "Sent fabrication file notification (%s) for booking_id=%s to %s (%d attachment(s), %d link(s))",
        reason,
        booking_id,
        ", ".join(recipients),
        len(attachments),
        len(links),
    )
    return True


def _safe_attachment_name(filename: str, used: dict[str, int], default_ext: str = ".stl") -> str:
    fallback = f"model{default_ext}"
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", (filename or fallback).strip()) or fallback
    if not base.lower().endswith(default_ext):
        base = f"{base}{default_ext}"
    count = used.get(base, 0)
    used[base] = count + 1
    if count == 0:
        return base
    stem, dot, ext = base.rpartition(".")
    if not dot:
        return f"{base}_{count + 1}"
    return f"{stem}_{count + 1}.{ext}"


def _read_file(file_field):
    from django.core.files.storage import default_storage

    if not file_field or not file_field.name:
        return None
    storage = getattr(file_field, "storage", None) or default_storage
    resolved_name = file_field.name
    try:
        if not storage.exists(resolved_name):
            alt = resolved_name[6:] if resolved_name.startswith("media/") else f"media/{resolved_name}"
            if not storage.exists(alt):
                return None
            resolved_name = alt
        with storage.open(resolved_name, "rb") as fh:
            return fh.read()
    except Exception:
        logger.exception("Failed to read design file %s", file_field.name)
        return None


def _file_link(analysis, file_field, default_name: str) -> str:
    from .print_3d_views import presigned_design_file_url

    url = presigned_design_file_url(
        file_field,
        analysis.original_filename,
        default_name,
        expires_in=getattr(settings, "FABRICATION_EMAIL_LINK_EXPIRY_SECONDS", 7 * 24 * 3600),
    )
    if url:
        return url
    base = (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")
    booking_id = getattr(analysis, "booking_id", None)
    return f"{base}/bookings/{booking_id}" if booking_id else base


def _collect_attachments_and_links(files):
    """Attach files while the running total fits the cap; the rest become (filename, url) links."""
    cap = int(getattr(settings, "FABRICATION_EMAIL_MAX_ATTACHMENT_BYTES", 10 * 1024 * 1024))
    attachments: list[tuple[str, bytes, str]] = []
    links: list[tuple[str, str]] = []
    used_names: dict[str, int] = {}
    total = 0
    for analysis, file_field, default_name in files:
        ext = "." + default_name.rsplit(".", 1)[-1]
        name = _safe_attachment_name(
            analysis.original_filename or (file_field.name or default_name).rsplit("/", 1)[-1], used_names, ext
        )
        size = None
        try:
            size = file_field.size if file_field and file_field.name else None
        except Exception:
            size = None
        if size is not None and total + size > cap:
            links.append((name, _file_link(analysis, file_field, default_name)))
            continue
        content = _read_file(file_field)
        if content is None:
            logger.warning("Design file missing for analysis %s", analysis.pk)
            continue
        if total + len(content) > cap:
            links.append((name, _file_link(analysis, file_field, default_name)))
            continue
        total += len(content)
        attachments.append((name, content, "application/octet-stream"))
    return attachments, links


def _build_email_body(booking, files, attachments, links, *, reason: str, is_laser: bool) -> str:
    from .fabrication import fabrication_parts_summary, format_part_line

    user = booking.user
    equipment = booking.equipment
    display_id = booking_display_id_for_email(booking)
    kind = "2D laser cutting" if is_laser else "3D print"
    if reason == REASON_FILES_REPLACED_AFTER_REJECTION:
        intro = (
            f"The user has uploaded new files for this {kind} booking after the lab rejected it as not feasible. "
            "The booking is active again with the same slot. Use these files, not the earlier ones."
        )
    elif reason == REASON_FILES_UPDATED:
        intro = f"UPDATED: the design files of this {kind} booking were replaced. Use these files, not the earlier ones."
    elif reason == REASON_FILES_RESTORED:
        intro = (
            f"UPDATED: a file replacement on this {kind} booking was cancelled (extra charge not paid). "
            "The files below are the ones to use."
        )
    else:
        intro = f"A new {kind} booking has been confirmed."
    from .fbr_email import fbr_text_line

    lines = [
        intro,
        "",
        f"Booking ID: {display_id}",
    ]
    fbr_line = fbr_text_line(booking)
    if fbr_line:
        lines.append(fbr_line)
    lines += [
        f"Equipment: {equipment.name} ({equipment.code})",
        f"Booked by: {(getattr(user, 'name', None) or '').strip() or '—'}",
        f"User email: {(getattr(user, 'email', None) or '').strip() or '—'}",
    ]

    phone = getattr(user, "phone", None) or getattr(user, "mobile", None)
    if phone:
        lines.append(f"User phone: {phone}")

    lines.append(f"Total charge: ₹{booking.total_charge}")
    if not is_laser:
        lines.append(f"Estimated print time: {booking.total_time_minutes} minutes")
    lines.append(
        "Material: user brings own material" if getattr(booking, "own_material", False) else "Material: supplied by the lab"
    )

    parts = fabrication_parts_summary(booking)
    if parts:
        lines.extend(["", "Parts:"])
        for idx, part in enumerate(parts, start=1):
            lines.append(f"  {idx}. {format_part_line(part)}")
        if not is_laser:
            material = ""
            for analysis, _f, _d in files:
                m = getattr(analysis, "material", None)
                material = f"{m.name} ({m.code})" if m else (analysis.material_code_snapshot or "")
                if material:
                    break
            if material:
                lines.append(f"  Material: {material}")

    from .input_display import booking_display_values, booking_input_fields, input_summary_lines

    summary = input_summary_lines(booking_display_values(booking), booking_input_fields(booking))
    if summary:
        lines.extend(["", "Booking inputs:"])
        for label, text in summary:
            lines.append(f"  {label}: {text}")

    slots = list(booking.daily_slots.all().order_by("start_datetime"))
    if slots:
        lines.extend(["", "Booked slot(s):"])
        for slot in slots:
            start = timezone.localtime(slot.start_datetime) if slot.start_datetime else None
            end = timezone.localtime(slot.end_datetime) if slot.end_datetime else None
            slot_label = ""
            if slot.slot_master:
                slot_label = slot.slot_master.slot_name or f"Slot {slot.slot_master.slot_number}"
            if start and end:
                lines.append(
                    f"  {start.strftime('%Y-%m-%d %H:%M')} – {strftime_slot_end(end, '%H:%M')}"
                    + (f" ({slot_label})" if slot_label else "")
                )
            elif slot_label:
                lines.append(f"  {slot_label}")

    file_kind = "DXF" if is_laser else "STL"
    if attachments:
        lines.extend(["", f"{len(attachments)} {file_kind} file(s) attached to this email."])
    if links:
        lines.extend(["", f"{len(links)} {file_kind} file(s) are too large to attach. Download them here:"])
        for name, url in links:
            lines.append(f"  {name}: {url}")
    if not attachments and not links:
        lines.extend(["", f"No {file_kind} files could be attached (files may be missing from storage)."])

    lines.extend(["", "Institute Instrumentation Centre, IIT Roorkee."])
    return "\n".join(lines)

"""Email lab inbox with STL attachments when a 3D print booking is confirmed."""

import logging
import re
from typing import Optional

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import transaction
from django.utils import timezone

from iic_booking.communication.utils import booking_display_id_for_email

logger = logging.getLogger(__name__)


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


def maybe_dispatch_print_3d_stl_notification(
    booking,
    event_type: str,
    *,
    previous_status: Optional[str] = None,
    new_status: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> None:
    """Queue STL notification after booking confirmation (on transaction commit)."""
    from .models import EquipmentProfileType

    if not should_send_print_3d_stl_notification(
        event_type,
        previous_status=previous_status,
        new_status=new_status,
        metadata=metadata,
    ):
        return

    equipment = getattr(booking, "equipment", None)
    if not equipment or equipment.profile_type != EquipmentProfileType.PRINT_3D:
        return

    recipient = (getattr(equipment, "print_3d_stl_notification_email", None) or "").strip()
    if not recipient:
        return

    booking_id = booking.booking_id

    def _dispatch():
        try:
            try:
                from iic_booking.equipment.tasks import send_print_3d_stl_booking_email_task

                send_print_3d_stl_booking_email_task.delay(booking_id)
                return
            except Exception:
                logger.warning(
                    "Failed to queue print 3D STL email for booking_id=%s; sending inline",
                    booking_id,
                    exc_info=True,
                )
            try:
                send_print_3d_stl_booking_email(booking_id)
            except Exception:
                # Never break booking flow for email/SMTP issues (e.g. SMTP 535 auth errors)
                logger.exception(
                    "print_3d_stl_email: send failed for booking_id=%s (ignored)",
                    booking_id,
                )
        except Exception:
            logger.exception(
                "print_3d_stl_email: unexpected dispatch failure for booking_id=%s (ignored)",
                booking_id,
            )

    if transaction.get_connection().in_atomic_block:
        transaction.on_commit(_dispatch)
    else:
        _dispatch()


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
    """Delete STL files from storage once a PRINT_3D booking is completed."""
    from .models import EquipmentProfileType

    if not should_cleanup_print_3d_stl_files(event_type, new_status=new_status):
        return

    equipment = getattr(booking, "equipment", None)
    if not equipment or equipment.profile_type != EquipmentProfileType.PRINT_3D:
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
                    "Failed to queue print 3D STL cleanup for booking_id=%s; running inline",
                    booking_id,
                    exc_info=True,
                )
            try:
                delete_print_3d_booking_stl_files(booking_id)
            except Exception:
                logger.exception(
                    "print_3d_stl_cleanup: failed for booking_id=%s (ignored)",
                    booking_id,
                )
        except Exception:
            logger.exception(
                "print_3d_stl_cleanup: unexpected dispatch failure for booking_id=%s (ignored)",
                booking_id,
            )

    if transaction.get_connection().in_atomic_block:
        transaction.on_commit(_dispatch)
    else:
        _dispatch()


def delete_print_3d_booking_stl_files(booking_id: int) -> int:
    """
    Delete STL objects from the configured storage (S3/local) for a completed booking,
    and clear the DB FileField so the download endpoint won't leak/stale-reference.

    Returns: number of deleted objects.
    """
    from django.core.files.storage import default_storage
    from .models import Booking, BookingStatus, PrintAnalysis

    try:
        booking = Booking.objects.select_related("equipment").get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return 0
    if booking.status != BookingStatus.COMPLETED:
        return 0

    analyses = list(
        PrintAnalysis.objects.filter(booking=booking)
        .only("id", "stl_file")
        .order_by("sequence", "created_at")
    )
    if not analyses:
        return 0

    deleted = 0
    for analysis in analyses:
        file_field = getattr(analysis, "stl_file", None)
        if not file_field or not file_field.name:
            continue
        storage = getattr(file_field, "storage", None) or default_storage
        name = file_field.name
        candidate_names = [name]
        if name.startswith("media/"):
            candidate_names.append(name[len("media/"):])
        else:
            candidate_names.append(f"media/{name}")
        resolved = next((n for n in candidate_names if storage.exists(n)), None)
        if resolved:
            try:
                storage.delete(resolved)
                deleted += 1
            except Exception:
                logger.exception("Failed deleting STL object for analysis=%s name=%s", analysis.id, resolved)
        # Clear the DB field regardless (so we don't keep pointing at a deleted object)
        try:
            analysis.stl_file = ""
            analysis.save(update_fields=["stl_file"])
        except Exception:
            logger.exception("Failed clearing stl_file for analysis=%s", analysis.id)

    return deleted


def send_print_3d_stl_booking_email(booking_id: int) -> bool:
    """Send booking details and STL attachment(s) to the equipment notification inbox."""
    from .models import Booking, EquipmentProfileType, PrintAnalysis

    try:
        booking = (
            Booking.objects.select_related("user", "equipment", "print_analysis", "print_analysis_batch")
            .prefetch_related("daily_slots__slot_master")
            .get(booking_id=booking_id)
        )
    except Booking.DoesNotExist:
        logger.warning("print_3d_stl_email: booking_id=%s not found", booking_id)
        return False

    equipment = booking.equipment
    if not equipment or equipment.profile_type != EquipmentProfileType.PRINT_3D:
        return False

    recipient = (getattr(equipment, "print_3d_stl_notification_email", None) or "").strip()
    if not recipient:
        return False

    analyses = list(
        PrintAnalysis.objects.filter(booking=booking, cancelled_at__isnull=True)
        .select_related("material")
        .order_by("sequence", "created_at")
    )
    if not analyses and booking.print_analysis_id and not getattr(booking.print_analysis, "cancelled_at", None):
        analyses = [booking.print_analysis]

    attachments = _collect_stl_attachments(analyses)
    body = _build_email_body(booking, analyses, attachments)
    subject = (
        f"3D print booking {booking_display_id_for_email(booking)} — "
        f"{equipment.code or equipment.name}"
    )

    email = EmailMessage(
        subject=subject,
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[recipient],
    )
    for filename, content, mime in attachments:
        email.attach(filename, content, mime)
    email.send(fail_silently=False)

    logger.info(
        "Sent print 3D STL notification for booking_id=%s to %s (%d attachment(s))",
        booking_id,
        recipient,
        len(attachments),
    )
    return True


def _safe_attachment_name(filename: str, used: dict[str, int]) -> str:
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", (filename or "model.stl").strip()) or "model.stl"
    if not base.lower().endswith(".stl"):
        base = f"{base}.stl"
    count = used.get(base, 0)
    used[base] = count + 1
    if count == 0:
        return base
    stem, dot, ext = base.rpartition(".")
    if not dot:
        return f"{base}_{count + 1}"
    return f"{stem}_{count + 1}.{ext}"


def _collect_stl_attachments(analyses) -> list[tuple[str, bytes, str]]:
    from django.core.files.storage import default_storage

    attachments: list[tuple[str, bytes, str]] = []
    used_names: dict[str, int] = {}

    for analysis in analyses:
        file_field = getattr(analysis, "stl_file", None)
        if not file_field or not file_field.name:
            continue
        storage = getattr(file_field, "storage", None) or default_storage
        resolved_name = file_field.name
        try:
            if not storage.exists(resolved_name):
                alt = resolved_name[6:] if resolved_name.startswith("media/") else f"media/{resolved_name}"
                if storage.exists(alt):
                    resolved_name = alt
                else:
                    logger.warning("STL file missing for analysis %s: %s", analysis.pk, file_field.name)
                    continue
            with storage.open(resolved_name, "rb") as fh:
                content = fh.read()
        except Exception:
            logger.exception("Failed to read STL for analysis %s", analysis.pk)
            continue

        filename = _safe_attachment_name(analysis.original_filename or resolved_name.rsplit("/", 1)[-1], used_names)
        attachments.append((filename, content, "application/octet-stream"))

    return attachments


def _build_email_body(booking, analyses, attachments: list[tuple[str, bytes, str]]) -> str:
    user = booking.user
    equipment = booking.equipment
    display_id = booking_display_id_for_email(booking)
    lines = [
        "A new 3D print booking has been confirmed.",
        "",
        f"Booking ID: {display_id}",
        f"Equipment: {equipment.name} ({equipment.code})",
        f"Booked by: {(getattr(user, 'name', None) or '').strip() or '—'}",
        f"User email: {(getattr(user, 'email', None) or '').strip() or '—'}",
    ]

    phone = getattr(user, "phone", None) or getattr(user, "mobile", None)
    if phone:
        lines.append(f"User phone: {phone}")

    lines.extend(
        [
            f"Total charge: ₹{booking.total_charge}",
            f"Estimated print time: {booking.total_time_minutes} minutes",
            "",
            "Print parameters:",
        ]
    )

    input_values = booking.input_values or {}
    if input_values:
        for key in sorted(input_values.keys()):
            if str(key).endswith("_elements"):
                continue
            lines.append(f"  {key}: {input_values[key]}")
    else:
        lines.append("  —")

    if analyses:
        lines.extend(["", "STL file(s):"])
        for idx, analysis in enumerate(analyses, start=1):
            material = getattr(analysis, "material", None)
            material_label = ""
            if material:
                material_label = f"{material.name} ({material.code})"
            elif analysis.material_code_snapshot:
                material_label = analysis.material_code_snapshot
            weight = analysis.actual_weight_grams if analysis.actual_weight_grams is not None else analysis.weight_grams
            time_min = (
                analysis.actual_time_minutes
                if analysis.actual_time_minutes is not None
                else analysis.estimated_time_minutes
            )
            lines.append(
                f"  {idx}. {analysis.original_filename or 'model.stl'}"
                f" — material: {material_label or '—'}, weight: {weight or '—'} g, time: {time_min or '—'} min"
            )

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
                    f"  {start.strftime('%Y-%m-%d %H:%M')} – {end.strftime('%H:%M')}"
                    + (f" ({slot_label})" if slot_label else "")
                )
            elif slot_label:
                lines.append(f"  {slot_label}")

    if attachments:
        lines.extend(["", f"{len(attachments)} STL file(s) attached to this email."])
    else:
        lines.extend(["", "No STL files could be attached (files may be missing from storage)."])

    lines.extend(["", "Institute Instrumentation Centre, IIT Roorkee."])
    return "\n".join(lines)

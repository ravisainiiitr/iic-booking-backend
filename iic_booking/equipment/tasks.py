"""Celery tasks for the equipment app."""

import logging
from calendar import monthrange
from datetime import date, datetime, time, timedelta
from typing import Optional

from celery import shared_task
from django.utils import timezone
from django.db import transaction
from django.db import models
from django.db.models import OuterRef, Subquery

from .models import Booking, BookingStatus

logger = logging.getLogger(__name__)


@shared_task(name="equipment.send_manual_disruption_operational_followup")
def send_manual_disruption_operational_followup(booking_id: int) -> bool:
    """
    One-time follow-up for disruption emails (maintenance/operator-absent):
    after the initial disruption email, check equipment status after ~5 minutes and
    email the user *only if* the equipment is operational and the booking is still
    awaiting the user's choice.
    """
    from .maintenance_policy import _send_equipment_operational_email, is_equipment_operational_status

    try:
        booking = Booking.objects.select_related("user", "equipment").get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return False
    if booking.status != BookingStatus.DISRUPTION_PENDING:
        return False
    if getattr(booking, "maintenance_operational_marked_at", None):
        return False
    equipment = getattr(booking, "equipment", None)
    if not equipment or not is_equipment_operational_status(getattr(equipment, "status", None)):
        return False
    try:
        booking.maintenance_operational_marked_at = timezone.now()
        booking.save(update_fields=["maintenance_operational_marked_at"])
    except Exception:
        logger.exception(
            "Failed to mark maintenance_operational_marked_at for booking_id=%s", booking.booking_id
        )
    _send_equipment_operational_email(booking)
    return True


@shared_task(name="equipment.clear_waitlist_before_reference")
def clear_waitlist_before_reference() -> int:
    """
    Clear equipment waitlist queues 60 minutes prior to each equipment's configured
    slot window reference weekday/time.

    Returns:
        Number of waitlist entries removed.
    """
    from .waitlist import clear_waitlist_due_before_reference

    deleted = clear_waitlist_due_before_reference()
    logger.info("clear_waitlist_before_reference: deleted_entries=%d", deleted)
    return deleted


@shared_task(name="equipment.generate_external_slot_quota_snapshots")
def generate_external_slot_quota_snapshots() -> int:
    """
    Create weekly external slot quota snapshots in the 15-minute window before
    each equipment's slot-window reference instant (for next-to-next week W2).

    Returns:
        Number of newly created snapshots.
    """
    from .external_slot_quota import ExternalSlotQuotaService

    created = ExternalSlotQuotaService.generate_due_snapshots()
    logger.info("generate_external_slot_quota_snapshots: created=%d", created)
    return created


@shared_task(name="equipment.send_oic_monthly_reports")
def send_oic_monthly_reports(target_month: Optional[str] = None) -> int:
    """
    After month-end (or when invoked with target_month), generate one performance PDF per equipment
    and email each linked Officer in charge and Lab operator (deduplicated per equipment).
    When run by beat on the 1st at 00:05, target_month is None and the report is for the previous month.

    Args:
        target_month: Optional "YYYY-MM" for testing; e.g. "2026-01". If None, uses previous month.

    Returns:
        Number of individual emails successfully sent.
    """
    from iic_booking.equipment.report_exports import build_report_pdf
    from iic_booking.equipment.models import Equipment, EquipmentManager, EquipmentOperator
    from iic_booking.communication.service import CommunicationService
    from iic_booking.communication.models import CommunicationTemplate
    import tempfile
    import os

    today = timezone.localdate()
    if target_month:
        try:
            year, month = map(int, target_month.split("-"))
            start = date(year, month, 1)
            _, last = monthrange(year, month)
            end = date(year, month, last)
        except (ValueError, AttributeError):
            if today.month == 1:
                start = date(today.year - 1, 12, 1)
                end = date(today.year - 1, 12, 31)
            else:
                start = date(today.year, today.month - 1, 1)
                _, last = monthrange(start.year, start.month)
                end = start.replace(day=last)
    else:
        if today.month == 1:
            start = date(today.year - 1, 12, 1)
            end = date(today.year - 1, 12, 31)
        else:
            start = today.replace(month=today.month - 1, day=1)
            _, last = monthrange(start.year, start.month)
            end = start.replace(day=last)

    date_from = start.isoformat()
    date_to = end.isoformat()

    equipment_ids_with_staff = set(
        EquipmentManager.objects.values_list("equipment_id", flat=True)
    ) | set(EquipmentOperator.objects.values_list("equipment_id", flat=True))

    template = None
    try:
        template = CommunicationTemplate.objects.get(
            code="oic_monthly_report",
            communication_type=CommunicationTemplate.CommunicationType.EMAIL,
            is_active=True,
        )
    except CommunicationTemplate.DoesNotExist:
        logger.warning("Template 'oic_monthly_report' not found; sending report with default subject/body")

    sent = 0
    for eid in sorted(equipment_ids_with_staff):
        managers = list(
            EquipmentManager.objects.filter(equipment_id=eid).select_related("manager")
        )
        operators = list(
            EquipmentOperator.objects.filter(equipment_id=eid).select_related("operator")
        )
        recipient_users = []
        seen_uid = set()
        for em in managers:
            u = em.manager
            if u and u.id not in seen_uid and getattr(u, "email", None):
                seen_uid.add(u.id)
                recipient_users.append(u)
        for eo in operators:
            u = eo.operator
            if u and u.id not in seen_uid and getattr(u, "email", None):
                seen_uid.add(u.id)
                recipient_users.append(u)
        if not recipient_users:
            continue

        pdf_bytes = build_report_pdf(
            date_from=date_from,
            date_to=date_to,
            equipment_ids=[eid],
        )
        eq = Equipment.objects.filter(equipment_id=eid).only("code", "name").first()
        eq_code = (eq.code if eq else str(eid)) or str(eid)
        eq_name = (eq.name if eq else "") or ""

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            path = f.name
        try:
            for user in recipient_users:
                attach_name = f"performance-report-{eq_code}-{date_from}-to-{date_to}.pdf"
                try:
                    if template:
                        context = {
                            "date_from": date_from,
                            "date_to": date_to,
                            "equipment_codes": eq_code,
                            "equipment_name": eq_name,
                        }
                        CommunicationService.send_email_with_attachments(
                            user,
                            template="oic_monthly_report",
                            template_context=context,
                            attachment_paths=[(path, attach_name)],
                        )
                    else:
                        from django.core.mail import EmailMessage
                        from django.conf import settings

                        email = EmailMessage(
                            subject=f"Equipment performance report — {eq_code} ({date_from} to {date_to})",
                            body=(
                                f"Please find attached the monthly equipment performance report for {eq_name} ({eq_code}) "
                                f"for the period {date_from} to {date_to}. "
                                "Institute Instrumentation Centre, IIT Roorkee."
                            ),
                            from_email=settings.DEFAULT_FROM_EMAIL,
                            to=[user.email],
                        )
                        with open(path, "rb") as fp:
                            email.attach(attach_name, fp.read(), "application/pdf")
                        email.send()
                    sent += 1
                except Exception as e:
                    logger.exception(
                        "Failed to send performance report for equipment_id=%s to %s: %s",
                        eid,
                        getattr(user, "email", ""),
                        e,
                    )
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    logger.info("send_oic_monthly_reports: period=%s to %s, emails_sent=%d", date_from, date_to, sent)
    return sent


@shared_task(name="equipment.archive_expired_samples")
def archive_expired_samples() -> int:
    """
    Daily task: auto-mark sample lifecycle as ARCHIVED after retention period.

    Rule:
    - If the latest sample trace status is COMPLETED (Analyzed)
      and its timestamp is older than (now - retention_days),
      and the booking is not already RETURNED / ARCHIVED / DISPOSED,
      then create a new BookingSampleTrace event with status ARCHIVED.

    Returns:
        Number of bookings archived (events created).
    """
    from .models import BookingBufferConfig, BookingSampleTrace, SampleTraceStatus

    cfg = BookingBufferConfig.objects.first()
    if not cfg or not getattr(cfg, "auto_archive_enabled", True):
        logger.info("archive_expired_samples: disabled (no config or auto_archive_enabled=False)")
        return 0
    retention_days = int(getattr(cfg, "sample_retention_days", 0) or 0)
    if retention_days <= 0:
        logger.info("archive_expired_samples: retention_days<=0, skipping")
        return 0

    now = timezone.now()
    cutoff = now - timedelta(days=retention_days)

    latest_status_sq = Subquery(
        BookingSampleTrace.objects.filter(booking_id=OuterRef("booking_id"))
        .order_by("-created_at")
        .values("status")[:1]
    )
    latest_created_sq = Subquery(
        BookingSampleTrace.objects.filter(booking_id=OuterRef("booking_id"))
        .order_by("-created_at")
        .values("created_at")[:1]
    )

    candidates = (
        Booking.objects.filter(status=BookingStatus.COMPLETED)
        .annotate(_latest_sample_status=latest_status_sq, _latest_sample_time=latest_created_sq)
        .filter(_latest_sample_status=SampleTraceStatus.COMPLETED, _latest_sample_time__lt=cutoff)
        .select_related("user", "equipment")
    )

    archived = 0
    for b in candidates.iterator():
        # Safety: don't archive if something changed since annotate (race) or there are newer events.
        latest = (
            BookingSampleTrace.objects.filter(booking_id=b.booking_id)
            .order_by("-created_at")
            .values_list("status", flat=True)
            .first()
        )
        if latest != SampleTraceStatus.COMPLETED:
            continue
        with transaction.atomic():
            BookingSampleTrace.objects.create(
                booking_id=b.booking_id,
                status=SampleTraceStatus.ARCHIVED,
                reason="Auto-archived after retention period.",
                created_by=None,
            )
        archived += 1

    logger.info(
        "archive_expired_samples: retention_days=%d, cutoff=%s, archived=%d",
        retention_days,
        cutoff.isoformat(),
        archived,
    )
    return archived


@shared_task(name="equipment.send_booking_reminders")
def send_booking_reminders(target_date: Optional[str] = None) -> int:
    """
    Send reminder emails for all BOOKED bookings whose slot date is the given date.
    Runs daily at 8:30 AM; when called by beat, target_date is not set so today (in project timezone) is used.

    Args:
        target_date: Optional date string YYYY-MM-DD for testing; defaults to today in project timezone.

    Returns:
        Number of reminders sent.
    """
    from .booking_reminders import send_reminder_for_booking

    if target_date:
        try:
            day = date.fromisoformat(target_date)
        except ValueError:
            logger.warning("Invalid target_date '%s', using today", target_date)
            day = timezone.localdate()
    else:
        day = timezone.localdate()

    bookings = (
        Booking.objects.filter(
            status=BookingStatus.BOOKED,
            daily_slots__date=day,
        )
        .select_related("user", "equipment")
        .distinct()
    )
    booking_list = list(bookings)
    sent = 0
    for booking in booking_list:
        try:
            send_reminder_for_booking(booking)
            sent += 1
        except Exception as e:
            logger.exception(
                "Failed to send booking reminder for booking_id=%s: %s",
                booking.booking_id,
                e,
            )

    logger.info(
        "send_booking_reminders: date=%s, found=%d, sent=%d",
        day,
        len(booking_list),
        sent,
    )
    return sent


@shared_task(name="equipment.send_sample_submission_deadline_reminders")
def send_sample_submission_deadline_reminders() -> int:
    """
    Poll BOOKED bookings and send email + in-app notification once when the
    sample submission deadline is within 12 hours (deadline = slot start − lead hours).
    Intended to run every few minutes via Celery beat.
    """
    from .sample_submission_deadline_reminders import (
        iter_bookings_for_sample_submission_deadline_reminders,
        send_sample_submission_deadline_reminder,
    )

    sent = 0
    booking_list = list(iter_bookings_for_sample_submission_deadline_reminders())
    for booking in booking_list:
        try:
            if send_sample_submission_deadline_reminder(booking):
                sent += 1
        except Exception:
            logger.exception(
                "Failed sample submission deadline reminder for booking_id=%s",
                booking.booking_id,
            )
    logger.info(
        "send_sample_submission_deadline_reminders: candidates=%d sent=%d",
        len(booking_list),
        sent,
    )
    return sent


@shared_task(name="equipment.check_booking_not_utilized")
def check_booking_not_utilized() -> int:
    """
    Daily at 20:00 (Asia/Kolkata): on **working days only** (excludes Saturday, Sunday, and institute
    holidays per ``Holiday.is_holiday``), find BOOKED bookings where **the latest ``DailySlot.end_datetime``
    among BOOKED slots** is at least **24 hours** before *now* (i.e. ``now >= that_end + 24h``), sample
    lifecycle is **empty or only SAMPLE_SENT** (excludes forwarded/accepted/processing — those follow
    operator disruption / unavailable rules), and all slots are still BOOKED; mark Booking Not Utilized
    (no refund) and email user + faculty wallet owner.
    """
    from django.db.models import Count, Exists, F, Max, OuterRef, Q

    from .booking_not_utilized_service import apply_booking_not_utilized
    from .models import (
        Booking,
        BookingSampleTrace,
        BookingStatus,
        Holiday,
        SampleTraceStatus,
        SlotStatus,
    )

    today = timezone.localdate()
    is_off, reason = Holiday.is_holiday(today)
    if is_off:
        logger.info("check_booking_not_utilized: skip (not a working day: %s)", reason)
        return 0

    # 24h is measured from max(end_datetime) of BOOKED slots only — not booking created_at or slot.date.
    now = timezone.now()
    cutoff = now - timedelta(hours=24)

    # Not utilized only when no lifecycle progress past "sample sent" (see sample_trace_policy).
    bad_trace = BookingSampleTrace.objects.filter(booking_id=OuterRef("pk")).exclude(
        status=SampleTraceStatus.SAMPLE_SENT
    )

    qs = (
        Booking.objects.filter(status=BookingStatus.BOOKED)
        .annotate(
            latest_booked_slot_end=Max(
                "daily_slots__end_datetime",
                filter=Q(daily_slots__status=SlotStatus.BOOKED),
            ),
            n_slots=Count("daily_slots", distinct=True),
            n_booked=Count(
                "daily_slots",
                filter=Q(daily_slots__status=SlotStatus.BOOKED),
                distinct=True,
            ),
            has_bad_trace=Exists(bad_trace),
        )
        .filter(
            latest_booked_slot_end__isnull=False,
            latest_booked_slot_end__lte=cutoff,
            n_slots__gt=0,
            n_booked=F("n_slots"),
            has_bad_trace=False,
        )
        .select_related("user", "equipment")
    )

    marked = 0
    for booking in qs.iterator(chunk_size=50):
        try:
            if apply_booking_not_utilized(
                booking,
                actor=None,
                automated=True,
                hours_after_last_slot_end=24,
            ):
                marked += 1
        except Exception:
            logger.exception(
                "check_booking_not_utilized: failed booking_id=%s",
                getattr(booking, "booking_id", None),
            )

    logger.info("check_booking_not_utilized: marked=%d", marked)
    return marked


@shared_task(name="equipment.auto_mark_operator_unavailable_after_booking_end")
def auto_mark_operator_unavailable_after_booking_end() -> int:
    """
    Daily at 20:30 IST. For each equipment with operator_unavailable_after_booking_end_hours > 0:
    BOOKED/PENDING bookings whose last slot ended before now minus that window, with sample trace beyond
    SAMPLE_SENT but not completed (COMPLETED/RETURNED/ARCHIVED/DISPOSED/NOT_UTILIZED/OP_UNAVAILABLE),
    may be marked Operator Unavailable with full refund.

    Skips when the **latest** trace is forwarded/accepted/processing or awaiting user (held/rejected):
    those use ``auto_mark_operator_absent_disruption_after_booking_end`` or manual disruption instead.

    Bookings with only SAMPLE_SENT or no trace are handled by ``check_booking_not_utilized`` (20:00 IST) or staff.
    """
    from .models import Booking, BookingSampleTrace, BookingStatus, SampleTraceStatus
    from .operator_unavailable import apply_operator_unavailable_booking
    from .sample_trace_policy import (
        OPERATOR_UNAVAILABLE_AUTO_REFUND_EXCLUDED_LATEST_STATUSES,
        latest_sample_trace,
    )

    now = timezone.now()
    marked = 0
    bookings = (
        Booking.objects.filter(status__in=[BookingStatus.PENDING, BookingStatus.BOOKED])
        .select_related("user", "equipment")
        .prefetch_related("daily_slots")
    )
    for booking in bookings:
        equipment = booking.equipment
        hours = int(getattr(equipment, "operator_unavailable_after_booking_end_hours", 24) or 0)
        if hours <= 0:
            continue
        slots = list(booking.daily_slots.all())
        if not slots:
            continue
        end_times = [s.end_datetime for s in slots if getattr(s, "end_datetime", None) is not None]
        if not end_times:
            continue
        end_dt = max(end_times)
        if timezone.is_naive(end_dt):
            end_dt = timezone.make_aware(end_dt)
        hold_until = getattr(booking, "operator_absent_hold_until", None)
        if hold_until is not None:
            if timezone.is_naive(hold_until):
                hold_until = timezone.make_aware(hold_until)
            end_dt = max(end_dt, hold_until)
        if now < end_dt + timedelta(hours=hours):
            continue

        non_sample_sent_exists = BookingSampleTrace.objects.filter(booking_id=booking.booking_id).exclude(
            status=SampleTraceStatus.SAMPLE_SENT
        ).exists()
        if not non_sample_sent_exists:
            continue

        latest_ev = latest_sample_trace(booking.booking_id)
        latest_st = getattr(latest_ev, "status", None)
        if latest_st in OPERATOR_UNAVAILABLE_AUTO_REFUND_EXCLUDED_LATEST_STATUSES:
            continue

        advanced = BookingSampleTrace.objects.filter(
            booking_id=booking.booking_id,
            status__in=[
                SampleTraceStatus.COMPLETED,
                SampleTraceStatus.RETURNED,
                SampleTraceStatus.ARCHIVED,
                SampleTraceStatus.DISPOSED,
                SampleTraceStatus.NOT_UTILIZED,
                SampleTraceStatus.OP_UNAVAILABLE,
            ],
        ).exists()
        if advanced:
            continue

        try:
            apply_operator_unavailable_booking(
                booking,
                notes="Automatically marked after booking end (scheduled job).",
                actor=None,
            )
        except ValueError as e:
            logger.warning(
                "auto_mark_operator_unavailable_after_booking_end: skip booking_id=%s: %s",
                booking.booking_id,
                e,
            )
            continue
        marked += 1

    logger.info("auto_mark_operator_unavailable_after_booking_end: marked=%d", marked)
    return marked


@shared_task(name="equipment.auto_mark_operator_absent_disruption_after_booking_end")
def auto_mark_operator_absent_disruption_after_booking_end() -> int:
    """
    If a booking remains stuck in "Forwarded to Lab", "Sample Accepted", or "Processing" long after
    slot end, treat it as an Operator Absent disruption and trigger the standard disruption flow
    (refund vs reschedule choice).

    Per-equipment configuration:
    - equipment.operator_absent_disruption_after_booking_end_hours (default: 48; 0 disables)
    """
    from .models import Booking, BookingSampleTrace, BookingStatus, SampleTraceStatus
    from .maintenance_policy import apply_operator_absent_disruption_for_booking
    from .sample_trace_policy import SAMPLE_TRACE_IN_LAB_OR_ANALYSIS_STATUSES

    now = timezone.now()
    marked = 0
    bookings = (
        Booking.objects.filter(status__in=[BookingStatus.PENDING, BookingStatus.BOOKED])
        .select_related("user", "equipment")
        .prefetch_related("daily_slots")
    )
    for booking in bookings:
        equipment = booking.equipment
        hours = int(getattr(equipment, "operator_absent_disruption_after_booking_end_hours", 48) or 0)
        if hours <= 0:
            continue

        slots = list(booking.daily_slots.all())
        if not slots:
            continue
        end_times = [s.end_datetime for s in slots if getattr(s, "end_datetime", None) is not None]
        if not end_times:
            continue
        end_dt = max(end_times)
        if timezone.is_naive(end_dt):
            end_dt = timezone.make_aware(end_dt)
        hold_until = getattr(booking, "operator_absent_hold_until", None)
        if hold_until is not None:
            if timezone.is_naive(hold_until):
                hold_until = timezone.make_aware(hold_until)
            end_dt = max(end_dt, hold_until)

        if now < end_dt + timedelta(hours=hours):
            continue

        latest = (
            BookingSampleTrace.objects.filter(booking_id=booking.booking_id)
            .order_by("-created_at")
            .values_list("status", "created_at")
            .first()
        )
        if not latest:
            continue
        latest_status, latest_at = latest
        if latest_status not in SAMPLE_TRACE_IN_LAB_OR_ANALYSIS_STATUSES:
            continue
        if latest_at and timezone.is_naive(latest_at):
            latest_at = timezone.make_aware(latest_at)
        if latest_at and now < latest_at + timedelta(hours=hours):
            continue

        try:
            tag = "[Auto disruption: operator absent (stuck sample status)]"
            booking.notes = f"{(booking.notes or '').strip()}\n{tag}".strip()
            booking.save(update_fields=["notes"])
        except Exception:
            logger.exception(
                "auto_mark_operator_absent_disruption_after_booking_end: failed to tag notes booking_id=%s",
                booking.booking_id,
            )

        try:
            apply_operator_absent_disruption_for_booking(booking, triggered_at=now)
            marked += 1
        except Exception as e:
            logger.exception(
                "auto_mark_operator_absent_disruption_after_booking_end: skip booking_id=%s: %s",
                booking.booking_id,
                e,
            )
            continue

    logger.info("auto_mark_operator_absent_disruption_after_booking_end: marked=%d", marked)
    return marked


@shared_task(name="equipment.process_maintenance_booking_deadlines")
def process_maintenance_booking_deadlines() -> int:
    """
    Auto-cancel BOOKED/PENDING bookings that are past maintenance_decision_deadline_at
    under the equipment maintenance disruption policy (full refund + email).
    Schedule via Celery Beat (e.g. every 5 minutes).
    """
    from .maintenance_policy import auto_cancel_expired_maintenance_bookings

    n = auto_cancel_expired_maintenance_bookings()
    logger.info("process_maintenance_booking_deadlines: auto_cancelled=%s", n)
    return n


@shared_task(name="equipment.daily_under_maintenance_sweep")
def daily_under_maintenance_sweep() -> dict:
    """
    Daily maintenance sweep (schedule at 08:15 local time):
    - For each equipment still under maintenance, mark today's AVAILABLE slots as UNDER_MAINTENANCE.
    - Notify today's affected users (in-progress/future slots) per maintenance disruption policy.
    """
    from .maintenance_policy import daily_under_maintenance_sweep_for_today

    result = daily_under_maintenance_sweep_for_today()
    logger.info(
        "daily_under_maintenance_sweep: equipments=%s, slots_marked=%s, bookings_notified=%s",
        result.get("equipments_checked", 0),
        result.get("slots_marked_under_maintenance", 0),
        result.get("bookings_notified", 0),
    )
    return result


@shared_task(name="equipment.daily_operator_on_leave_sweep")
def daily_operator_on_leave_sweep() -> dict:
    """
    Daily operator-on-leave sweep (schedule at ~08:20 local time):
    - For each equipment that has an active EquipmentOperatorCoverage in OPERATOR_ON_LEAVE mode,
      apply the operator-absent disruption policy to affected bookings for today (in-progress/future).

    This makes the disruption policy apply day-by-day during the leave period.
    """
    from django.db import close_old_connections

    from .maintenance_policy import apply_operator_absent_disruption_for_booking, get_affected_booking_ids_for_equipment_today
    from .models import Equipment, EquipmentOperatorCoverage

    close_old_connections()
    try:
        now_ts = timezone.now()
        today = timezone.localdate(now_ts)
        # Active coverages whose window intersects today.
        day_start = timezone.make_aware(datetime.combine(today, time.min))
        day_end = timezone.make_aware(datetime.combine(today, time.max))

        coverages = (
            EquipmentOperatorCoverage.objects.filter(
                mode=EquipmentOperatorCoverage.Mode.OPERATOR_ON_LEAVE,
                starts_at__lte=day_end,
                ends_at__gte=day_start,
            )
            .filter(models.Q(ended_early_at__isnull=True) | models.Q(ended_early_at__gt=now_ts))
            .values_list("equipment_id", flat=True)
            .distinct()
        )
        equipment_ids = list(coverages)
        equipments = Equipment.objects.filter(equipment_id__in=equipment_ids)

        bookings_marked = 0
        equipments_checked = 0
        for eq in equipments:
            equipments_checked += 1
            ids = get_affected_booking_ids_for_equipment_today(eq)
            if not ids:
                continue
            qs = Booking.objects.filter(booking_id__in=list(ids)).select_related("equipment", "user").prefetch_related("daily_slots")
            for b in qs:
                before = b.status
                apply_operator_absent_disruption_for_booking(b, triggered_at=now_ts)
                # If it transitioned, count it.
                if before != b.status and b.status == BookingStatus.DISRUPTION_PENDING:
                    bookings_marked += 1

        return {"equipments_checked": equipments_checked, "bookings_marked_disruption": bookings_marked}
    finally:
        close_old_connections()


@shared_task(name="equipment.notify_user_results_available")
def notify_user_results_available_task(booking_id: int) -> None:
    """
    After result files are listed for a booking, record event + send push/email without blocking GET /results/.
    """
    from django.db import close_old_connections

    close_old_connections()
    try:
        from iic_booking.equipment.api_views import _notify_user_results_available_by_id

        _notify_user_results_available_by_id(booking_id)
    except Exception:
        logger.exception(
            "notify_user_results_available_task failed for booking_id=%s",
            booking_id,
        )
    finally:
        close_old_connections()


@shared_task(name="equipment.send_unsuccessful_booking_waitlist_email")
def send_unsuccessful_booking_waitlist_email_task(
    user_id: int, equipment_pk: int, position: int, failure_reason: str = ""
) -> None:
    """
    Waitlist confirmation email after a failed booking attempt — must not block POST /equipments/<id>/book/.
    """
    from django.contrib.auth import get_user_model
    from django.db import close_old_connections

    from .models import Equipment
    from .waitlist import send_unsuccessful_booking_waitlist_email

    close_old_connections()
    try:
        User = get_user_model()
        user = User.objects.get(pk=user_id)
        equipment = Equipment.objects.get(pk=equipment_pk)
        send_unsuccessful_booking_waitlist_email(
            user, equipment, position, failure_reason=failure_reason or ""
        )
    except Exception:
        logger.exception(
            "send_unsuccessful_booking_waitlist_email_task failed user_id=%s equipment_pk=%s",
            user_id,
            equipment_pk,
        )
    finally:
        close_old_connections()


@shared_task(name="equipment.send_booking_event_notifications")
def send_booking_event_notifications_task(event_id: int) -> None:
    """
    Send email/push for a booking event after the HTTP transaction has committed.
    Keeps POST /equipments/<id>/book/ fast by not blocking on SMTP or push providers.
    """
    from .models import BookingEvent
    from .booking_events import send_booking_event_notification

    try:
        event = BookingEvent.objects.select_related(
            "booking",
            "booking__user",
            "booking__equipment",
            "created_by",
        ).get(event_id=event_id)
    except BookingEvent.DoesNotExist:
        logger.warning("send_booking_event_notifications_task: event_id=%s not found", event_id)
        return
    try:
        send_booking_event_notification(event)
    except Exception:
        logger.exception(
            "send_booking_event_notifications_task: failed for event_id=%s",
            event_id,
        )
        return
    BookingEvent.objects.filter(event_id=event_id).update(notification_sent=True)


def run_print_analysis_impl(analysis_id: str) -> None:
    from django.db import close_old_connections

    from .models import PrintAnalysis, PrintAnalysisMethod, PrintAnalysisStatus
    from .print_3d_service import analyze_stl_file

    close_old_connections()
    try:
        analysis = PrintAnalysis.objects.select_related("equipment", "material").get(pk=analysis_id)
    except PrintAnalysis.DoesNotExist:
        return

    analysis_pk = analysis.pk
    analysis.status = PrintAnalysisStatus.PROCESSING
    analysis.save(update_fields=["status", "updated_at"])

    try:
        with analysis.stl_file.open("rb") as fh:
            stl_bytes = fh.read()
        density = float(analysis.material.density_g_per_cm3) if analysis.material else 1.24
        slicer_settings = analysis.slicer_settings or {}
        equipment = analysis.equipment
        bed_size_mm = {
            "x": float(getattr(equipment, "print_bed_width_mm", None) or 220),
            "y": float(getattr(equipment, "print_bed_depth_mm", None) or 220),
            "z": float(getattr(equipment, "print_bed_height_mm", None) or 250),
        }
    except Exception as exc:
        logger.exception("Failed to load STL for print analysis %s", analysis_id)
        close_old_connections()
        analysis = PrintAnalysis.objects.get(pk=analysis_pk)
        analysis.status = PrintAnalysisStatus.FAILED
        analysis.error_message = str(exc)
        analysis.save()
        return

    # Release DB connections before CPU-heavy STL parsing / slicing.
    close_old_connections()

    try:
        estimate = analyze_stl_file(
            stl_bytes,
            density_g_per_cm3=density,
            slicer_settings=slicer_settings,
            bed_size_mm=bed_size_mm,
        )
        result_status = PrintAnalysisStatus.COMPLETED
        result_error = ""
        result_weight = estimate.weight_grams
        result_volume = estimate.volume_cm3
        result_time = estimate.estimated_time_minutes
        result_bbox = estimate.bounding_box
        result_warnings = estimate.warnings
        result_method = (
            PrintAnalysisMethod.CURAENGINE
            if estimate.analysis_method == "CURAENGINE"
            else PrintAnalysisMethod.HEURISTIC
        )
    except Exception as exc:
        logger.exception("Print analysis failed for %s", analysis_id)
        result_status = PrintAnalysisStatus.FAILED
        result_error = str(exc)
        result_weight = None
        result_volume = None
        result_time = None
        result_bbox = None
        result_warnings = []
        result_method = None

    close_old_connections()
    analysis = PrintAnalysis.objects.get(pk=analysis_pk)
    analysis.status = result_status
    analysis.error_message = result_error
    if result_status == PrintAnalysisStatus.COMPLETED:
        analysis.weight_grams = result_weight
        analysis.volume_cm3 = result_volume
        analysis.estimated_time_minutes = result_time
        bbox = dict(result_bbox or {})
        if estimate.volume_mm3:
            bbox["_volume_mm3"] = estimate.volume_mm3
        if estimate.surface_area_mm2:
            bbox["_surface_area_mm2"] = estimate.surface_area_mm2
        analysis.bounding_box = bbox
        analysis.warnings = result_warnings
        analysis.analysis_method = result_method
    analysis.save()

    if analysis.batch_id:
        from .print_3d_views import _refresh_batch_status

        try:
            batch = analysis.batch
            if batch:
                _refresh_batch_status(batch)
        except Exception:
            logger.exception("Failed to refresh batch status for analysis %s", analysis_id)


@shared_task(name="equipment.send_print_3d_stl_booking_email")
def send_print_3d_stl_booking_email_task(booking_id: int) -> bool:
    """Email configured lab inbox with STL file(s) and booking details."""
    from .print_3d_notifications import send_print_3d_stl_booking_email

    return send_print_3d_stl_booking_email(booking_id)


@shared_task(name="equipment.delete_print_3d_booking_stl_files")
def delete_print_3d_booking_stl_files_task(booking_id: int) -> int:
    """Delete STL files from storage for a completed 3D print booking."""
    from .print_3d_notifications import delete_print_3d_booking_stl_files

    return delete_print_3d_booking_stl_files(booking_id)


@shared_task(name="equipment.run_print_analysis", soft_time_limit=600, time_limit=660)
def run_print_analysis_task(analysis_id: str) -> None:
    run_print_analysis_impl(analysis_id)


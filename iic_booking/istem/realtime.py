"""Real-time I-STEM sync (12 Nov 2026): send each event to I-STEM within seconds instead of waiting for the 5-minute
sync, which stays as the safety net.

* A booking on an instrument whose charge profile requires an I-STEM FBR is saved (paid / booked, or marked
  Completed by the lab operator): the sync runs at once, so Create Booking + Confirm (allocate) follow the payment and
  FBR Execute follows completion.
* An external user's account becomes active (registration finished): their I-STEM user is created, or their existing
  one linked, straight away (``ISTEM_USER_CREATION_ENABLED``). IIT Roorkee internal users are still linked only when
  their first I-STEM booking is sent.

* 14 Nov 2026: a cancellation, or a slot moved (reschedule), of a booking already on I-STEM also starts the sync at
  once, so Cancel Booking and the move follow within seconds.
* Peak booking window (slot opening, ``equipment.peak_window``): nothing is sent to I-STEM while it is active, so
  the booking rush is never slowed by I-STEM calls. One run is scheduled for the moment the window ends, and the
  scheduled sync also waits for it.

Events in quick succession share one run: the first schedules ``istem.run_sync`` a couple of seconds after the commit
and later events inside that window are picked up by it. Every remote call still goes through the outbox (dedupe
keys and job leases), so a run triggered here and the scheduled sync can never send the same request twice.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.core.cache import cache
from django.db import transaction
from django.db.models.signals import post_save

from . import conf

logger = logging.getLogger(__name__)

KICK_KEY = "istem:realtime-kick"
KICK_DELAY_SECONDS = 2
AFTER_PEAK_KEY = "istem:realtime-after-peak"
AFTER_PEAK_DELAY_SECONDS = 15


def realtime_enabled() -> bool:
    return conf.automatic_fbr_enabled() and not conf.missing_live_settings()


def peak_ends_at(at=None):
    """When the active peak booking window ends, or None outside it (and when the window cannot be read)."""
    try:
        from iic_booking.equipment.peak_window import compute_peak_state

        state = compute_peak_state(at)
    except Exception:  # noqa: BLE001 - never let the peak setting block I-STEM
        logger.warning("istem realtime: peak window not readable", exc_info=True)
        return None
    if not state.get("peak_window_active"):
        return None
    current = state.get("_current")
    return getattr(current, "ends_at", None)


def _schedule_after_peak(ends_at, reason: str) -> None:
    """One run when the peak window ends, however many events arrive during it."""
    from django.utils import timezone

    ttl = max(60, int((ends_at - timezone.now()).total_seconds()) + AFTER_PEAK_DELAY_SECONDS + 60)
    if not cache.add(AFTER_PEAK_KEY, ends_at.isoformat(), ttl):
        return
    from .tasks import run_istem_sync

    try:
        run_istem_sync.apply_async(eta=ends_at + timedelta(seconds=AFTER_PEAK_DELAY_SECONDS))
    except Exception:  # broker down: the scheduled sync sends it after the window
        cache.delete(AFTER_PEAK_KEY)
        logger.warning("istem realtime: could not schedule the after-peak sync (%s)", reason, exc_info=True)


def kick(reason: str) -> bool:
    """Schedule one sync run shortly after the current transaction commits (or when the peak booking window ends).
    True when a run was scheduled."""
    if not realtime_enabled():
        return False

    def _schedule():
        ends_at = peak_ends_at()
        if ends_at is not None:
            _schedule_after_peak(ends_at, reason)
            return
        if not cache.add(KICK_KEY, 1, KICK_DELAY_SECONDS):
            return  # a run is already due within the window and will pick this event up
        from .tasks import run_istem_sync

        try:
            run_istem_sync.apply_async(countdown=KICK_DELAY_SECONDS)
        except Exception:  # broker down: the scheduled 5-minute sync sends it instead
            cache.delete(KICK_KEY)
            logger.warning("istem realtime: could not schedule a sync (%s)", reason, exc_info=True)

    transaction.on_commit(_schedule)
    return True


def _fbr_required(booking) -> bool:
    from iic_booking.equipment.models import ChargeProfile

    if not booking.charge_profile_id:
        return False
    return ChargeProfile.objects.filter(pk=booking.charge_profile_id, require_istem_fbr=True).exists()


CLOSED_STATUSES = ("CANCELLED", "REFUNDED")


def _on_istem(booking_id) -> bool:
    """The booking already reached I-STEM (it has an I-STEM booking to cancel or move)."""
    from .models import IstemBookingLink

    return IstemBookingLink.objects.filter(booking_id=booking_id).exclude(request_id__isnull=True).exclude(request_id="").exists()


def on_booking_saved(sender, instance, created, **kwargs):
    from .eligibility import ELIGIBLE_STATUSES

    if kwargs.get("raw"):
        return
    try:
        if instance.status in ELIGIBLE_STATUSES:
            if realtime_enabled() and _fbr_required(instance):
                kick(f"booking {instance.pk} {instance.status}")
        elif instance.status in CLOSED_STATUSES and not created:
            if realtime_enabled() and _on_istem(instance.pk):
                kick(f"booking {instance.pk} {instance.status}")
    except Exception:  # never let I-STEM break saving a booking
        logger.warning("istem realtime: booking %s not scheduled", instance.pk, exc_info=True)


MOVE_OR_CLOSE_EVENTS = ("RESCHEDULED", "CANCELLED", "REFUNDED")


def on_booking_event_saved(sender, instance, created, **kwargs):
    """A booking already on I-STEM was rescheduled or cancelled (every path records a booking event, including those
    that move slots with a bulk update and so never save a slot)."""
    if kwargs.get("raw") or not created or instance.event_type not in MOVE_OR_CLOSE_EVENTS:
        return
    try:
        if realtime_enabled() and _on_istem(instance.booking_id):
            kick(f"booking {instance.booking_id} {instance.event_type.lower()}")
    except Exception:  # never let I-STEM break a reschedule or cancellation
        logger.warning("istem realtime: booking event %s not scheduled", instance.pk, exc_info=True)


def on_user_saved(sender, instance, created, **kwargs):
    from iic_booking.users.models.user_type import UserType

    if kwargs.get("raw") or not instance.is_active:
        return
    if instance.user_type not in UserType.get_external_user_codes():
        return
    try:
        if not (realtime_enabled() and conf.user_creation_enabled()):
            return
        from .models import IstemUserLink

        if IstemUserLink.objects.filter(user=instance).exists():
            return  # already linked, waiting or failed: the booking flow and the scheduled sync take it from there
        from .outbox import enqueue_user_link

        enqueue_user_link(instance, trigger="registered")
        kick(f"user {instance.pk} registered")
    except Exception:  # never let I-STEM break registration
        logger.warning("istem realtime: user %s not queued", instance.pk, exc_info=True)


def connect() -> None:
    from django.contrib.auth import get_user_model

    from iic_booking.equipment.models import Booking, BookingEvent

    post_save.connect(on_booking_saved, sender=Booking, dispatch_uid="istem_realtime_booking")
    post_save.connect(on_booking_event_saved, sender=BookingEvent, dispatch_uid="istem_realtime_booking_event")
    post_save.connect(on_user_saved, sender=get_user_model(), dispatch_uid="istem_realtime_user")

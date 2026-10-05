"""Per-department Remote Analysis switch for reservations that are not tied to a booking.

Booking-backed reservations are governed by the booked equipment's department (``BookingAnalysisEligibilityService``).
A reservation without a booking is checked against the department that owns the analysis workstation it gets: a
workstation whose department refuses the user is never allocated to it. Workstations without a department are not
restricted.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

OFF_REASON = (
    "Remote Analysis is switched off (or open to test accounts only) for the departments that own the analysis "
    "workstations"
)


def refused_workstation_department_ids(user, *, started_at=None) -> set[int]:
    """Departments whose workstations may not take this user's booking-less reservation (created at ``started_at``)."""
    try:
        from iic_booking.department_modules import access
        from iic_booking.department_modules.constants import ModuleKey

        return access.refused_department_ids(ModuleKey.REMOTE_ANALYSIS, user, started_at=started_at)
    except Exception:  # noqa: BLE001 - the switches only narrow; never take allocation down
        logger.exception("remote analysis department switch lookup failed")
        return set()


def new_reservation_block_reason(user) -> str:
    """Why a new booking-less reservation cannot get any workstation because of the switches ("" if it can)."""
    from iic_booking.remote_analysis.models import AnalysisWorkstation

    refused = refused_workstation_department_ids(user)
    if not refused:
        return ""
    enabled = AnalysisWorkstation.objects.filter(enabled=True)
    if not enabled.filter(department_id__in=refused).exists():
        return ""
    if enabled.exclude(department_id__in=refused).exists():
        return ""
    return OFF_REASON

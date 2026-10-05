"""Eligibility evaluation for Remote Analysis on a booking."""

from __future__ import annotations

from dataclasses import dataclass, field

from django.utils import timezone

from iic_booking.equipment.models import Booking, BookingStatus, SampleTraceStatus


@dataclass
class EligibilityResult:
    eligible: bool
    reason: str
    checks: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"eligible": self.eligible, "reason": self.reason, "checks": self.checks}


class BookingAnalysisEligibilityService:
    """Determine whether Remote Analysis may start for a booking."""

    TERMINAL_BLOCK = {
        BookingStatus.CANCELLED,
        BookingStatus.REFUNDED,
        BookingStatus.ABSENT,
        BookingStatus.BOOKING_NOT_UTILIZED,
    }

    def evaluate(self, booking: Booking) -> EligibilityResult:
        equipment = booking.equipment
        checks: dict[str, bool | str] = {}

        if not equipment or not getattr(equipment, "enable_remote_analysis", False):
            checks["equipment_enabled"] = False
            return EligibilityResult(False, "Remote Analysis is not enabled for this equipment", checks)
        checks["equipment_enabled"] = True

        department_reason = self.department_block_reason(booking, equipment)
        if department_reason:
            checks["department_enabled"] = False
            return EligibilityResult(False, department_reason, checks)
        checks["department_enabled"] = True

        if booking.status in self.TERMINAL_BLOCK:
            checks["booking_active"] = False
            return EligibilityResult(False, f"Booking status {booking.status} blocks analysis", checks)
        checks["booking_active"] = True

        required_status = (equipment.remote_analysis_enabled_from_status or BookingStatus.COMPLETED).upper()
        if getattr(equipment, "analysis_requires_experiment_completion", True):
            if booking.status != BookingStatus.COMPLETED and booking.status != required_status:
                checks["experiment_completed"] = False
                return EligibilityResult(
                    False,
                    f"Booking must be {required_status} before Remote Analysis",
                    checks,
                )
            checks["experiment_completed"] = True
        else:
            checks["experiment_completed"] = True

        if booking.status == BookingStatus.PENDING_PAYMENT:
            checks["payment_complete"] = False
            return EligibilityResult(False, "Payment is not complete", checks)
        # Settled or non-pending-payment statuses are treated as payment-ok for eligibility
        checks["payment_complete"] = True
        checks["approval_complete"] = booking.status not in {BookingStatus.PENDING, BookingStatus.WAITLISTED}

        if getattr(equipment, "analysis_requires_sample_acceptance", False):
            accepted = self._sample_accepted(booking)
            checks["sample_accepted"] = accepted
            if not accepted:
                return EligibilityResult(False, "Sample acceptance is required", checks)
        else:
            checks["sample_accepted"] = True

        if booking.analysis_expiry and booking.analysis_expiry < timezone.now():
            checks["not_expired"] = False
            return EligibilityResult(False, "Analysis access has expired", checks)
        checks["not_expired"] = True

        if getattr(booking, "analysis_closed_at", None):
            checks["analysis_open"] = False
            return EligibilityResult(
                False,
                "Remote analysis session is over for this booking",
                checks,
            )
        try:
            from iic_booking.equipment.remote_analysis_integration.session_close import (
                ensure_analysis_closed_from_history,
            )

            if ensure_analysis_closed_from_history(booking):
                booking.refresh_from_db(fields=["analysis_closed_at"])
                if getattr(booking, "analysis_closed_at", None):
                    checks["analysis_open"] = False
                    return EligibilityResult(
                        False,
                        "Remote analysis session is over for this booking",
                        checks,
                    )
        except Exception:
            pass
        checks["analysis_open"] = True

        limit = int(getattr(equipment, "analysis_session_limit", 0) or 0)
        if limit > 0 and int(booking.analysis_session_count or 0) >= limit:
            checks["session_limit"] = False
            return EligibilityResult(False, "Analysis session limit reached", checks)
        checks["session_limit"] = True

        return EligibilityResult(True, "Eligible for Remote Analysis", checks)

    def department_block_reason(self, booking: Booking, equipment) -> str:
        """Per-department switch: bookings made before it was turned off (or limited) and live reservations finish."""
        dept_id = getattr(equipment, "internal_department_id", None)
        if not dept_id:
            return ""
        from iic_booking.department_modules import access as dept_access
        from iic_booking.department_modules.constants import ModuleKey

        cell = dept_access.cell(dept_id, ModuleKey.REMOTE_ANALYSIS)
        if not cell.restricted:
            return ""
        if cell.allows(is_test=dept_access.is_test(getattr(booking, "user", None)), started_at=getattr(booking, "created_at", None)):
            return ""
        try:
            from iic_booking.remote_analysis.services.reservation import TERMINAL
            from iic_booking.remote_analysis.scheduler_models import AnalysisReservation

            if AnalysisReservation.objects.filter(booking=booking).exclude(status__in=TERMINAL).exists():
                return ""
        except Exception:
            pass
        if not cell.enabled:
            return "Remote Analysis is switched off for this equipment's department"
        return "Remote Analysis for this equipment's department is open to test accounts only"

    def _sample_accepted(self, booking: Booking) -> bool:
        try:
            from iic_booking.equipment.models import BookingSampleTrace

            return BookingSampleTrace.objects.filter(
                booking=booking,
                status__in=[SampleTraceStatus.SAMPLE_ACCEPTED, SampleTraceStatus.COMPLETED],
            ).exists()
        except Exception:
            # If trace table missing or empty, do not hard-fail when requirement is on
            return False

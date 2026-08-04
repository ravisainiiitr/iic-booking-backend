"""Session authorization gates for Guacamole remote desktop (Phase 3).

Reuses booking eligibility, reservation window, workspace, and workstation checks.
Does not duplicate user management.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from datetime import timedelta

from django.utils import timezone

from iic_booking.remote_analysis.constants import AuditCategory, ReservationStatus, SessionStatus
from iic_booking.remote_analysis.services.audit import record_event
from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings, RemoteDesktopSession


ACTIVE_RESERVATION_STATUSES = {
    ReservationStatus.RESERVED,
    ReservationStatus.AWAITING_CHECKIN,
    ReservationStatus.PREPARING,
    ReservationStatus.READY,
    ReservationStatus.ACTIVE,
}

OPEN_SESSION_STATUSES = {
    SessionStatus.CREATED,
    SessionStatus.PREPARING,
    SessionStatus.READY,
    SessionStatus.TOKEN_GENERATED,
    SessionStatus.LAUNCHED,
    SessionStatus.CONNECTING,
    SessionStatus.CONNECTED,
    SessionStatus.ACTIVE,
    SessionStatus.IDLE,
}


@dataclass
class GateResult:
    ok: bool
    code: str = ""
    reason: str = ""
    checks: dict = field(default_factory=dict)

    def raise_session_error(self):
        from iic_booking.remote_analysis.guacamole.session import SessionError

        raise SessionError(self.reason or "Session not authorized", code=self.code or "forbidden")


def _reject(
    *,
    code: str,
    reason: str,
    checks: dict,
    reservation=None,
    booking=None,
    user=None,
    client_ip: str | None = None,
    action: str = "SessionAuthzRejected",
) -> GateResult:
    ws = getattr(reservation, "workstation", None) if reservation else None
    details = f"{code}: {reason}"
    if client_ip:
        details = f"{details} ip={client_ip}"
    record_event(
        category=AuditCategory.SESSION,
        action=action,
        details=details[:1000],
        success=False,
        workstation=ws,
        actor=user,
        correlation_id=str(getattr(reservation, "id", "") or getattr(booking, "booking_id", "") or ""),
    )
    if booking is not None:
        try:
            from iic_booking.equipment.remote_analysis_integration.audit import BookingAuditBridge

            BookingAuditBridge().log(booking, action, details=details[:500], actor=user, success=False)
        except Exception:
            pass
    return GateResult(False, code=code, reason=reason, checks=checks)


def evaluate_session_create_gates(
    *,
    reservation,
    user,
    client_ip: str | None = None,
    settings_obj: RemoteAnalysisSettings | None = None,
) -> GateResult:
    """Authorize creating a remote desktop session for a reservation."""
    settings_obj = settings_obj or RemoteAnalysisSettings.get_solo()
    checks: dict = {}
    booking = getattr(reservation, "booking", None)

    if reservation.status not in ACTIVE_RESERVATION_STATUSES:
        checks["reservation_active"] = False
        return _reject(
            code="reservation_inactive",
            reason="Reservation is not active",
            checks=checks,
            reservation=reservation,
            booking=booking,
            user=user,
            client_ip=client_ip,
        )
    checks["reservation_active"] = True

    now = timezone.now()
    # Allow prepare before window start; launch gates enforce started window.
    if reservation.requested_end and reservation.requested_end < now:
        checks["analysis_window_open"] = False
        return _reject(
            code="reservation_expired",
            reason="Reservation / analysis window has ended",
            checks=checks,
            reservation=reservation,
            booking=booking,
            user=user,
            client_ip=client_ip,
        )
    checks["analysis_window_open"] = True
    checks["analysis_window_started"] = not (
        reservation.requested_start and reservation.requested_start > now
    )

    if not reservation.workstation_id:
        checks["workstation_assigned"] = False
        return _reject(
            code="no_workstation",
            reason="Reservation has no allocated workstation",
            checks=checks,
            reservation=reservation,
            booking=booking,
            user=user,
            client_ip=client_ip,
        )
    checks["workstation_assigned"] = True

    if booking is not None:
        from iic_booking.equipment.remote_analysis_integration.eligibility import BookingAnalysisEligibilityService

        elig = BookingAnalysisEligibilityService().evaluate(booking)
        checks["booking_eligible"] = elig.eligible
        checks["eligibility"] = elig.as_dict()
        if not elig.eligible:
            return _reject(
                code="booking_ineligible",
                reason=elig.reason,
                checks=checks,
                reservation=reservation,
                booking=booking,
                user=user,
                client_ip=client_ip,
            )

        # Terminal / cancelled already covered by eligibility; double-check cancelled-like statuses
        status = str(getattr(booking, "status", "") or "").upper()
        if status in {"CANCELLED", "REFUNDED", "ABSENT", "BOOKING_NOT_UTILIZED"}:
            checks["booking_not_terminal"] = False
            return _reject(
                code="booking_terminal",
                reason=f"Booking status {status} blocks remote desktop",
                checks=checks,
                reservation=reservation,
                booking=booking,
                user=user,
                client_ip=client_ip,
            )
        checks["booking_not_terminal"] = True

    # Workspace must exist (ensure caller may create it; gate verifies presence or allow create path)
    from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace

    workspace = AnalysisWorkspace.objects.filter(reservation_id=reservation.id).first()
    checks["workspace_exists"] = bool(workspace)
    # Workspace may be created during session create; require only when already expected via booking link
    if booking is not None and getattr(booking, "analysis_workspace_id", None) and not workspace:
        return _reject(
            code="workspace_missing",
            reason="Linked analysis workspace not found",
            checks=checks,
            reservation=reservation,
            booking=booking,
            user=user,
            client_ip=client_ip,
        )

    if getattr(settings_obj, "single_active_session_per_booking", True):
        qs = RemoteDesktopSession.objects.filter(status__in=OPEN_SESSION_STATUSES)
        if booking is not None:
            qs = qs.filter(booking_id=booking.pk)
        else:
            qs = qs.filter(reservation_id=reservation.id)
        open_sessions = list(qs.order_by("-created_at")[:5])
        checks["open_session_count"] = len(open_sessions)
        # Idempotent reuse is allowed; gate only blocks when caller would create a second distinct session.
        # create_session handles reuse; here we expose the flag for launch checks.
        checks["single_active_session_per_booking"] = True
    else:
        checks["single_active_session_per_booking"] = False

    return GateResult(True, checks=checks)


def evaluate_session_launch_gates(
    *,
    session: RemoteDesktopSession,
    user,
    client_ip: str | None = None,
    settings_obj: RemoteAnalysisSettings | None = None,
) -> GateResult:
    """Re-check authorization immediately before issuing a launch token."""
    settings_obj = settings_obj or RemoteAnalysisSettings.get_solo()
    reservation = session.reservation
    booking = session.booking or getattr(reservation, "booking", None)
    base = evaluate_session_create_gates(
        reservation=reservation,
        user=user,
        client_ip=client_ip,
        settings_obj=settings_obj,
    )
    if not base.ok:
        return base

    checks = dict(base.checks)
    now = timezone.now()
    # Booking analysis window already open ⇒ allow launch even if the scheduler
    # reservation slot is still in the future (common for Analyze Data on completed bookings).
    booking_from = getattr(booking, "analysis_available_from", None) if booking is not None else None
    if booking_from is not None and booking_from <= now:
        checks["analysis_window_started"] = True
        checks["analysis_window_source"] = "booking.analysis_available_from"
    else:
        # Allow early launch/prepare shortly before scheduled start (ops buffer).
        early_minutes = max(60, int(getattr(settings_obj, "prepare_timeout_seconds", 120) or 120) // 60)
        candidates = [
            t
            for t in (
                reservation.requested_start,
                booking_from,
                getattr(reservation, "reserved_start", None),
            )
            if t is not None
        ]
        window_start = min(candidates) if candidates else None
        if window_start and (window_start - timedelta(minutes=early_minutes)) > now:
            checks["analysis_window_started"] = False
            return _reject(
                code="window_not_started",
                reason="Analysis window has not started",
                checks=checks,
                reservation=reservation,
                booking=booking,
                user=user,
                client_ip=client_ip,
                action="LaunchRejected",
            )
        checks["analysis_window_started"] = True
        checks["analysis_window_source"] = "reservation_or_earliest"

    if session.user_id != getattr(user, "pk", None):
        from iic_booking.remote_analysis.permissions import CanManageRemoteAnalysis

        # Managers may observe/terminate but not steal launch — owner only (existing policy)
        checks["owner_launch"] = False
        return _reject(
            code="forbidden",
            reason="Only the reservation owner may launch this session",
            checks=checks,
            reservation=reservation,
            booking=booking,
            user=user,
            client_ip=client_ip,
            action="LaunchRejected",
        )
    checks["owner_launch"] = True

    if session.status in {
        SessionStatus.COMPLETED,
        SessionStatus.TERMINATED,
        SessionStatus.EXPIRED,
        SessionStatus.FAILED,
        SessionStatus.DISCONNECTING,
    }:
        checks["session_open"] = False
        return _reject(
            code="session_closed",
            reason=f"Session is closed (status={session.status})",
            checks=checks,
            reservation=reservation,
            booking=booking,
            user=user,
            client_ip=client_ip,
            action="LaunchRejected",
        )
    checks["session_open"] = True

    return GateResult(True, checks=checks)


def find_reusable_open_session(reservation, *, settings_obj: RemoteAnalysisSettings | None = None):
    """Return an existing open session when single-active policy applies."""
    settings_obj = settings_obj or RemoteAnalysisSettings.get_solo()
    if not getattr(settings_obj, "single_active_session_per_booking", True):
        return None
    booking = getattr(reservation, "booking", None)
    qs = RemoteDesktopSession.objects.filter(status__in=OPEN_SESSION_STATUSES)
    if booking is not None:
        qs = qs.filter(booking_id=booking.pk)
    else:
        qs = qs.filter(reservation_id=reservation.id)
    return qs.order_by("-created_at").first()

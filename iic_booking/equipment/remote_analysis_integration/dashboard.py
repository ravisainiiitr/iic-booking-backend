"""Dashboard aggregates for student / faculty / lab views."""

from __future__ import annotations

from django.db.models import Q
from django.utils import timezone

from iic_booking.equipment.models import Booking, BookingStatus
from iic_booking.remote_analysis.constants import ReservationStatus, SessionStatus


ACTIVE_SESSION = {
    SessionStatus.CONNECTING,
    SessionStatus.CONNECTED,
    SessionStatus.ACTIVE,
    SessionStatus.IDLE,
    SessionStatus.LAUNCHED,
}


class BookingAnalysisDashboardService:
    def for_user(self, user) -> dict:
        qs = Booking.objects.filter(user=user).select_related(
            "equipment", "analysis_reservation", "analysis_workspace"
        )
        return self._bucket(qs)

    def for_faculty(self, user) -> dict:
        qs = Booking.objects.filter(
            Q(user=user) | Q(created_by=user) | Q(user__department_id=getattr(user, "department_id", None))
        ).select_related("equipment", "analysis_reservation", "analysis_workspace")[:200]
        return self._bucket(qs)

    def for_lab(self, user) -> dict:
        from iic_booking.remote_analysis.constants import AlertStatus
        from iic_booking.remote_analysis.models import AnalysisWorkstation
        from iic_booking.remote_analysis.operations_models import AlertEvent
        from iic_booking.remote_analysis.session_models import RemoteDesktopSession

        workstations = list(
            AnalysisWorkstation.objects.all().values("id", "hostname", "status", "enabled")[:50]
        )
        active_sessions = RemoteDesktopSession.objects.filter(status__in=ACTIVE_SESSION).count()
        alerts = AlertEvent.objects.filter(status__in=[AlertStatus.OPEN, AlertStatus.ACKNOWLEDGED]).count()
        ready_bookings = Booking.objects.filter(analysis_available=True, status=BookingStatus.COMPLETED).count()
        return {
            "workstations": workstations,
            "active_sessions": active_sessions,
            "open_alerts": alerts,
            "analysis_ready_bookings": ready_bookings,
        }

    def _bucket(self, qs) -> dict:
        from iic_booking.remote_analysis.session_models import RemoteDesktopSession

        ready, preparing, running, completed, expired = [], [], [], [], []
        now = timezone.now()
        for b in qs.filter(equipment__enable_remote_analysis=True)[:100]:
            item = {
                "booking_id": b.booking_id,
                "equipment": getattr(b.equipment, "code", None),
                "analysis_available": b.analysis_available,
                "reservation_status": getattr(b.analysis_reservation, "status", None),
            }
            if b.analysis_expiry and b.analysis_expiry < now:
                expired.append(item)
                continue
            res = b.analysis_reservation
            if not b.analysis_available and not res:
                continue
            if res and res.status in {
                ReservationStatus.PREPARING,
                ReservationStatus.QUEUED,
                ReservationStatus.VALIDATING,
                ReservationStatus.REQUESTED,
            }:
                preparing.append(item)
            elif res and res.status == ReservationStatus.COMPLETED:
                completed.append(item)
            elif res and res.status in {
                ReservationStatus.ACTIVE,
                ReservationStatus.READY,
                ReservationStatus.RESERVED,
            }:
                sess = RemoteDesktopSession.objects.filter(reservation=res).order_by("-created_at").first()
                if sess and sess.status in ACTIVE_SESSION:
                    running.append(item)
                else:
                    ready.append(item)
            elif b.analysis_available:
                ready.append(item)
        return {
            "ready": ready,
            "preparing": preparing,
            "running": running,
            "completed": completed,
            "expired": expired,
        }

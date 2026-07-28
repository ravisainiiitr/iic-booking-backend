"""Thin collaboration hooks — emit notifications/activity without redesigning core flows."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def on_session_created(session, *, actor=None) -> None:
    try:
        from iic_booking.remote_analysis.activity import ActivityService
        from iic_booking.remote_analysis.constants import ActivityVerb, NotificationType
        from iic_booking.remote_analysis.notifications import NotificationEngine

        user = session.user
        ActivityService().record(
            ActivityVerb.SESSION_START,
            f"Session created on {getattr(session.workstation, 'hostname', '')}",
            actor=actor or user,
            user=user,
            session=session,
            reservation=session.reservation,
        )
        NotificationEngine().notify(
            user,
            NotificationType.SESSION_STARTING,
            "Session starting",
            f"Remote analysis session {session.id} is preparing.",
            metadata={"session_id": str(session.id)},
        )
    except Exception:
        logger.exception("collaboration hook on_session_created failed")


def on_session_terminated(session, *, actor=None, reason: str = "") -> None:
    try:
        from iic_booking.remote_analysis.activity import ActivityService
        from iic_booking.remote_analysis.constants import ActivityVerb, NotificationType
        from iic_booking.remote_analysis.notifications import NotificationEngine

        user = session.user
        ActivityService().record(
            ActivityVerb.SESSION_END,
            f"Session ended: {reason or session.status}",
            actor=actor or user,
            user=user,
            session=session,
            reservation=session.reservation,
        )
        NotificationEngine().notify(
            user,
            NotificationType.SESSION_TERMINATED,
            "Session terminated",
            reason or f"Session {session.id} ended.",
            metadata={"session_id": str(session.id)},
        )
    except Exception:
        logger.exception("collaboration hook on_session_terminated failed")


def on_workspace_synced(workspace, *, actor=None) -> None:
    try:
        from iic_booking.remote_analysis.activity import ActivityService
        from iic_booking.remote_analysis.constants import ActivityVerb, NotificationType
        from iic_booking.remote_analysis.notifications import NotificationEngine

        user = workspace.user
        ActivityService().record(
            ActivityVerb.SYNC,
            f"Workspace synchronized: {workspace.id}",
            actor=actor or user,
            user=user,
            workspace=workspace,
            reservation=workspace.reservation,
        )
        NotificationEngine().notify(
            user,
            NotificationType.WORKSPACE_SYNCED,
            "Workspace synchronized",
            f"Workspace {workspace.id} sync completed.",
            metadata={"workspace_id": str(workspace.id)},
        )
    except Exception:
        logger.exception("collaboration hook on_workspace_synced failed")


def on_transfer_complete(transfer, *, is_upload: bool = True) -> None:
    try:
        from iic_booking.remote_analysis.activity import ActivityService
        from iic_booking.remote_analysis.constants import ActivityVerb, NotificationType
        from iic_booking.remote_analysis.notifications import NotificationEngine

        workspace = transfer.workspace
        user = workspace.user
        verb = ActivityVerb.UPLOAD if is_upload else ActivityVerb.DOWNLOAD
        ntype = NotificationType.UPLOAD_COMPLETE if is_upload else NotificationType.DOWNLOAD_COMPLETE
        title = "Upload complete" if is_upload else "Download complete"
        ActivityService().record(
            verb,
            f"{title}: {getattr(transfer, 'id', '')}",
            actor=user,
            user=user,
            workspace=workspace,
        )
        NotificationEngine().notify(
            user,
            ntype,
            title,
            f"Transfer {transfer.id} completed.",
            metadata={"transfer_id": str(transfer.id), "workspace_id": str(workspace.id)},
        )
    except Exception:
        logger.exception("collaboration hook on_transfer_complete failed")


def on_alert_raised(event) -> None:
    try:
        from iic_booking.remote_analysis.activity import ActivityService
        from iic_booking.remote_analysis.constants import ActivityVerb, NotificationType
        from iic_booking.remote_analysis.notifications import NotificationEngine
        from django.contrib.auth import get_user_model

        ActivityService().record(
            ActivityVerb.ALERT,
            event.title,
            details=event.message or "",
            also_global=True,
            user=None,
            metadata={"alert_id": str(event.id)},
        )
        # Notify staff users with manage permission is heavy; notify workstation owner if any
        User = get_user_model()
        recipients = User.objects.filter(is_staff=True, is_active=True)[:10]
        for u in recipients:
            NotificationEngine().notify(
                u,
                NotificationType.ALERT,
                event.title,
                event.message or "",
                metadata={"alert_id": str(event.id)},
                channels=["PORTAL"],
            )
    except Exception:
        logger.exception("collaboration hook on_alert_raised failed")


def on_reservation_confirmed(reservation) -> None:
    try:
        from iic_booking.remote_analysis.activity import ActivityService
        from iic_booking.remote_analysis.constants import ActivityVerb, NotificationType
        from iic_booking.remote_analysis.notifications import NotificationEngine

        user = reservation.user
        ActivityService().record(
            ActivityVerb.RESERVATION,
            f"Reservation confirmed: {reservation.id}",
            actor=user,
            user=user,
            reservation=reservation,
        )
        NotificationEngine().notify(
            user,
            NotificationType.RESERVATION_CONFIRMED,
            "Reservation confirmed",
            f"Your analysis reservation {reservation.id} is confirmed.",
            metadata={"reservation_id": str(reservation.id)},
        )
    except Exception:
        logger.exception("collaboration hook on_reservation_confirmed failed")

"""Session assistance workflow."""

from __future__ import annotations

from django.utils import timezone

from iic_booking.remote_analysis.activity import ActivityService
from iic_booking.remote_analysis.collaboration_models import (
    CollaborationTelemetry,
    SessionAssistanceEvent,
    SessionAssistanceRequest,
)
from iic_booking.remote_analysis.constants import (
    ActivityVerb,
    AssistancePriority,
    AssistanceStatus,
    AuditCategory,
    NotificationType,
)
from iic_booking.remote_analysis.notifications import NotificationEngine
from iic_booking.remote_analysis.permissions import CanManageRemoteAnalysis
from iic_booking.remote_analysis.services.audit import record_event


class AssistanceError(Exception):
    def __init__(self, message: str, code: str = "assistance_error"):
        super().__init__(message)
        self.code = code


class AssistanceService:
    def request_help(
        self,
        requested_by,
        subject: str,
        description: str = "",
        *,
        session=None,
        reservation=None,
        priority: str = AssistancePriority.NORMAL,
    ) -> SessionAssistanceRequest:
        row = SessionAssistanceRequest.objects.create(
            requested_by=requested_by,
            subject=subject[:255],
            description=description,
            session=session,
            reservation=reservation,
            priority=priority,
            status=AssistanceStatus.REQUESTED,
        )
        SessionAssistanceEvent.objects.create(
            request=row,
            actor=requested_by,
            from_status="",
            to_status=AssistanceStatus.REQUESTED,
            note="Help requested",
        )
        ActivityService().record(
            ActivityVerb.ASSISTANCE,
            f"Help requested: {subject}",
            actor=requested_by,
            user=requested_by,
            session=session,
            reservation=reservation,
        )
        record_event(category=AuditCategory.ASSISTANCE, action="HelpRequested", details=str(row.id), actor=requested_by)
        return row

    def _transition(self, request: SessionAssistanceRequest, to_status: str, actor, note: str = "") -> SessionAssistanceRequest:
        from_status = request.status
        request.status = to_status
        fields = ["status"]
        now = timezone.now()
        if to_status == AssistanceStatus.ACCEPTED:
            request.accepted_at = now
            fields.append("accepted_at")
        if to_status == AssistanceStatus.RESOLVED:
            request.resolved_at = now
            fields.append("resolved_at")
            if request.accepted_at:
                minutes = (now - request.accepted_at).total_seconds() / 60.0
                CollaborationTelemetry.objects.create(
                    metric_name="help_response_time",
                    value=minutes,
                    unit="minutes",
                )
        if to_status == AssistanceStatus.CLOSED:
            request.closed_at = now
            fields.append("closed_at")
        request.save(update_fields=fields)
        SessionAssistanceEvent.objects.create(
            request=request,
            actor=actor if actor is not None and getattr(actor, "pk", None) else None,
            from_status=from_status,
            to_status=to_status,
            note=note,
        )
        return request

    def assign(self, request: SessionAssistanceRequest, operator, actor=None) -> SessionAssistanceRequest:
        if not CanManageRemoteAnalysis().has_permission(type("R", (), {"user": actor or operator, "method": "POST"})(), None):
            raise AssistanceError("Not authorized", "forbidden")
        request.assigned_to = operator
        request.save(update_fields=["assigned_to"])
        self._transition(request, AssistanceStatus.ASSIGNED, actor or operator, note=f"Assigned to {operator}")
        NotificationEngine().notify(
            operator,
            NotificationType.ASSISTANCE,
            "Assistance assigned",
            request.subject,
            metadata={"request_id": str(request.id)},
        )
        return request

    def accept(self, request: SessionAssistanceRequest, operator) -> SessionAssistanceRequest:
        if request.assigned_to_id and request.assigned_to_id != operator.pk:
            if not CanManageRemoteAnalysis().has_permission(type("R", (), {"user": operator, "method": "POST"})(), None):
                raise AssistanceError("Not assigned to you", "forbidden")
        if not request.assigned_to_id:
            request.assigned_to = operator
            request.save(update_fields=["assigned_to"])
        return self._transition(request, AssistanceStatus.ACCEPTED, operator, note="Accepted")

    def resolve(self, request: SessionAssistanceRequest, operator, resolution: str = "") -> SessionAssistanceRequest:
        if resolution:
            request.resolution = resolution
            request.save(update_fields=["resolution"])
        self._transition(request, AssistanceStatus.RESOLVED, operator, note=resolution[:500])
        NotificationEngine().notify(
            request.requested_by,
            NotificationType.ASSISTANCE,
            "Assistance resolved",
            resolution or request.subject,
            metadata={"request_id": str(request.id)},
        )
        return request

    def close(self, request: SessionAssistanceRequest, actor) -> SessionAssistanceRequest:
        return self._transition(request, AssistanceStatus.CLOSED, actor, note="Closed")

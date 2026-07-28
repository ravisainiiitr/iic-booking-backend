"""Workspace sharing and invitations."""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from iic_booking.remote_analysis.activity import ActivityService
from iic_booking.remote_analysis.collaboration_models import (
    CollaborationTelemetry,
    SessionInvitation,
    SharedWorkspace,
    WorkspaceSharePermission,
)
from iic_booking.remote_analysis.constants import (
    ActivityVerb,
    AuditCategory,
    InvitationKind,
    InvitationStatus,
    NotificationType,
    SharePermissionLevel,
)
from iic_booking.remote_analysis.notifications import NotificationEngine
from iic_booking.remote_analysis.permissions import CanManageRemoteAnalysis
from iic_booking.remote_analysis.services.audit import record_event


class SharingError(Exception):
    def __init__(self, message: str, code: str = "share_error"):
        super().__init__(message)
        self.code = code


class SharingService:
    def can_share(self, user, workspace) -> bool:
        if workspace.user_id == getattr(user, "pk", None):
            return True
        return CanManageRemoteAnalysis().has_permission(type("R", (), {"user": user, "method": "POST"})(), None)

    def share(
        self,
        workspace,
        created_by,
        *,
        user=None,
        department=None,
        permissions: list[str] | None = None,
        name: str = "",
        expires_hours: int | None = 72,
    ) -> SharedWorkspace:
        if not self.can_share(created_by, workspace):
            raise SharingError("Not authorized to share this workspace", "forbidden")
        if not user and not department:
            raise SharingError("user or department required", "invalid")
        # Never anonymous
        expires = timezone.now() + timedelta(hours=expires_hours) if expires_hours else None
        shared = SharedWorkspace.objects.create(
            workspace=workspace,
            name=name or f"Share {workspace.id}",
            created_by=created_by,
            expires_at=expires,
        )
        perms = permissions or [SharePermissionLevel.READ, SharePermissionLevel.COMMENT]
        for p in perms:
            WorkspaceSharePermission.objects.create(
                shared_workspace=shared,
                user=user,
                department=department,
                permission=p,
            )
        ActivityService().record(
            ActivityVerb.SHARE,
            f"Workspace shared: {shared.name}",
            actor=created_by,
            user=workspace.user,
            workspace=workspace,
        )
        if user:
            NotificationEngine().notify(
                user,
                NotificationType.SHARE,
                "Workspace shared with you",
                f"You were granted access to workspace {workspace.id}",
                metadata={"workspace_id": str(workspace.id), "share_id": str(shared.id)},
            )
        record_event(
            category=AuditCategory.COLLABORATION,
            action="ShareGranted",
            details=str(shared.id),
            actor=created_by,
            workstation=workspace.workstation,
        )
        CollaborationTelemetry.objects.create(metric_name="workspace_sharing", value=1.0)
        return shared

    def user_has_permission(self, user, workspace, permission: str) -> bool:
        if workspace.user_id == getattr(user, "pk", None):
            return True
        if CanManageRemoteAnalysis().has_permission(type("R", (), {"user": user, "method": "GET"})(), None):
            return True
        now = timezone.now()
        qs = WorkspaceSharePermission.objects.filter(
            shared_workspace__workspace=workspace,
            shared_workspace__revoked_at__isnull=True,
            permission=permission,
        ).filter(
            models_q_active(now)
        )
        if qs.filter(user=user).exists():
            return True
        dept_id = getattr(user, "department_id", None)
        if dept_id and qs.filter(department_id=dept_id).exists():
            return True
        return False


def models_q_active(now):
    from django.db.models import Q

    return Q(shared_workspace__expires_at__isnull=True) | Q(shared_workspace__expires_at__gte=now)


class InvitationService:
    def invite(
        self,
        invited_by,
        *,
        invited_user=None,
        invited_email: str = "",
        session=None,
        reservation=None,
        workspace=None,
        kind: str = InvitationKind.COLLABORATOR,
        message: str = "",
        expires_hours: int = 72,
    ) -> SessionInvitation:
        if not invited_user and not invited_email:
            raise SharingError("invited_user or invited_email required", "invalid")
        inv = SessionInvitation.objects.create(
            invited_by=invited_by,
            invited_user=invited_user,
            invited_email=invited_email,
            session=session,
            reservation=reservation,
            workspace=workspace,
            kind=kind,
            message=message,
            expires_at=timezone.now() + timedelta(hours=expires_hours),
            status=InvitationStatus.PENDING,
        )
        target = invited_user
        if target:
            NotificationEngine().notify(
                target,
                NotificationType.INVITATION,
                "Session invitation",
                message or f"You were invited ({kind})",
                metadata={"invitation_id": str(inv.id)},
            )
        ActivityService().record(
            ActivityVerb.INVITATION,
            f"Invitation sent ({kind})",
            actor=invited_by,
            user=invited_user or invited_by,
            session=session,
            workspace=workspace,
            reservation=reservation,
        )
        record_event(category=AuditCategory.COLLABORATION, action="InvitationCreated", details=str(inv.id), actor=invited_by)
        return inv

    def accept(self, invitation: SessionInvitation, user) -> SessionInvitation:
        if invitation.status != InvitationStatus.PENDING:
            raise SharingError("Invitation not pending", "invalid_state")
        if invitation.expires_at and invitation.expires_at < timezone.now():
            invitation.status = InvitationStatus.EXPIRED
            invitation.save(update_fields=["status"])
            raise SharingError("Invitation expired", "expired")
        if invitation.invited_user_id and invitation.invited_user_id != user.pk:
            raise SharingError("Invitation not for this user", "forbidden")
        invitation.status = InvitationStatus.ACCEPTED
        invitation.accepted_at = timezone.now()
        invitation.invited_user = user
        invitation.save(update_fields=["status", "accepted_at", "invited_user"])
        CollaborationTelemetry.objects.create(metric_name="invitation_acceptance", value=1.0)
        return invitation

    def expire_stale(self) -> int:
        now = timezone.now()
        return SessionInvitation.objects.filter(
            status=InvitationStatus.PENDING,
            expires_at__lt=now,
        ).update(status=InvitationStatus.EXPIRED)

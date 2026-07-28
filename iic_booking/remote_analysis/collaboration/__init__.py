"""Collaboration Center facade + dashboard helpers."""

from __future__ import annotations

from django.utils import timezone

from iic_booking.remote_analysis.activity import ActivityService
from iic_booking.remote_analysis.assistance import AssistanceService
from iic_booking.remote_analysis.collaboration_models import (
    Announcement,
    Bookmark,
    FavoriteWorkstation,
    RecentWorkspace,
    SessionAssistanceRequest,
    SessionInvitation,
    SharedWorkspace,
)
from iic_booking.remote_analysis.constants import AssistanceStatus, InvitationStatus
from iic_booking.remote_analysis.notifications import NotificationEngine


class CollaborationDashboard:
    def build(self, user) -> dict:
        now = timezone.now()
        notifications = NotificationEngine().list_for_user(user, limit=30)
        activity = ActivityService().list_events(user, limit=40)
        pending_help = SessionAssistanceRequest.objects.filter(
            status__in=[AssistanceStatus.REQUESTED, AssistanceStatus.ASSIGNED, AssistanceStatus.ACCEPTED]
        ).select_related("requested_by", "assigned_to")[:20]
        if not getattr(user, "is_staff", False):
            # operators/managers see all pending; owners see own
            from iic_booking.remote_analysis.permissions import CanManageRemoteAnalysis

            if not CanManageRemoteAnalysis().has_permission(type("R", (), {"user": user, "method": "GET"})(), None):
                pending_help = [r for r in pending_help if r.requested_by_id == user.pk]

        shared = (
            SharedWorkspace.objects.filter(revoked_at__isnull=True)
            .filter(models_q_share_visible(user, now))
            .select_related("workspace")
            .distinct()[:20]
        )

        announcements = Announcement.objects.filter(active=True).order_by("-created_at")[:10]
        bookmarks = Bookmark.objects.filter(user=user)[:20]
        favorites = FavoriteWorkstation.objects.filter(user=user).select_related("workstation")[:20]
        recent = RecentWorkspace.objects.filter(user=user).select_related("workspace")[:20]
        invitations = SessionInvitation.objects.filter(
            invited_user=user,
            status=InvitationStatus.PENDING,
        )[:20]

        return {
            "notifications": [
                {
                    "id": str(n.id),
                    "type": n.notification_type,
                    "title": n.title,
                    "body": n.body,
                    "status": n.status,
                    "created_at": n.created_at.isoformat(),
                    "link": n.link,
                }
                for n in notifications
            ],
            "activity": [
                {
                    "id": str(e.id),
                    "verb": e.verb,
                    "summary": e.summary,
                    "created_at": e.created_at.isoformat(),
                    "actor": getattr(e.actor, "email", None),
                }
                for e in activity
            ],
            "pending_assistance": [
                {
                    "id": str(r.id),
                    "subject": r.subject,
                    "status": r.status,
                    "priority": r.priority,
                    "requested_by": getattr(r.requested_by, "email", None),
                    "created_at": r.created_at.isoformat(),
                }
                for r in pending_help
            ],
            "shared_workspaces": [
                {
                    "id": str(s.id),
                    "name": s.name,
                    "workspace_id": str(s.workspace_id),
                    "expires_at": s.expires_at.isoformat() if s.expires_at else None,
                }
                for s in shared
            ],
            "announcements": [
                {"id": str(a.id), "title": a.title, "body": a.body, "created_at": a.created_at.isoformat()}
                for a in announcements
            ],
            "bookmarks": [
                {"id": str(b.id), "label": b.label, "target_type": b.target_type, "target_id": b.target_id, "url": b.url}
                for b in bookmarks
            ],
            "favorites": [
                {"id": str(f.id), "workstation_id": str(f.workstation_id), "hostname": f.workstation.hostname}
                for f in favorites
            ],
            "recent_workspaces": [
                {"id": str(r.id), "workspace_id": str(r.workspace_id), "last_accessed_at": r.last_accessed_at.isoformat()}
                for r in recent
            ],
            "invitations": [
                {
                    "id": str(i.id),
                    "kind": i.kind,
                    "status": i.status,
                    "message": i.message,
                    "expires_at": i.expires_at.isoformat() if i.expires_at else None,
                }
                for i in invitations
            ],
            "generated_at": now.isoformat(),
        }


def models_q_share_visible(user, now):
    from django.db.models import Q

    return (
        Q(created_by=user)
        | Q(permissions__user=user)
        | Q(permissions__department_id=getattr(user, "department_id", None))
    ) & (Q(expires_at__isnull=True) | Q(expires_at__gte=now))

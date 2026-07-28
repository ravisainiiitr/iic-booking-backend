"""Comments and notes services."""

from __future__ import annotations

from iic_booking.remote_analysis.activity import ActivityService
from iic_booking.remote_analysis.collaboration_models import (
    CollaborationTelemetry,
    SessionComment,
    SessionNote,
    WorkspaceComment,
)
from iic_booking.remote_analysis.constants import ActivityVerb, AuditCategory, NoteVisibility, NotificationType
from iic_booking.remote_analysis.notifications import NotificationEngine
from iic_booking.remote_analysis.services.audit import record_event


class CommentService:
    def add_session_comment(self, session, author, body: str, *, pinned: bool = False) -> SessionComment:
        row = SessionComment.objects.create(session=session, author=author, body=body, pinned=pinned)
        ActivityService().record(
            ActivityVerb.COMMENT,
            f"Comment on session {session.id}",
            actor=author,
            user=session.user,
            session=session,
            details=body[:500],
        )
        if session.user_id != author.pk:
            NotificationEngine().notify(
                session.user,
                NotificationType.COMMENT,
                "New session comment",
                body[:200],
                metadata={"session_id": str(session.id)},
            )
        record_event(category=AuditCategory.COLLABORATION, action="SessionComment", details=str(row.id), actor=author)
        CollaborationTelemetry.objects.create(metric_name="comment_count", value=1.0, tags={"kind": "session"})
        return row

    def add_workspace_comment(self, workspace, author, body: str, *, pinned: bool = False) -> WorkspaceComment:
        row = WorkspaceComment.objects.create(workspace=workspace, author=author, body=body, pinned=pinned)
        ActivityService().record(
            ActivityVerb.COMMENT,
            f"Comment on workspace {workspace.id}",
            actor=author,
            user=workspace.user,
            workspace=workspace,
            details=body[:500],
        )
        if workspace.user_id != author.pk:
            NotificationEngine().notify(
                workspace.user,
                NotificationType.COMMENT,
                "New workspace comment",
                body[:200],
                metadata={"workspace_id": str(workspace.id)},
            )
        record_event(category=AuditCategory.COLLABORATION, action="WorkspaceComment", details=str(row.id), actor=author)
        CollaborationTelemetry.objects.create(metric_name="comment_count", value=1.0, tags={"kind": "workspace"})
        return row


class NoteService:
    def add_note(
        self,
        author,
        body: str,
        *,
        session=None,
        workspace=None,
        title: str = "",
        visibility: str = NoteVisibility.PRIVATE,
        pinned: bool = False,
    ) -> SessionNote:
        row = SessionNote.objects.create(
            author=author,
            body=body,
            title=title[:255],
            session=session,
            workspace=workspace,
            visibility=visibility,
            pinned=pinned,
        )
        ActivityService().record(
            ActivityVerb.NOTE,
            title or "Note added",
            actor=author,
            user=getattr(session, "user", None) or getattr(workspace, "user", None),
            session=session,
            workspace=workspace,
            details=body[:500],
        )
        record_event(category=AuditCategory.COLLABORATION, action="NoteCreated", details=str(row.id), actor=author)
        return row

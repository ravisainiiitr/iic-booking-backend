"""API views for Collaboration Center."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.remote_analysis.activity import ActivityService
from iic_booking.remote_analysis.assistance import AssistanceError, AssistanceService
from iic_booking.remote_analysis.collaboration import CollaborationDashboard
from iic_booking.remote_analysis.collaboration_models import (
    Announcement,
    Bookmark,
    FavoriteWorkstation,
    RecentWorkspace,
    SessionAssistanceRequest,
    SessionComment,
    SessionInvitation,
    SessionNote,
    SharedWorkspace,
    WorkspaceComment,
)
from iic_booking.remote_analysis.comments import CommentService, NoteService
from iic_booking.remote_analysis.constants import AssistancePriority, InvitationKind, NoteVisibility, SharePermissionLevel
from iic_booking.remote_analysis.notifications import NotificationEngine
from iic_booking.remote_analysis.permissions import CanManageRemoteAnalysis, CanViewRemoteAnalysis
from iic_booking.remote_analysis.scheduler_models import AnalysisReservation
from iic_booking.remote_analysis.session_models import RemoteDesktopSession
from iic_booking.remote_analysis.sharing import InvitationService, SharingError, SharingService
from iic_booking.remote_analysis.timeline import TimelineService
from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace

_AUTH = [IsAuthenticated]
_VIEW = [IsAuthenticated, CanViewRemoteAnalysis]
_MANAGE = [IsAuthenticated, CanManageRemoteAnalysis]
User = get_user_model()


@api_view(["GET"])
@permission_classes(_AUTH)
def collaboration_dashboard(request):
    return Response(CollaborationDashboard().build(request.user))


@api_view(["GET"])
@permission_classes(_AUTH)
def activity_feed(request):
    """GET /api/v1/analysis/activity/"""
    verb = request.query_params.get("verb")
    events = ActivityService().list_events(request.user, limit=100, verb=verb)
    return Response(
        [
            {
                "id": str(e.id),
                "verb": e.verb,
                "summary": e.summary,
                "details": e.details,
                "created_at": e.created_at.isoformat(),
                "actor": getattr(e.actor, "email", None),
                "session_id": str(e.session_id) if e.session_id else None,
                "workspace_id": str(e.workspace_id) if e.workspace_id else None,
            }
            for e in events
        ]
    )


@api_view(["GET"])
@permission_classes(_AUTH)
def notifications_list(request):
    """GET /api/v1/analysis/notifications/"""
    unread = request.query_params.get("unread") in {"1", "true", "yes"}
    rows = NotificationEngine().list_for_user(request.user, unread_only=unread, limit=100)
    return Response(
        [
            {
                "id": str(n.id),
                "type": n.notification_type,
                "channel": n.channel,
                "status": n.status,
                "title": n.title,
                "body": n.body,
                "link": n.link,
                "created_at": n.created_at.isoformat(),
                "read_at": n.read_at.isoformat() if n.read_at else None,
            }
            for n in rows
        ]
    )


@api_view(["POST"])
@permission_classes(_AUTH)
def notifications_read(request):
    """POST /api/v1/analysis/notifications/read/"""
    ids = request.data.get("ids") or []
    all_unread = bool(request.data.get("all"))
    count = NotificationEngine().mark_read(request.user, ids, all_unread=all_unread or not ids)
    return Response({"marked": count})


@api_view(["POST", "GET"])
@permission_classes(_AUTH)
def comments_collection(request):
    """POST/GET /api/v1/analysis/comments/"""
    if request.method == "GET":
        session_id = request.query_params.get("session_id")
        workspace_id = request.query_params.get("workspace_id")
        if session_id:
            rows = SessionComment.objects.filter(session_id=session_id, deleted=False).select_related("author")[:100]
            return Response(
                [
                    {
                        "id": str(c.id),
                        "body": c.body,
                        "author": c.author.email,
                        "pinned": c.pinned,
                        "created_at": c.created_at.isoformat(),
                        "kind": "session",
                    }
                    for c in rows
                ]
            )
        if workspace_id:
            rows = WorkspaceComment.objects.filter(workspace_id=workspace_id, deleted=False).select_related("author")[:100]
            return Response(
                [
                    {
                        "id": str(c.id),
                        "body": c.body,
                        "author": c.author.email,
                        "pinned": c.pinned,
                        "created_at": c.created_at.isoformat(),
                        "kind": "workspace",
                    }
                    for c in rows
                ]
            )
        return Response({"detail": "session_id or workspace_id required"}, status=status.HTTP_400_BAD_REQUEST)

    body = (request.data.get("body") or "").strip()
    if not body:
        return Response({"detail": "body required"}, status=status.HTTP_400_BAD_REQUEST)
    pinned = bool(request.data.get("pinned"))
    if request.data.get("session_id"):
        session = get_object_or_404(RemoteDesktopSession, pk=request.data["session_id"])
        row = CommentService().add_session_comment(session, request.user, body, pinned=pinned)
        return Response({"id": str(row.id), "kind": "session"}, status=status.HTTP_201_CREATED)
    if request.data.get("workspace_id"):
        workspace = get_object_or_404(AnalysisWorkspace, pk=request.data["workspace_id"])
        row = CommentService().add_workspace_comment(workspace, request.user, body, pinned=pinned)
        return Response({"id": str(row.id), "kind": "workspace"}, status=status.HTTP_201_CREATED)
    return Response({"detail": "session_id or workspace_id required"}, status=status.HTTP_400_BAD_REQUEST)


@api_view(["POST", "GET"])
@permission_classes(_AUTH)
def notes_collection(request):
    """POST/GET /api/v1/analysis/notes/"""
    if request.method == "GET":
        qs = SessionNote.objects.select_related("author").order_by("-created_at")
        if request.query_params.get("session_id"):
            qs = qs.filter(session_id=request.query_params["session_id"])
        if request.query_params.get("workspace_id"):
            qs = qs.filter(workspace_id=request.query_params["workspace_id"])
        # Private notes only visible to author unless manager
        if not CanManageRemoteAnalysis().has_permission(request, None):
            from django.db.models import Q

            qs = qs.filter(Q(visibility=NoteVisibility.PUBLIC) | Q(author=request.user))
        return Response(
            [
                {
                    "id": str(n.id),
                    "title": n.title,
                    "body": n.body,
                    "visibility": n.visibility,
                    "pinned": n.pinned,
                    "author": n.author.email,
                    "created_at": n.created_at.isoformat(),
                }
                for n in qs[:100]
            ]
        )

    body = (request.data.get("body") or "").strip()
    if not body:
        return Response({"detail": "body required"}, status=status.HTTP_400_BAD_REQUEST)
    session = (
        get_object_or_404(RemoteDesktopSession, pk=request.data["session_id"])
        if request.data.get("session_id")
        else None
    )
    workspace = (
        get_object_or_404(AnalysisWorkspace, pk=request.data["workspace_id"])
        if request.data.get("workspace_id")
        else None
    )
    row = NoteService().add_note(
        request.user,
        body,
        session=session,
        workspace=workspace,
        title=request.data.get("title") or "",
        visibility=(request.data.get("visibility") or NoteVisibility.PRIVATE).upper(),
        pinned=bool(request.data.get("pinned")),
    )
    return Response({"id": str(row.id)}, status=status.HTTP_201_CREATED)


@api_view(["POST", "GET"])
@permission_classes(_AUTH)
def share_collection(request):
    """POST/GET /api/v1/analysis/share/"""
    if request.method == "GET":
        qs = SharedWorkspace.objects.filter(revoked_at__isnull=True).select_related("workspace", "created_by")[:50]
        return Response(
            [
                {
                    "id": str(s.id),
                    "name": s.name,
                    "workspace_id": str(s.workspace_id),
                    "created_by": s.created_by.email,
                    "expires_at": s.expires_at.isoformat() if s.expires_at else None,
                }
                for s in qs
            ]
        )

    workspace = get_object_or_404(AnalysisWorkspace, pk=request.data.get("workspace_id"))
    user = None
    if request.data.get("user_id"):
        user = get_object_or_404(User, pk=request.data["user_id"])
    elif request.data.get("user_email"):
        user = User.objects.filter(email__iexact=request.data["user_email"]).first()
        if not user:
            return Response({"detail": "User not found"}, status=status.HTTP_404_NOT_FOUND)
    department = None
    if request.data.get("department_id"):
        from iic_booking.users.models import Department

        department = get_object_or_404(Department, pk=request.data["department_id"])
    perms = request.data.get("permissions") or [SharePermissionLevel.READ, SharePermissionLevel.COMMENT]
    try:
        shared = SharingService().share(
            workspace,
            request.user,
            user=user,
            department=department,
            permissions=perms,
            name=request.data.get("name") or "",
            expires_hours=request.data.get("expires_hours", 72),
        )
    except SharingError as exc:
        code = status.HTTP_403_FORBIDDEN if exc.code == "forbidden" else status.HTTP_400_BAD_REQUEST
        return Response({"detail": str(exc), "code": exc.code}, status=code)
    return Response({"id": str(shared.id)}, status=status.HTTP_201_CREATED)


@api_view(["POST", "GET"])
@permission_classes(_AUTH)
def invite_collection(request):
    """POST/GET /api/v1/analysis/invite/"""
    if request.method == "GET":
        qs = SessionInvitation.objects.filter(invited_user=request.user).order_by("-created_at")[:50]
        return Response(
            [
                {
                    "id": str(i.id),
                    "kind": i.kind,
                    "status": i.status,
                    "message": i.message,
                    "expires_at": i.expires_at.isoformat() if i.expires_at else None,
                }
                for i in qs
            ]
        )

    if request.data.get("accept_id"):
        inv = get_object_or_404(SessionInvitation, pk=request.data["accept_id"])
        try:
            InvitationService().accept(inv, request.user)
        except SharingError as exc:
            return Response({"detail": str(exc), "code": exc.code}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"id": str(inv.id), "status": inv.status})

    invited_user = None
    if request.data.get("user_id"):
        invited_user = get_object_or_404(User, pk=request.data["user_id"])
    elif request.data.get("user_email"):
        invited_user = User.objects.filter(email__iexact=request.data["user_email"]).first()

    session = (
        get_object_or_404(RemoteDesktopSession, pk=request.data["session_id"])
        if request.data.get("session_id")
        else None
    )
    reservation = (
        get_object_or_404(AnalysisReservation, pk=request.data["reservation_id"])
        if request.data.get("reservation_id")
        else None
    )
    workspace = (
        get_object_or_404(AnalysisWorkspace, pk=request.data["workspace_id"])
        if request.data.get("workspace_id")
        else None
    )
    try:
        inv = InvitationService().invite(
            request.user,
            invited_user=invited_user,
            invited_email=request.data.get("user_email") or "",
            session=session,
            reservation=reservation,
            workspace=workspace,
            kind=(request.data.get("kind") or InvitationKind.COLLABORATOR).upper(),
            message=request.data.get("message") or "",
            expires_hours=int(request.data.get("expires_hours") or 72),
        )
    except SharingError as exc:
        return Response({"detail": str(exc), "code": exc.code}, status=status.HTTP_400_BAD_REQUEST)
    return Response({"id": str(inv.id)}, status=status.HTTP_201_CREATED)


@api_view(["POST", "GET"])
@permission_classes(_AUTH)
def assistance_collection(request):
    """POST/GET /api/v1/analysis/assistance/"""
    svc = AssistanceService()
    if request.method == "GET":
        qs = SessionAssistanceRequest.objects.select_related("requested_by", "assigned_to").order_by("-created_at")
        if not CanManageRemoteAnalysis().has_permission(request, None):
            qs = qs.filter(requested_by=request.user)
        status_filter = request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter.upper())
        return Response(
            [
                {
                    "id": str(r.id),
                    "subject": r.subject,
                    "description": r.description,
                    "status": r.status,
                    "priority": r.priority,
                    "requested_by": r.requested_by.email,
                    "assigned_to": getattr(r.assigned_to, "email", None),
                    "resolution": r.resolution,
                    "created_at": r.created_at.isoformat(),
                }
                for r in qs[:100]
            ]
        )

    action = (request.data.get("action") or "request").lower()
    if action == "request":
        session = (
            get_object_or_404(RemoteDesktopSession, pk=request.data["session_id"])
            if request.data.get("session_id")
            else None
        )
        reservation = (
            get_object_or_404(AnalysisReservation, pk=request.data["reservation_id"])
            if request.data.get("reservation_id")
            else None
        )
        subject = (request.data.get("subject") or "").strip()
        if not subject:
            return Response({"detail": "subject required"}, status=status.HTTP_400_BAD_REQUEST)
        row = svc.request_help(
            request.user,
            subject,
            request.data.get("description") or "",
            session=session,
            reservation=reservation,
            priority=(request.data.get("priority") or AssistancePriority.NORMAL).upper(),
        )
        return Response({"id": str(row.id), "status": row.status}, status=status.HTTP_201_CREATED)

    req = get_object_or_404(SessionAssistanceRequest, pk=request.data.get("request_id"))
    try:
        if action == "assign":
            op = get_object_or_404(User, pk=request.data.get("assignee_id") or request.user.pk)
            row = svc.assign(req, op, actor=request.user)
        elif action == "accept":
            row = svc.accept(req, request.user)
        elif action == "resolve":
            row = svc.resolve(req, request.user, resolution=request.data.get("resolution") or "")
        elif action == "close":
            row = svc.close(req, request.user)
        else:
            return Response({"detail": "Unknown action"}, status=status.HTTP_400_BAD_REQUEST)
    except AssistanceError as exc:
        return Response({"detail": str(exc), "code": exc.code}, status=status.HTTP_403_FORBIDDEN)
    return Response({"id": str(row.id), "status": row.status})


@api_view(["GET"])
@permission_classes(_AUTH)
def timeline_view(request):
    """GET /api/v1/analysis/timeline/"""
    if request.query_params.get("session_id"):
        session = get_object_or_404(RemoteDesktopSession, pk=request.query_params["session_id"])
        return Response(TimelineService().build_for_session(session))
    if request.query_params.get("reservation_id"):
        reservation = get_object_or_404(AnalysisReservation, pk=request.query_params["reservation_id"])
        return Response(TimelineService().build_for_reservation(reservation))
    return Response({"detail": "session_id or reservation_id required"}, status=status.HTTP_400_BAD_REQUEST)


@api_view(["GET", "POST"])
@permission_classes(_VIEW)
def announcements_collection(request):
    if request.method == "GET":
        rows = Announcement.objects.filter(active=True).order_by("-created_at")[:50]
        return Response(
            [{"id": str(a.id), "title": a.title, "body": a.body, "created_at": a.created_at.isoformat()} for a in rows]
        )
    if not CanManageRemoteAnalysis().has_permission(request, None):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    row = Announcement.objects.create(
        title=(request.data.get("title") or "")[:255],
        body=request.data.get("body") or "",
        created_by=request.user,
        active=True,
    )
    return Response({"id": str(row.id)}, status=status.HTTP_201_CREATED)


@api_view(["GET", "POST"])
@permission_classes(_AUTH)
def bookmarks_collection(request):
    if request.method == "GET":
        rows = Bookmark.objects.filter(user=request.user)[:50]
        return Response(
            [
                {"id": str(b.id), "label": b.label, "target_type": b.target_type, "target_id": b.target_id, "url": b.url}
                for b in rows
            ]
        )
    row = Bookmark.objects.create(
        user=request.user,
        label=(request.data.get("label") or "Bookmark")[:255],
        target_type=(request.data.get("target_type") or "url")[:64],
        target_id=str(request.data.get("target_id") or "")[:64],
        url=(request.data.get("url") or "")[:1024],
    )
    return Response({"id": str(row.id)}, status=status.HTTP_201_CREATED)


@api_view(["GET", "POST"])
@permission_classes(_AUTH)
def favorites_collection(request):
    from iic_booking.remote_analysis.models import AnalysisWorkstation

    if request.method == "GET":
        rows = FavoriteWorkstation.objects.filter(user=request.user).select_related("workstation")[:50]
        return Response(
            [{"id": str(f.id), "workstation_id": str(f.workstation_id), "hostname": f.workstation.hostname} for f in rows]
        )
    ws = get_object_or_404(AnalysisWorkstation, pk=request.data.get("workstation_id"))
    row, _ = FavoriteWorkstation.objects.get_or_create(user=request.user, workstation=ws)
    return Response({"id": str(row.id)}, status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes(_AUTH)
def recent_workspace_touch(request):
    workspace = get_object_or_404(AnalysisWorkspace, pk=request.data.get("workspace_id"))
    row, _ = RecentWorkspace.objects.update_or_create(user=request.user, workspace=workspace)
    return Response({"id": str(row.id), "last_accessed_at": row.last_accessed_at.isoformat()})

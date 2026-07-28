"""API views for Analysis Workspace & secure file exchange."""

from __future__ import annotations

from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, parser_classes
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.remote_analysis.authentication import RemoteAnalysisAgentUser
from iic_booking.remote_analysis.permissions import CanManageRemoteAnalysis, CanViewRemoteAnalysis, IsRemoteAnalysisAgent
from iic_booking.remote_analysis.scheduler_models import AnalysisReservation
from iic_booking.remote_analysis.workspace.permissions import can_access_workspace, can_write_workspace
from iic_booking.remote_analysis.workspace.serializers import (
    AnalysisWorkspaceSerializer,
    CreateWorkspaceSerializer,
    UploadMetaSerializer,
    WorkspaceAuditSerializer,
    WorkspaceFileSerializer,
    WorkspaceTransferSerializer,
    WorkspaceVersionSerializer,
)
from iic_booking.remote_analysis.workspace.storage import StorageError, StorageManager
from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService
from iic_booking.remote_analysis.workspace.transfer import TransferError, TransferManager
from iic_booking.remote_analysis.workspace_models import (
    AnalysisWorkspace,
    WorkspaceAudit,
    WorkspaceFile,
    WorkspaceTelemetry,
    WorkspaceTransfer,
)

_AUTH = [IsAuthenticated]
_VIEW = [IsAuthenticated, CanViewRemoteAnalysis]
_AGENT = [IsRemoteAnalysisAgent]


@api_view(["GET", "POST"])
@permission_classes(_AUTH)
def workspaces_collection(request):
    """GET/POST /api/v1/analysis/workspaces/"""
    if request.method == "GET":
        qs = AnalysisWorkspace.objects.select_related("user", "workstation", "reservation").prefetch_related("folders")
        if not CanManageRemoteAnalysis().has_permission(request, None):
            qs = qs.filter(user=request.user)
        status_filter = request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter.upper())
        archived = request.query_params.get("archived")
        if archived == "1":
            qs = qs.filter(archive_status="ARCHIVED")
        return Response(AnalysisWorkspaceSerializer(qs[:200], many=True).data)

    ser = CreateWorkspaceSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    reservation = get_object_or_404(AnalysisReservation, pk=ser.validated_data["reservation_id"])
    if reservation.user_id != request.user.pk and not CanManageRemoteAnalysis().has_permission(request, None):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    try:
        workspace = WorkspaceSyncService().ensure_for_reservation(reservation, actor=request.user)
    except StorageError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(AnalysisWorkspaceSerializer(workspace).data, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes(_AUTH)
def workspace_detail(request, workspace_id):
    """GET /api/v1/analysis/workspaces/{id}/"""
    workspace = get_object_or_404(
        AnalysisWorkspace.objects.select_related("user", "workstation", "reservation").prefetch_related("folders"),
        pk=workspace_id,
    )
    if not can_access_workspace(request.user, workspace):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    data = AnalysisWorkspaceSerializer(workspace).data
    data["recent_transfers"] = WorkspaceTransferSerializer(
        workspace.transfers.order_by("-created_at")[:20], many=True
    ).data
    return Response(data)


@api_view(["POST"])
@permission_classes(_AUTH)
@parser_classes([MultiPartParser, FormParser])
def workspace_upload(request, workspace_id):
    """POST /api/v1/analysis/workspaces/{id}/upload/"""
    workspace = get_object_or_404(AnalysisWorkspace, pk=workspace_id)
    if not can_write_workspace(request.user, workspace):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    uploaded = request.FILES.get("file") or request.FILES.get("upload")
    if not uploaded:
        return Response({"detail": "Missing file"}, status=status.HTTP_400_BAD_REQUEST)
    meta = UploadMetaSerializer(data=request.data)
    meta.is_valid(raise_exception=True)
    try:
        file_row = TransferManager().upload(
            workspace,
            uploaded,
            folder=meta.validated_data.get("folder") or "RawData",
            actor=request.user,
            expected_sha256=meta.validated_data.get("sha256") or "",
            override_quota=CanManageRemoteAnalysis().has_permission(request, None),
        )
    except (TransferError, StorageError) as exc:
        code = getattr(exc, "code", "error")
        return Response({"detail": str(exc), "code": code}, status=status.HTTP_400_BAD_REQUEST)
    return Response(WorkspaceFileSerializer(file_row).data, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes(_AUTH)
def workspace_download(request, workspace_id):
    """GET /api/v1/analysis/workspaces/{id}/download/?file_id= | ?zip=1"""
    workspace = get_object_or_404(AnalysisWorkspace, pk=workspace_id)
    if not can_access_workspace(request.user, workspace):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)

    if request.query_params.get("zip") in {"1", "true", "yes"}:
        files = WorkspaceFile.objects.filter(workspace=workspace, deleted=False, is_current=True)
        ids = request.query_params.getlist("file_id")
        if ids:
            files = files.filter(id__in=ids)
        return TransferManager().download_zip(workspace, files, actor=request.user)

    file_id = request.query_params.get("file_id")
    if not file_id:
        return Response({"detail": "file_id required"}, status=status.HTTP_400_BAD_REQUEST)
    file_row = get_object_or_404(WorkspaceFile, pk=file_id, workspace=workspace)
    try:
        return TransferManager().download_file(workspace, file_row, actor=request.user)
    except TransferError as exc:
        return Response({"detail": str(exc), "code": exc.code}, status=status.HTTP_400_BAD_REQUEST)


@api_view(["POST"])
@permission_classes(_AUTH)
def workspace_archive(request, workspace_id):
    """POST /api/v1/analysis/workspaces/{id}/archive/"""
    workspace = get_object_or_404(AnalysisWorkspace, pk=workspace_id)
    if not can_write_workspace(request.user, workspace) and not CanManageRemoteAnalysis().has_permission(request, None):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    try:
        StorageManager().archive(workspace, actor=request.user, note=request.data.get("note") or "")
    except StorageError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(AnalysisWorkspaceSerializer(workspace).data)


@api_view(["POST"])
@permission_classes(_AUTH)
def workspace_restore(request, workspace_id):
    """POST /api/v1/analysis/workspaces/{id}/restore/"""
    workspace = get_object_or_404(AnalysisWorkspace, pk=workspace_id)
    if not CanManageRemoteAnalysis().has_permission(request, None):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    try:
        StorageManager().restore(workspace, actor=request.user)
    except StorageError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(AnalysisWorkspaceSerializer(workspace).data)


@api_view(["GET"])
@permission_classes(_AUTH)
def workspace_files(request, workspace_id):
    """GET /api/v1/analysis/workspaces/{id}/files/"""
    workspace = get_object_or_404(AnalysisWorkspace, pk=workspace_id)
    if not can_access_workspace(request.user, workspace):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    qs = WorkspaceFile.objects.filter(workspace=workspace, deleted=False, is_current=True).select_related("folder", "uploaded_by")
    folder = request.query_params.get("folder")
    if folder:
        qs = qs.filter(relative_path__startswith=folder)
    return Response(WorkspaceFileSerializer(qs[:500], many=True).data)


@api_view(["POST"])
@permission_classes(_AUTH)
def workspace_sync(request, workspace_id):
    """POST /api/v1/analysis/workspaces/{id}/sync/"""
    workspace = get_object_or_404(AnalysisWorkspace, pk=workspace_id)
    if not can_write_workspace(request.user, workspace) and not CanManageRemoteAnalysis().has_permission(request, None):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    try:
        cmd = WorkspaceSyncService().issue_sync_command(workspace, actor=request.user)
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response({"command_id": str(cmd.id), "workspace": AnalysisWorkspaceSerializer(workspace).data})


@api_view(["GET"])
@permission_classes(_AUTH)
def workspace_file_versions(request, workspace_id, file_id):
    workspace = get_object_or_404(AnalysisWorkspace, pk=workspace_id)
    if not can_access_workspace(request.user, workspace):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    file_row = get_object_or_404(WorkspaceFile, pk=file_id, workspace=workspace)
    return Response(WorkspaceVersionSerializer(file_row.versions.all()[:50], many=True).data)


@api_view(["GET"])
@permission_classes(_VIEW)
def workspace_dashboard(request):
    from django.db.models import Avg, Count, Sum

    from iic_booking.remote_analysis.constants import WorkspaceStatus

    qs = AnalysisWorkspace.objects.all()
    by_status = {row["status"]: row["c"] for row in qs.values("status").annotate(c=Count("id"))}
    usage = qs.aggregate(total=Sum("current_usage_bytes"), avg=Avg("current_usage_bytes"))
    transfers = WorkspaceTransfer.objects.all()
    failed = transfers.filter(status="FAILED").count()
    total_t = transfers.count() or 1
    return Response(
        {
            "workspaces_total": qs.count(),
            "by_status": by_status,
            "active": qs.filter(
                status__in=[WorkspaceStatus.ACTIVE, WorkspaceStatus.READY, WorkspaceStatus.SYNCING]
            ).count(),
            "archived": qs.filter(status=WorkspaceStatus.ARCHIVED).count(),
            "storage_bytes": usage["total"] or 0,
            "average_workspace_bytes": usage["avg"] or 0,
            "transfer_failure_rate": failed / total_t,
            "recent_audits": WorkspaceAuditSerializer(
                WorkspaceAudit.objects.order_by("-created_at")[:20], many=True
            ).data,
            "transfer_queue": WorkspaceTransferSerializer(
                transfers.filter(status__in=["PENDING", "IN_PROGRESS"]).order_by("-created_at")[:20],
                many=True,
            ).data,
            "checked_at": timezone.now().isoformat(),
        }
    )


# --- Agent endpoints (Bearer agent token) ---


@api_view(["GET"])
@permission_classes(_AGENT)
def agent_workspace_manifest(request, workspace_id):
    """Agent pulls sync manifest for its assigned workstation only."""
    workspace = get_object_or_404(AnalysisWorkspace, pk=workspace_id)
    agent_ws = getattr(request.user, "workstation", None)
    if agent_ws is None or workspace.workstation_id != agent_ws.id:
        return Response({"detail": "Workspace not assigned to this agent"}, status=status.HTTP_403_FORBIDDEN)
    return Response(WorkspaceSyncService().build_manifest(workspace))


@api_view(["GET"])
@permission_classes(_AGENT)
def agent_file_content(request, workspace_id, file_id):
    workspace = get_object_or_404(AnalysisWorkspace, pk=workspace_id)
    agent_ws = getattr(request.user, "workstation", None)
    if agent_ws is None or workspace.workstation_id != agent_ws.id:
        return Response({"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)
    file_row = get_object_or_404(WorkspaceFile, pk=file_id, workspace=workspace, deleted=False)
    try:
        return TransferManager().download_file(workspace, file_row, actor=None)
    except TransferError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(["POST"])
@permission_classes(_AGENT)
@parser_classes([MultiPartParser, FormParser])
def agent_workspace_upload(request, workspace_id):
    workspace = get_object_or_404(AnalysisWorkspace, pk=workspace_id)
    agent_ws = getattr(request.user, "workstation", None)
    if agent_ws is None or workspace.workstation_id != agent_ws.id:
        return Response({"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)
    uploaded = request.FILES.get("file")
    if not uploaded:
        return Response({"detail": "Missing file"}, status=status.HTTP_400_BAD_REQUEST)
    folder = request.data.get("folder") or "Processed"
    sha = request.data.get("sha256") or ""
    try:
        file_row = TransferManager().upload(
            workspace,
            uploaded,
            folder=folder,
            actor=None,
            expected_sha256=sha,
            source="agent",
        )
    except (TransferError, StorageError) as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    WorkspaceSyncService().mark_synced(workspace, success=True, message="agent upload")
    return Response(WorkspaceFileSerializer(file_row).data, status=status.HTTP_201_CREATED)

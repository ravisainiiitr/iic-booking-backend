"""
My Research API (mounted at /api/v1/my-research/).

Every endpoint re-checks: feature flag -> IITR internal eligibility -> workspace role. Workspaces the
caller cannot access answer 404 (never 403) so ids cannot be probed. Only the owner may change
anything; viewers are strictly read-only. Archived workspaces reject all content changes.
"""

from __future__ import annotations

import base64
import binascii
import logging
import math
import mimetypes
import re
import uuid

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.equipment.models import Booking, EquipmentPublicationClaim

from . import file_policy, storage
from .access import (
    DISABLED_CODE,
    NOT_ELIGIBLE_CODE,
    NOT_ELIGIBLE_MESSAGE,
    WorkspaceAccess,
    accessible_workspace_ids,
    can_create_workspace,
    eligible_users,
    feature_enabled,
    is_eligible,
    pilot_emails,
    resolve_access,
)
from .models import (
    ActivityAction,
    FileStatus,
    MemberRole,
    ResearchActivity,
    ResearchFile,
    ResearchFolder,
    ResearchWorkspace,
    ResearchWorkspaceBooking,
    ResearchWorkspaceMember,
    ResearchWorkspacePublication,
    WorkspaceStatus,
)
from .services import (
    FolderError,
    UploadVerificationError,
    active_folders,
    annotate_booking_timing,
    annotate_folder_counts,
    annotate_workspace_stats,
    file_queryset,
    finalize_upload,
    folder_breadcrumbs,
    folder_depth,
    link_booking,
    mark_failed,
    notify_viewer_added,
    notify_viewer_removed,
    owner_usage_bytes,
    quota_error,
    record_activity,
    search_workspace,
    serialize_activity,
    serialize_booking_safe,
    serialize_file,
    serialize_folder,
    serialize_publication,
    serialize_workspace_card,
    sibling_name_taken,
    soft_delete_folder,
    user_summary,
    validate_folder_move,
)

logger = logging.getLogger(__name__)

TEXT_PREVIEW_BYTES = 256 * 1024
MAX_PARTS_PER_REQUEST = 100
S3_MAX_PARTS = 10000
S3_MIN_PART_SIZE = 5 * 1024**2
_CONTENT_TYPE_RE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]{1,64}/[A-Za-z0-9!#$&^_.+-]{1,100}$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")


# ---------------------------------------------------------------- helpers


def _error(message: str, http_status: int, code: str | None = None, **extra) -> Response:
    payload = {"error": message, **extra}
    if code:
        payload["code"] = code
    return Response(payload, status=http_status)


def _gate(request) -> Response | None:
    if not feature_enabled():
        return _error("My Research is not available.", status.HTTP_404_NOT_FOUND, DISABLED_CODE)
    if not is_eligible(request.user):
        return _error(NOT_ELIGIBLE_MESSAGE, status.HTTP_403_FORBIDDEN, NOT_ELIGIBLE_CODE)
    return None


def _not_found(what: str = "Workspace") -> Response:
    return _error(f"{what} not found.", status.HTTP_404_NOT_FOUND, "not_found")


def _access(request, workspace_id, *, owner: bool = False, edit: bool = False):
    """Returns (access, None) or (None, error_response)."""
    denied = _gate(request)
    if denied:
        return None, denied
    access = resolve_access(request.user, workspace_id)
    if access is None:
        return None, _not_found()
    if (owner or edit) and not access.is_owner:
        return None, _error(
            "You have read-only access to this workspace.", status.HTTP_403_FORBIDDEN, "read_only"
        )
    if edit and access.workspace.is_archived:
        return None, _error(
            "This workspace is archived. Restore it to make changes.", status.HTTP_409_CONFLICT, "workspace_archived"
        )
    return access, None


def _parse_uuid(value):
    if value in (None, "", "root", "null"):
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return False


def _page_params(request, default: int = 50, maximum: int = 200) -> tuple[int, int]:
    try:
        page = max(int(request.query_params.get("page", 1)), 1)
    except (TypeError, ValueError):
        page = 1
    try:
        size = int(request.query_params.get("page_size", default))
    except (TypeError, ValueError):
        size = default
    return page, min(max(size, 1), maximum)


def _paginate(queryset, page: int, size: int):
    total = queryset.count()
    start = (page - 1) * size
    items = list(queryset[start : start + size])
    return items, {"page": page, "page_size": size, "total": total, "has_next": start + size < total}


def _permissions(access: WorkspaceAccess) -> dict:
    return {
        "read_only": not access.can_edit,
        "can_edit": access.can_edit,
        "can_share": access.is_owner,
        "can_upload": access.can_edit,
        "can_archive": access.is_owner and not access.workspace.is_archived,
        "can_restore": access.is_owner and access.workspace.is_archived,
    }


def _load_folder(access: WorkspaceAccess, folder_id) -> ResearchFolder | None:
    parsed = _parse_uuid(folder_id)
    if not parsed:
        return None
    return active_folders(access.workspace).select_related("parent").filter(pk=parsed).first()


def _resolve_parent(access: WorkspaceAccess, raw):
    """(folder_or_None, error_response). Missing/'root' means the workspace root."""
    parsed = _parse_uuid(raw)
    if parsed is None:
        return None, None
    if parsed is False:
        return None, _error("Invalid folder id.", status.HTTP_400_BAD_REQUEST)
    folder = active_folders(access.workspace).filter(pk=parsed).first()
    if folder is None:
        return None, _not_found("Folder")
    return folder, None


def _workspace_for_folder(request, folder_id, **kwargs):
    denied = _gate(request)
    if denied:
        return None, None, denied
    workspace_id = ResearchFolder.objects.filter(pk=folder_id, deleted_at__isnull=True).values_list(
        "workspace_id", flat=True
    ).first()
    if workspace_id is None:
        return None, None, _not_found("Folder")
    access, error = _access(request, workspace_id, **kwargs)
    if error:
        return None, None, (error if error.status_code != 404 else _not_found("Folder"))
    folder = _load_folder(access, folder_id)
    if folder is None:
        return None, None, _not_found("Folder")
    return access, folder, None


def _workspace_for_file(request, file_id, *, statuses=(FileStatus.AVAILABLE,), **kwargs):
    denied = _gate(request)
    if denied:
        return None, None, denied
    research_file = file_queryset().select_related("workspace", "folder").filter(pk=file_id, status__in=statuses).first()
    if research_file is None:
        return None, None, _not_found("File")
    access, error = _access(request, research_file.workspace_id, **kwargs)
    if error:
        return None, None, (error if error.status_code != 404 else _not_found("File"))
    return access, research_file, None


# ---------------------------------------------------------------- bootstrap / home


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def bootstrap(request):
    """Always 200 for signed-in users so the UI can decide whether to show My Research."""
    enabled = feature_enabled()
    eligible = is_eligible(request.user)
    data = {"enabled": enabled, "eligible": eligible, "available": enabled and eligible}
    if enabled and eligible:
        data.update(
            {
                "can_create": can_create_workspace(request.user),
                "pilot": bool(pilot_emails()),
                "storage_configured": storage.storage_configured(),
                "limits": {
                    "max_file_size": int(settings.MY_RESEARCH_MAX_FILE_SIZE),
                    "multipart_threshold": int(settings.MY_RESEARCH_MULTIPART_THRESHOLD),
                    "download_url_expiry_seconds": int(settings.MY_RESEARCH_DOWNLOAD_URL_EXPIRY_SECONDS),
                    "user_storage_quota": int(settings.MY_RESEARCH_USER_STORAGE_QUOTA or 0),
                    "workspace_storage_quota": int(settings.MY_RESEARCH_WORKSPACE_STORAGE_QUOTA or 0),
                },
            }
        )
    return Response(data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def home(request):
    denied = _gate(request)
    if denied:
        return denied
    user = request.user
    owned = annotate_workspace_stats(
        ResearchWorkspace.objects.filter(owner=user).select_related("owner", "owner__department")
    ).order_by("status", "-last_activity_at")
    shared_ids = ResearchWorkspaceMember.objects.filter(
        user=user, role=MemberRole.VIEWER, revoked_at__isnull=True
    ).values_list("workspace_id", flat=True)
    shared = annotate_workspace_stats(
        ResearchWorkspace.objects.filter(pk__in=shared_ids).select_related("owner", "owner__department")
    ).order_by("-last_activity_at")
    recent = (
        ResearchActivity.objects.filter(workspace_id__in=accessible_workspace_ids(user))
        .select_related("actor", "actor__department", "workspace")
        .order_by("-created_at")[:15]
    )
    return Response(
        {
            "my_workspaces": [serialize_workspace_card(w, MemberRole.OWNER) for w in owned],
            "shared_with_me": [serialize_workspace_card(w, MemberRole.VIEWER) for w in shared],
            "recent_activity": [serialize_activity(a, include_workspace=True) for a in recent],
            "can_create": can_create_workspace(user),
            "storage": {
                "used_bytes": owner_usage_bytes(user),
                "quota_bytes": int(settings.MY_RESEARCH_USER_STORAGE_QUOTA or 0),
            },
        }
    )


# ---------------------------------------------------------------- workspaces


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def workspaces_collection(request):
    denied = _gate(request)
    if denied:
        return denied
    user = request.user
    if request.method == "GET":
        owned = ResearchWorkspace.objects.filter(owner=user, status=WorkspaceStatus.ACTIVE).order_by("name")
        linked = set()
        booking_id = request.query_params.get("booking")
        if booking_id and str(booking_id).isdigit():
            linked = set(
                ResearchWorkspaceBooking.objects.filter(
                    booking_id=int(booking_id), workspace__owner=user
                ).values_list("workspace_id", flat=True)
            )
        return Response(
            {
                "results": [
                    {"id": str(w.pk), "name": w.name, "booking_linked": w.pk in linked} for w in owned
                ]
            }
        )

    if not can_create_workspace(user):
        return _error(
            "Creating workspaces is limited to the My Research pilot group right now.",
            status.HTTP_403_FORBIDDEN,
            "my_research_pilot_only",
        )
    name = file_policy.strip_control_chars(request.data.get("name")).strip()
    description = file_policy.strip_control_chars(request.data.get("description"), multiline=True).strip()
    if not name:
        return _error("Workspace name is required.", status.HTTP_400_BAD_REQUEST, field_errors={"name": "Required"})
    if len(name) > 200:
        return _error("Workspace name can be at most 200 characters.", status.HTTP_400_BAD_REQUEST)
    if len(description) > 5000:
        return _error("Description can be at most 5000 characters.", status.HTTP_400_BAD_REQUEST)
    workspace = ResearchWorkspace.objects.create(owner=user, name=name, description=description)
    ResearchWorkspaceMember.objects.create(workspace=workspace, user=user, role=MemberRole.OWNER, added_by=user)
    record_activity(workspace, user, ActivityAction.WORKSPACE_CREATED, target_type="workspace", target_id=workspace.pk, target_label=name)
    workspace = annotate_workspace_stats(ResearchWorkspace.objects.filter(pk=workspace.pk).select_related("owner")).get()
    return Response(serialize_workspace_card(workspace, MemberRole.OWNER), status=status.HTTP_201_CREATED)


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def workspace_detail(request, workspace_id):
    access, error = _access(request, workspace_id, edit=request.method == "PATCH")
    if error:
        return error
    workspace = access.workspace
    if request.method == "PATCH":
        changes = {}
        if "name" in request.data:
            name = file_policy.strip_control_chars(request.data.get("name")).strip()
            if not name or len(name) > 200:
                return _error("Workspace name must be 1–200 characters.", status.HTTP_400_BAD_REQUEST)
            changes["name"] = name
        if "description" in request.data:
            description = file_policy.strip_control_chars(request.data.get("description"), multiline=True).strip()
            if len(description) > 5000:
                return _error("Description can be at most 5000 characters.", status.HTTP_400_BAD_REQUEST)
            changes["description"] = description
        if changes:
            for field, value in changes.items():
                setattr(workspace, field, value)
            workspace.save(update_fields=[*changes.keys(), "updated_at"])
            record_activity(
                workspace, request.user, ActivityAction.WORKSPACE_UPDATED,
                target_type="workspace", target_id=workspace.pk, target_label=workspace.name,
                details={"fields": sorted(changes.keys())},
            )
    annotated = annotate_workspace_stats(
        ResearchWorkspace.objects.filter(pk=workspace.pk).select_related("owner", "owner__department")
    ).get()
    data = serialize_workspace_card(annotated, access.role)
    data["permissions"] = _permissions(access)
    return Response(data)


def _set_archived(request, workspace_id, archive: bool):
    access, error = _access(request, workspace_id, owner=True)
    if error:
        return error
    workspace = access.workspace
    if archive == workspace.is_archived:
        return _error(
            "Workspace is already archived." if archive else "Workspace is not archived.",
            status.HTTP_409_CONFLICT,
        )
    workspace.status = WorkspaceStatus.ARCHIVED if archive else WorkspaceStatus.ACTIVE
    workspace.archived_at = timezone.now() if archive else None
    workspace.save(update_fields=["status", "archived_at", "updated_at"])
    record_activity(
        workspace, request.user,
        ActivityAction.WORKSPACE_ARCHIVED if archive else ActivityAction.WORKSPACE_RESTORED,
        target_type="workspace", target_id=workspace.pk, target_label=workspace.name,
    )
    access = WorkspaceAccess(workspace, access.role)
    return Response({"status": workspace.status, "archived_at": workspace.archived_at, "permissions": _permissions(access)})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def workspace_archive(request, workspace_id):
    return _set_archived(request, workspace_id, True)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def workspace_restore(request, workspace_id):
    return _set_archived(request, workspace_id, False)


# ---------------------------------------------------------------- folders


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def folders_collection(request, workspace_id):
    access, error = _access(request, workspace_id, edit=request.method == "POST")
    if error:
        return error
    workspace = access.workspace

    if request.method == "GET":
        parent, error = _resolve_parent(access, request.query_params.get("parent"))
        if error:
            return error
        page, size = _page_params(request, default=200, maximum=500)
        qs = annotate_folder_counts(active_folders(workspace).filter(parent=parent)).order_by("name")
        items, meta = _paginate(qs, page, size)
        return Response(
            {
                "parent": serialize_folder(parent) if parent else None,
                "breadcrumbs": folder_breadcrumbs(parent),
                "results": [serialize_folder(f) for f in items],
                "pagination": meta,
            }
        )

    parent, error = _resolve_parent(access, request.data.get("parent_id"))
    if error:
        return error
    try:
        name = file_policy.clean_folder_name(request.data.get("name"))
    except file_policy.InvalidFilename as exc:
        return _error(str(exc), status.HTTP_400_BAD_REQUEST)
    max_depth = int(settings.MY_RESEARCH_MAX_FOLDER_DEPTH)
    if folder_depth(parent) + 1 > max_depth:
        return _error(f"Folders can be nested at most {max_depth} levels deep.", status.HTTP_400_BAD_REQUEST)
    if sibling_name_taken(workspace, parent, name):
        return _error("A folder with this name already exists here.", status.HTTP_409_CONFLICT, "duplicate_name")
    try:
        with transaction.atomic():
            folder = ResearchFolder.objects.create(workspace=workspace, parent=parent, name=name, created_by=request.user)
    except IntegrityError:
        return _error("A folder with this name already exists here.", status.HTTP_409_CONFLICT, "duplicate_name")
    record_activity(
        workspace, request.user, ActivityAction.FOLDER_CREATED,
        target_type="folder", target_id=folder.pk, target_label=name,
        details={"parent_id": str(parent.pk) if parent else None},
    )
    return Response(serialize_folder(folder), status=status.HTTP_201_CREATED)


@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def folder_detail(request, folder_id):
    access, folder, error = _workspace_for_folder(request, folder_id, edit=request.method != "GET")
    if error:
        return error
    workspace = access.workspace

    if request.method == "GET":
        data = serialize_folder(annotate_folder_counts(ResearchFolder.objects.filter(pk=folder.pk)).get())
        data["breadcrumbs"] = folder_breadcrumbs(folder)
        return Response(data)

    if request.method == "DELETE":
        counts = soft_delete_folder(folder, request.user)
        record_activity(
            workspace, request.user, ActivityAction.FOLDER_DELETED,
            target_type="folder", target_id=folder.pk, target_label=folder.name, details=counts,
        )
        return Response({"deleted": True, **counts})

    new_parent = folder.parent
    moving = "parent_id" in request.data
    if moving:
        new_parent, error = _resolve_parent(access, request.data.get("parent_id"))
        if error:
            return error
        try:
            validate_folder_move(folder, new_parent)
        except FolderError as exc:
            return _error(str(exc), status.HTTP_400_BAD_REQUEST, "invalid_move")
    new_name = folder.name
    if "name" in request.data:
        try:
            new_name = file_policy.clean_folder_name(request.data.get("name"))
        except file_policy.InvalidFilename as exc:
            return _error(str(exc), status.HTTP_400_BAD_REQUEST)
    if sibling_name_taken(workspace, new_parent, new_name, exclude_id=folder.pk):
        return _error("A folder with this name already exists there.", status.HTTP_409_CONFLICT, "duplicate_name")

    old_name, old_parent_id = folder.name, folder.parent_id
    folder.name = new_name
    folder.parent = new_parent
    try:
        with transaction.atomic():
            folder.save(update_fields=["name", "parent", "updated_at"])
    except IntegrityError:
        return _error("A folder with this name already exists there.", status.HTTP_409_CONFLICT, "duplicate_name")
    if new_name != old_name:
        record_activity(
            workspace, request.user, ActivityAction.FOLDER_RENAMED,
            target_type="folder", target_id=folder.pk, target_label=new_name, details={"from": old_name},
        )
    if moving and folder.parent_id != old_parent_id:
        record_activity(
            workspace, request.user, ActivityAction.FOLDER_MOVED,
            target_type="folder", target_id=folder.pk, target_label=new_name,
            details={"to": [c["name"] for c in folder_breadcrumbs(new_parent)]},
        )
    data = serialize_folder(annotate_folder_counts(ResearchFolder.objects.filter(pk=folder.pk)).get())
    data["breadcrumbs"] = folder_breadcrumbs(folder)
    return Response(data)


# ---------------------------------------------------------------- files


FILE_SORTS = {
    "name": ("display_name",),
    "-name": ("-display_name",),
    "newest": ("-uploaded_at", "display_name"),
    "oldest": ("uploaded_at", "display_name"),
    "size": ("-size_bytes", "display_name"),
}


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def files_collection(request, workspace_id):
    access, error = _access(request, workspace_id)
    if error:
        return error
    qs = file_queryset().filter(workspace=access.workspace, status=FileStatus.AVAILABLE)
    booking_id = request.query_params.get("booking")
    if booking_id:
        if not str(booking_id).isdigit():
            return _error("Invalid booking id.", status.HTTP_400_BAD_REQUEST)
        qs = qs.filter(booking_id=int(booking_id))
    else:
        folder, error = _resolve_parent(access, request.query_params.get("folder"))
        if error:
            return error
        qs = qs.filter(folder=folder)
    qs = qs.order_by(*FILE_SORTS.get(request.query_params.get("sort") or "name", FILE_SORTS["name"]))
    page, size = _page_params(request)
    items, meta = _paginate(qs, page, size)
    return Response({"results": [serialize_file(f) for f in items], "pagination": meta})


@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def file_detail(request, file_id):
    access, research_file, error = _workspace_for_file(request, file_id, edit=request.method != "GET")
    if error:
        return error
    workspace = access.workspace

    if request.method == "GET":
        data = serialize_file(research_file)
        data["folder_path"] = folder_breadcrumbs(research_file.folder)
        return Response(data)

    if request.method == "DELETE":
        research_file.status = FileStatus.DELETED
        research_file.deleted_at = timezone.now()
        research_file.deleted_by = request.user
        research_file.save(update_fields=["status", "deleted_at", "deleted_by", "updated_at"])
        record_activity(
            workspace, request.user, ActivityAction.FILE_DELETED,
            target_type="file", target_id=research_file.pk, target_label=research_file.display_name,
        )
        return Response({"deleted": True})

    updates = []
    old_name = research_file.display_name
    old_folder_id = research_file.folder_id
    if "name" in request.data:
        try:
            name = file_policy.clean_display_name(request.data.get("name"))
        except file_policy.InvalidFilename as exc:
            return _error(str(exc), status.HTTP_400_BAD_REQUEST)
        if file_policy.is_blocked_name(name):
            return _error("That file extension is not allowed.", status.HTTP_400_BAD_REQUEST, "blocked_extension")
        research_file.display_name = name
        updates.append("display_name")
    if "folder_id" in request.data:
        folder, error = _resolve_parent(access, request.data.get("folder_id"))
        if error:
            return error
        research_file.folder = folder
        updates.append("folder")
    if "booking_id" in request.data:
        raw = request.data.get("booking_id")
        if raw in (None, ""):
            research_file.booking = None
        else:
            link = ResearchWorkspaceBooking.objects.filter(
                workspace=workspace, booking_id=raw if str(raw).isdigit() else -1
            ).select_related("booking").first()
            if link is None:
                return _error("Associate the booking with this workspace first.", status.HTTP_400_BAD_REQUEST)
            research_file.booking = link.booking
        updates.append("booking")
    if updates:
        research_file.save(update_fields=[*updates, "updated_at"])
    if research_file.display_name != old_name:
        record_activity(
            workspace, request.user, ActivityAction.FILE_RENAMED,
            target_type="file", target_id=research_file.pk, target_label=research_file.display_name,
            details={"from": old_name},
        )
    if research_file.folder_id != old_folder_id:
        record_activity(
            workspace, request.user, ActivityAction.FILE_MOVED,
            target_type="file", target_id=research_file.pk, target_label=research_file.display_name,
            details={"to": [c["name"] for c in folder_breadcrumbs(research_file.folder)]},
        )
    research_file = file_queryset().select_related("folder").get(pk=research_file.pk)
    data = serialize_file(research_file)
    data["folder_path"] = folder_breadcrumbs(research_file.folder)
    return Response(data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def file_download(request, file_id):
    access, research_file, error = _workspace_for_file(request, file_id)
    if error:
        return error
    requested = "inline" if request.data.get("disposition") == "inline" else "attachment"
    disposition, content_type = file_policy.serving_content_type(research_file.detected_type, requested)
    expires_in = int(settings.MY_RESEARCH_DOWNLOAD_URL_EXPIRY_SECONDS)
    try:
        url = storage.presign_get(
            research_file.storage_key,
            filename=research_file.display_name,
            disposition=disposition,
            content_type=content_type,
            expires_in=expires_in,
        )
    except storage.ResearchStorageError:
        logger.exception("my_research presign_get failed file=%s", research_file.pk)
        return _error("Storage is temporarily unavailable. Please try again.", status.HTTP_503_SERVICE_UNAVAILABLE)
    logger.info(
        "my_research download issued file=%s workspace=%s user=%s role=%s disposition=%s",
        research_file.pk, access.workspace.pk, request.user.pk, access.role, disposition,
    )
    return Response({"url": url, "expires_in": expires_in, "disposition": disposition, "content_type": content_type})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def file_preview(request, file_id):
    """Text/CSV preview served as JSON (first 256 KB) so no cross-origin S3 read is needed."""
    access, research_file, error = _workspace_for_file(request, file_id)
    if error:
        return error
    kind = file_policy.preview_kind(research_file.detected_type, research_file.display_name)
    if kind not in {"text", "csv"}:
        return _error("This file type has no text preview.", status.HTTP_400_BAD_REQUEST, "no_text_preview")
    try:
        head = storage.read_prefix(research_file.storage_key, TEXT_PREVIEW_BYTES)
    except storage.ResearchStorageError:
        logger.exception("my_research preview read failed file=%s", research_file.pk)
        return _error("Storage is temporarily unavailable. Please try again.", status.HTTP_503_SERVICE_UNAVAILABLE)
    return Response(
        {
            "kind": kind,
            "content": head.decode("utf-8", errors="replace"),
            "truncated": research_file.size_bytes > TEXT_PREVIEW_BYTES,
            "size_bytes": research_file.size_bytes,
        }
    )


# ---------------------------------------------------------------- uploads


def _declared_content_type(raw, filename: str) -> str:
    value = str(raw or "").strip().lower()
    if value and _CONTENT_TYPE_RE.match(value):
        return value
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _multipart_part_size(size: int) -> int:
    part = max(int(settings.MY_RESEARCH_MULTIPART_PART_SIZE), S3_MIN_PART_SIZE)
    if math.ceil(size / part) > S3_MAX_PARTS:
        mib = 1024**2
        part = math.ceil(size / S3_MAX_PARTS / mib) * mib
    return part


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def upload_initiate(request, workspace_id):
    access, error = _access(request, workspace_id, edit=True)
    if error:
        return error
    workspace = access.workspace
    if not storage.storage_configured():
        return _error("Research storage is not configured.", status.HTTP_503_SERVICE_UNAVAILABLE, "storage_unavailable")

    try:
        display_name = file_policy.clean_display_name(request.data.get("filename"))
    except file_policy.InvalidFilename as exc:
        return _error(str(exc), status.HTTP_400_BAD_REQUEST)
    if file_policy.is_blocked_name(display_name):
        return _error(
            "Executable and installer files cannot be stored in My Research.",
            status.HTTP_400_BAD_REQUEST,
            "blocked_extension",
        )
    try:
        size = int(request.data.get("size"))
    except (TypeError, ValueError):
        return _error("File size is required.", status.HTTP_400_BAD_REQUEST)
    max_size = int(settings.MY_RESEARCH_MAX_FILE_SIZE)
    if size < 0:
        return _error("Invalid file size.", status.HTTP_400_BAD_REQUEST)
    if size > max_size:
        return _error(
            f"Files can be at most {max_size // (1024**2)} MB.", status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "file_too_large"
        )

    folder, error = _resolve_parent(access, request.data.get("folder_id"))
    if error:
        return error

    booking = None
    raw_booking = request.data.get("booking_id")
    if raw_booking not in (None, ""):
        booking = (
            Booking.objects.select_related("equipment")
            .filter(booking_id=raw_booking if str(raw_booking).isdigit() else -1, user_id=workspace.owner_id)
            .first()
        )
        if booking is None:
            return _not_found("Booking")

    pending = ResearchFile.objects.filter(uploaded_by=request.user, status=FileStatus.PENDING_UPLOAD).count()
    if pending >= int(settings.MY_RESEARCH_MAX_PENDING_UPLOADS_PER_USER):
        return _error(
            "Too many uploads in progress. Finish or cancel some first.",
            status.HTTP_429_TOO_MANY_REQUESTS,
            "too_many_pending_uploads",
        )
    quota_message = quota_error(workspace, size)
    if quota_message:
        return _error(quota_message, status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "quota_exceeded")

    multipart = size >= int(settings.MY_RESEARCH_MULTIPART_THRESHOLD)
    checksum_b64 = ""
    raw_sha = str(request.data.get("sha256") or "").strip()
    if raw_sha and not multipart:
        if not _SHA256_HEX_RE.match(raw_sha):
            return _error("Invalid SHA-256 checksum.", status.HTTP_400_BAD_REQUEST)
        checksum_b64 = base64.b64encode(binascii.unhexlify(raw_sha)).decode("ascii")

    content_type = _declared_content_type(request.data.get("content_type"), display_name)
    file_id = uuid.uuid4()
    key = storage.build_object_key(workspace.pk, file_id, file_policy.storage_safe_name(display_name))
    research_file = ResearchFile.objects.create(
        id=file_id,
        workspace=workspace,
        folder=folder,
        booking=booking,
        original_name=file_policy.strip_control_chars(request.data.get("filename") or display_name)[:255],
        display_name=display_name,
        storage_key=key,
        declared_content_type=content_type,
        size_bytes=size,
        checksum_sha256=checksum_b64,
        uploaded_by=request.user,
    )
    if booking is not None:
        link_booking(workspace, booking, request.user)

    expires_in = int(settings.MY_RESEARCH_UPLOAD_URL_EXPIRY_SECONDS)
    try:
        if multipart:
            upload_id = storage.create_multipart_upload(key, content_type=content_type)
            research_file.multipart_upload_id = upload_id
            research_file.save(update_fields=["multipart_upload_id", "updated_at"])
            part_size = _multipart_part_size(size)
            upload = {
                "mode": "multipart",
                "part_size": part_size,
                "part_count": max(math.ceil(size / part_size), 1),
                "expires_in": expires_in,
            }
        else:
            url, headers = storage.presign_put(
                key, content_type=content_type, expires_in=expires_in, checksum_sha256_b64=checksum_b64
            )
            upload = {"mode": "single", "method": "PUT", "url": url, "headers": headers, "expires_in": expires_in}
    except storage.ResearchStorageError:
        logger.exception("my_research upload initiate failed file=%s", research_file.pk)
        mark_failed(research_file, "Storage was unavailable when the upload started.", delete_object=False)
        return _error("Storage is temporarily unavailable. Please try again.", status.HTTP_503_SERVICE_UNAVAILABLE)

    return Response({"file": serialize_file(research_file), "upload": upload}, status=status.HTTP_201_CREATED)


def _pending_upload(request, file_id):
    """Pending file the caller started, in a workspace they still own. (file, error)."""
    access, research_file, error = _workspace_for_file(
        request, file_id, statuses=(FileStatus.PENDING_UPLOAD,), owner=True
    )
    if error:
        return None, error
    if research_file.uploaded_by_id != request.user.pk:
        return None, _not_found("Upload")
    return research_file, None


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def upload_parts(request, file_id):
    research_file, error = _pending_upload(request, file_id)
    if error:
        return error
    if not research_file.multipart_upload_id:
        return _error("This upload does not use multipart transfer.", status.HTTP_400_BAD_REQUEST)
    raw = request.data.get("part_numbers") or []
    if not isinstance(raw, list) or not raw or len(raw) > MAX_PARTS_PER_REQUEST:
        return _error(f"Request between 1 and {MAX_PARTS_PER_REQUEST} parts at a time.", status.HTTP_400_BAD_REQUEST)
    try:
        numbers = sorted({int(n) for n in raw})
    except (TypeError, ValueError):
        return _error("Invalid part numbers.", status.HTTP_400_BAD_REQUEST)
    if numbers[0] < 1 or numbers[-1] > S3_MAX_PARTS:
        return _error("Invalid part numbers.", status.HTTP_400_BAD_REQUEST)
    expires_in = int(settings.MY_RESEARCH_UPLOAD_URL_EXPIRY_SECONDS)
    try:
        parts = [
            {
                "part_number": n,
                "url": storage.presign_upload_part(
                    research_file.storage_key,
                    upload_id=research_file.multipart_upload_id,
                    part_number=n,
                    expires_in=expires_in,
                ),
            }
            for n in numbers
        ]
    except storage.ResearchStorageError:
        logger.exception("my_research part presign failed file=%s", research_file.pk)
        return _error("Storage is temporarily unavailable. Please try again.", status.HTTP_503_SERVICE_UNAVAILABLE)
    return Response({"parts": parts, "expires_in": expires_in})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def upload_complete(request, file_id):
    research_file, error = _pending_upload(request, file_id)
    if error:
        return error
    if research_file.multipart_upload_id:
        raw_parts = request.data.get("parts") or []
        if not isinstance(raw_parts, list) or not raw_parts or len(raw_parts) > S3_MAX_PARTS:
            return _error("The list of uploaded parts is required.", status.HTTP_400_BAD_REQUEST)
        try:
            parts = sorted(
                ({"part_number": int(p["part_number"]), "etag": str(p["etag"])} for p in raw_parts),
                key=lambda p: p["part_number"],
            )
        except (TypeError, ValueError, KeyError):
            return _error("Invalid parts list.", status.HTTP_400_BAD_REQUEST)
        if [p["part_number"] for p in parts] != list(range(1, len(parts) + 1)):
            return _error("Parts must be numbered 1..N without gaps.", status.HTTP_400_BAD_REQUEST)
        try:
            storage.complete_multipart_upload(
                research_file.storage_key, upload_id=research_file.multipart_upload_id, parts=parts
            )
        except storage.ObjectNotFound:
            mark_failed(research_file, "The multipart upload no longer exists.", delete_object=False)
            return _error("The upload expired. Please upload the file again.", status.HTTP_409_CONFLICT, "upload_expired")
        except storage.ResearchStorageError:
            logger.exception("my_research complete_multipart failed file=%s", research_file.pk)
            return _error(
                "Could not finish the upload. Please retry.", status.HTTP_503_SERVICE_UNAVAILABLE, "storage_unavailable"
            )
    try:
        research_file = finalize_upload(research_file, request.user)
    except storage.ObjectNotFound:
        return _error(
            "The file has not reached storage yet. Retry once the transfer finishes.",
            status.HTTP_409_CONFLICT,
            "upload_not_found",
        )
    except UploadVerificationError as exc:
        return _error(str(exc), status.HTTP_422_UNPROCESSABLE_ENTITY, "upload_rejected")
    except storage.ResearchStorageError:
        logger.exception("my_research finalize failed file=%s", research_file.pk)
        return _error(
            "Could not verify the upload. Please retry.", status.HTTP_503_SERVICE_UNAVAILABLE, "storage_unavailable"
        )
    research_file = file_queryset().get(pk=research_file.pk)
    return Response(serialize_file(research_file))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def upload_abort(request, file_id):
    research_file, error = _pending_upload(request, file_id)
    if error:
        return error
    try:
        if research_file.multipart_upload_id:
            storage.abort_multipart_upload(research_file.storage_key, upload_id=research_file.multipart_upload_id)
        storage.delete_object(research_file.storage_key)
    except storage.ResearchStorageError:
        logger.exception("my_research abort cleanup failed file=%s", research_file.pk)
    mark_failed(research_file, "Upload cancelled.", delete_object=False)
    return Response({"cancelled": True})


# ---------------------------------------------------------------- bookings


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def bookings_collection(request, workspace_id):
    access, error = _access(request, workspace_id, edit=request.method == "POST")
    if error:
        return error
    workspace = access.workspace

    if request.method == "GET":
        booking_ids = ResearchWorkspaceBooking.objects.filter(workspace=workspace).values_list("booking_id", flat=True)
        qs = annotate_booking_timing(Booking.objects.filter(booking_id__in=booking_ids)).order_by(
            "-first_slot_at", "-booking_id"
        )
        page, size = _page_params(request)
        items, meta = _paginate(qs, page, size)
        file_counts = dict(
            ResearchFile.objects.filter(
                workspace=workspace, status=FileStatus.AVAILABLE, booking_id__in=[b.booking_id for b in items]
            )
            .values("booking_id")
            .annotate(c=Count("pk"))
            .values_list("booking_id", "c")
        )
        results = []
        for booking in items:
            row = serialize_booking_safe(booking)
            row["file_count"] = file_counts.get(booking.booking_id, 0)
            results.append(row)
        return Response({"results": results, "pagination": meta})

    raw_ids = request.data.get("booking_ids")
    if raw_ids is None and request.data.get("booking_id") is not None:
        raw_ids = [request.data.get("booking_id")]
    if not isinstance(raw_ids, list) or not raw_ids or len(raw_ids) > 50:
        return _error("Provide between 1 and 50 booking ids.", status.HTTP_400_BAD_REQUEST)
    wanted = {int(b) for b in raw_ids if str(b).isdigit()}
    owned = {
        b.booking_id: b
        for b in Booking.objects.select_related("equipment").filter(booking_id__in=wanted, user_id=workspace.owner_id)
    }
    linked, already = [], []
    for booking_id in sorted(owned):
        _, created = link_booking(workspace, owned[booking_id], request.user)
        (linked if created else already).append(booking_id)
    rejected = sorted(wanted - set(owned)) + [b for b in raw_ids if not str(b).isdigit()]
    if not linked and not already:
        return _error("Booking not found.", status.HTTP_404_NOT_FOUND, "not_found", rejected=rejected)
    return Response({"linked": linked, "already_linked": already, "rejected": rejected})


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def booking_unlink(request, workspace_id, booking_id):
    access, error = _access(request, workspace_id, edit=True)
    if error:
        return error
    link = ResearchWorkspaceBooking.objects.filter(workspace=access.workspace, booking_id=booking_id).select_related(
        "booking", "booking__equipment"
    ).first()
    if link is None:
        return _not_found("Booking")
    label = f"{link.booking.equipment.name} · {serialize_booking_safe(link.booking)['display_id']}"
    link.delete()
    record_activity(
        access.workspace, request.user, ActivityAction.BOOKING_UNLINKED,
        target_type="booking", target_id=booking_id, target_label=label,
    )
    return Response({"removed": True})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def linkable_bookings(request, workspace_id):
    access, error = _access(request, workspace_id, owner=True)
    if error:
        return error
    linked = ResearchWorkspaceBooking.objects.filter(workspace=access.workspace).values_list("booking_id", flat=True)
    qs = Booking.objects.filter(user=request.user).exclude(booking_id__in=linked)
    q = file_policy.strip_control_chars(request.query_params.get("q")).strip()
    if q:
        match = Q(equipment__name__icontains=q) | Q(equipment__code__icontains=q) | Q(virtual_booking_id__icontains=q)
        if q.isdigit():
            match |= Q(booking_id=int(q))
        qs = qs.filter(match)
    qs = annotate_booking_timing(qs).order_by("-created_at")[:50]
    return Response({"results": [serialize_booking_safe(b) for b in qs]})


# ---------------------------------------------------------------- equipment


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def equipment_used(request, workspace_id):
    access, error = _access(request, workspace_id)
    if error:
        return error
    rows = (
        ResearchWorkspaceBooking.objects.filter(workspace=access.workspace)
        .values(
            "booking__equipment__equipment_id",
            "booking__equipment__name",
            "booking__equipment__code",
            "booking__equipment__internal_department__name",
        )
        .annotate(bookings=Count("booking", distinct=True))
        .order_by("-bookings", "booking__equipment__name")
    )
    file_counts = dict(
        ResearchFile.objects.filter(workspace=access.workspace, status=FileStatus.AVAILABLE, booking__isnull=False)
        .values("booking__equipment_id")
        .annotate(c=Count("pk"))
        .values_list("booking__equipment_id", "c")
    )
    return Response(
        {
            "results": [
                {
                    "equipment_id": r["booking__equipment__equipment_id"],
                    "name": r["booking__equipment__name"],
                    "code": r["booking__equipment__code"],
                    "department_name": r["booking__equipment__internal_department__name"],
                    "bookings": r["bookings"],
                    "files": file_counts.get(r["booking__equipment__equipment_id"], 0),
                }
                for r in rows
            ]
        }
    )


# ---------------------------------------------------------------- publications


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def publications_collection(request, workspace_id):
    access, error = _access(request, workspace_id, edit=request.method == "POST")
    if error:
        return error
    workspace = access.workspace
    if request.method == "GET":
        links = (
            ResearchWorkspacePublication.objects.filter(workspace=workspace)
            .select_related("claim")
            .prefetch_related("claim__equipments")
        )
        return Response({"results": [serialize_publication(link.claim) for link in links]})

    raw_ids = request.data.get("claim_ids")
    if not isinstance(raw_ids, list) or not raw_ids or len(raw_ids) > 50:
        return _error("Provide between 1 and 50 publication ids.", status.HTTP_400_BAD_REQUEST)
    wanted = {int(c) for c in raw_ids if str(c).isdigit()}
    claims = list(EquipmentPublicationClaim.objects.filter(claim_id__in=wanted, submitted_by_id=workspace.owner_id))
    if not claims:
        return _not_found("Publication")
    linked = []
    for claim in claims:
        _, created = ResearchWorkspacePublication.objects.get_or_create(
            workspace=workspace, claim=claim, defaults={"added_by": request.user}
        )
        if created:
            linked.append(claim.claim_id)
            record_activity(
                workspace, request.user, ActivityAction.PUBLICATION_LINKED,
                target_type="publication", target_id=claim.claim_id, target_label=claim.title,
            )
    return Response({"linked": linked, "rejected": sorted(wanted - {c.claim_id for c in claims})})


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def publication_unlink(request, workspace_id, claim_id):
    access, error = _access(request, workspace_id, edit=True)
    if error:
        return error
    link = ResearchWorkspacePublication.objects.filter(workspace=access.workspace, claim_id=claim_id).select_related(
        "claim"
    ).first()
    if link is None:
        return _not_found("Publication")
    title = link.claim.title
    link.delete()
    record_activity(
        access.workspace, request.user, ActivityAction.PUBLICATION_UNLINKED,
        target_type="publication", target_id=claim_id, target_label=title,
    )
    return Response({"removed": True})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def linkable_publications(request, workspace_id):
    access, error = _access(request, workspace_id, owner=True)
    if error:
        return error
    linked = ResearchWorkspacePublication.objects.filter(workspace=access.workspace).values_list("claim_id", flat=True)
    claims = (
        EquipmentPublicationClaim.objects.filter(submitted_by=request.user)
        .exclude(claim_id__in=linked)
        .prefetch_related("equipments")
        .order_by("-created_at")[:100]
    )
    return Response({"results": [serialize_publication(c) for c in claims]})


# ---------------------------------------------------------------- members


def _serialize_member(member: ResearchWorkspaceMember) -> dict:
    return {
        "id": member.pk,
        "role": member.role,
        "user": user_summary(member.user),
        "added_at": member.added_at,
        "added_by": user_summary(member.added_by) if member.added_by_id else None,
    }


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def members_collection(request, workspace_id):
    access, error = _access(request, workspace_id, edit=request.method == "POST")
    if error:
        return error
    workspace = access.workspace

    if request.method == "GET":
        members = (
            ResearchWorkspaceMember.objects.filter(workspace=workspace, revoked_at__isnull=True)
            .select_related("user", "user__department", "added_by", "added_by__department")
            .order_by("role", "added_at")
        )
        rows = [_serialize_member(m) for m in members]
        if not any(r["role"] == MemberRole.OWNER for r in rows):
            rows.insert(
                0,
                {"id": None, "role": MemberRole.OWNER, "user": user_summary(workspace.owner), "added_at": workspace.created_at, "added_by": None},
            )
        return Response({"results": rows})

    if request.data.get("confirm") is not True:
        return _error("Confirm sharing before adding a viewer.", status.HTTP_400_BAD_REQUEST, "confirmation_required")
    raw_user = request.data.get("user_id")
    target = (
        eligible_users().exclude(pk=workspace.owner_id).filter(pk=raw_user if str(raw_user).isdigit() else -1).first()
    )
    if target is None:
        return _error(
            "Only IIT Roorkee students and faculty can be added to a workspace.",
            status.HTTP_400_BAD_REQUEST,
            "recipient_not_eligible",
        )
    try:
        with transaction.atomic():
            member = ResearchWorkspaceMember.objects.create(
                workspace=workspace, user=target, role=MemberRole.VIEWER, added_by=request.user
            )
    except IntegrityError:
        return _error("This person already has access.", status.HTTP_409_CONFLICT, "already_member")
    record_activity(
        workspace, request.user, ActivityAction.MEMBER_ADDED,
        target_type="member", target_id=target.pk, target_label=target.name or target.email,
    )
    transaction.on_commit(lambda: notify_viewer_added(member))
    member = ResearchWorkspaceMember.objects.select_related("user", "user__department", "added_by").get(pk=member.pk)
    return Response(_serialize_member(member), status=status.HTTP_201_CREATED)


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def member_remove(request, workspace_id, member_id):
    access, error = _access(request, workspace_id, owner=True)
    if error:
        return error
    member = (
        ResearchWorkspaceMember.objects.filter(pk=member_id, workspace=access.workspace, revoked_at__isnull=True)
        .select_related("user", "workspace")
        .first()
    )
    if member is None:
        return _not_found("Member")
    if member.role == MemberRole.OWNER:
        return _error("The workspace owner cannot be removed.", status.HTTP_400_BAD_REQUEST)
    member.revoked_at = timezone.now()
    member.revoked_by = request.user
    member.save(update_fields=["revoked_at", "revoked_by"])
    record_activity(
        access.workspace, request.user, ActivityAction.MEMBER_REMOVED,
        target_type="member", target_id=member.user_id, target_label=member.user.name or member.user.email,
    )
    transaction.on_commit(lambda: notify_viewer_removed(member))
    return Response({"removed": True})


# ---------------------------------------------------------------- activity / search


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def activity_list(request, workspace_id):
    access, error = _access(request, workspace_id)
    if error:
        return error
    page, size = _page_params(request, default=30, maximum=100)
    qs = ResearchActivity.objects.filter(workspace=access.workspace).select_related("actor", "actor__department")
    items, meta = _paginate(qs.order_by("-created_at"), page, size)
    return Response({"results": [serialize_activity(a) for a in items], "pagination": meta})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def workspace_search(request, workspace_id):
    access, error = _access(request, workspace_id)
    if error:
        return error
    q = file_policy.strip_control_chars(request.query_params.get("q")).strip()
    if len(q) < 2 and not q.isdigit():
        return Response({"q": q, "folders": [], "files": [], "bookings": [], "publications": [], "min_chars": 2})
    return Response({"q": q, **search_workspace(access.workspace, q[:100]), "min_chars": 2})

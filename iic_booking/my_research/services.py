"""Business logic for My Research: activity, serialization, folder tree, uploads, notifications."""

from __future__ import annotations

import logging
from typing import Any, Iterable

from django.conf import settings
from django.db.models import Count, IntegerField, Min, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.html import escape

from iic_booking.communication.utils import booking_display_id_for_email

from . import file_policy, storage
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
)

logger = logging.getLogger(__name__)

IN_FLIGHT_STATUSES = (FileStatus.AVAILABLE, FileStatus.PENDING_UPLOAD)


class FolderError(ValueError):
    pass


class UploadVerificationError(Exception):
    """The uploaded object is missing, incomplete or not allowed; the file was marked FAILED."""


# ---------------------------------------------------------------- activity


def record_activity(
    workspace: ResearchWorkspace,
    actor,
    action: str,
    *,
    target_type: str = "",
    target_id: Any = "",
    target_label: str = "",
    details: dict | None = None,
) -> ResearchActivity:
    now = timezone.now()
    activity = ResearchActivity.objects.create(
        workspace=workspace,
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=str(target_id or ""),
        target_label=(target_label or "")[:300],
        details=details or {},
    )
    ResearchWorkspace.objects.filter(pk=workspace.pk).update(last_activity_at=now)
    workspace.last_activity_at = now
    return activity


def serialize_activity(activity: ResearchActivity, *, include_workspace: bool = False) -> dict[str, Any]:
    data = {
        "id": activity.pk,
        "action": activity.action,
        "action_label": activity.get_action_display(),
        "actor": user_summary(activity.actor) if activity.actor_id else None,
        "target_type": activity.target_type,
        "target_id": activity.target_id,
        "target_label": activity.target_label,
        "details": activity.details or {},
        "created_at": activity.created_at,
    }
    if include_workspace:
        data["workspace_id"] = str(activity.workspace_id)
        data["workspace_name"] = activity.workspace.name
    return data


# ---------------------------------------------------------------- users


def user_summary(user) -> dict[str, Any] | None:
    if user is None:
        return None
    department = getattr(user, "department", None)
    return {
        "id": user.pk,
        "name": user.name or user.email,
        "email": user.email,
        "department": department.name if department else None,
        "user_type_label": user.get_user_type_display_label() or user.user_type,
    }


# ---------------------------------------------------------------- workspaces


def _count_subquery(model, filters: Q, field: str = "workspace"):
    return Coalesce(
        Subquery(
            model.objects.filter(filters, **{field: OuterRef("pk")})
            .order_by()
            .values(field)
            .annotate(c=Count("pk"))
            .values("c")[:1],
            output_field=IntegerField(),
        ),
        Value(0),
    )


def annotate_workspace_stats(queryset):
    equipment_count = Coalesce(
        Subquery(
            ResearchWorkspaceBooking.objects.filter(workspace=OuterRef("pk"))
            .order_by()
            .values("workspace")
            .annotate(c=Count("booking__equipment", distinct=True))
            .values("c")[:1],
            output_field=IntegerField(),
        ),
        Value(0),
    )
    storage_bytes = Coalesce(
        Subquery(
            ResearchFile.objects.filter(workspace=OuterRef("pk"), status=FileStatus.AVAILABLE)
            .order_by()
            .values("workspace")
            .annotate(s=Sum("size_bytes"))
            .values("s")[:1]
        ),
        Value(0),
    )
    return queryset.annotate(
        files_count=_count_subquery(ResearchFile, Q(status=FileStatus.AVAILABLE)),
        folders_count=_count_subquery(ResearchFolder, Q(deleted_at__isnull=True)),
        bookings_count=_count_subquery(ResearchWorkspaceBooking, Q()),
        publications_count=_count_subquery(ResearchWorkspacePublication, Q()),
        viewers_count=_count_subquery(ResearchWorkspaceMember, Q(role=MemberRole.VIEWER, revoked_at__isnull=True)),
        equipment_count=equipment_count,
        storage_bytes=storage_bytes,
    )


def serialize_workspace_card(workspace: ResearchWorkspace, role: str) -> dict[str, Any]:
    return {
        "id": str(workspace.pk),
        "name": workspace.name,
        "description": workspace.description,
        "status": workspace.status,
        "role": role,
        "owner": user_summary(workspace.owner),
        "created_at": workspace.created_at,
        "archived_at": workspace.archived_at,
        "last_activity_at": workspace.last_activity_at,
        "stats": {
            "files": getattr(workspace, "files_count", 0),
            "folders": getattr(workspace, "folders_count", 0),
            "bookings": getattr(workspace, "bookings_count", 0),
            "equipment": getattr(workspace, "equipment_count", 0),
            "publications": getattr(workspace, "publications_count", 0),
            "viewers": getattr(workspace, "viewers_count", 0),
            "storage_bytes": int(getattr(workspace, "storage_bytes", 0) or 0),
        },
    }


# ---------------------------------------------------------------- storage usage / quotas


def owner_usage_bytes(owner) -> int:
    total = ResearchFile.objects.filter(workspace__owner=owner, status__in=IN_FLIGHT_STATUSES).aggregate(
        s=Sum("size_bytes")
    )["s"]
    return int(total or 0)


def workspace_usage_bytes(workspace: ResearchWorkspace) -> int:
    total = ResearchFile.objects.filter(workspace=workspace, status__in=IN_FLIGHT_STATUSES).aggregate(
        s=Sum("size_bytes")
    )["s"]
    return int(total or 0)


def quota_error(workspace: ResearchWorkspace, size_bytes: int) -> str | None:
    user_quota = int(getattr(settings, "MY_RESEARCH_USER_STORAGE_QUOTA", 0) or 0)
    workspace_quota = int(getattr(settings, "MY_RESEARCH_WORKSPACE_STORAGE_QUOTA", 0) or 0)
    if user_quota and owner_usage_bytes(workspace.owner) + size_bytes > user_quota:
        return "This upload would exceed your My Research storage quota."
    if workspace_quota and workspace_usage_bytes(workspace) + size_bytes > workspace_quota:
        return "This upload would exceed the storage quota of this workspace."
    return None


# ---------------------------------------------------------------- folders


def active_folders(workspace: ResearchWorkspace):
    return ResearchFolder.objects.filter(workspace=workspace, deleted_at__isnull=True)


def folder_breadcrumbs(folder: ResearchFolder | None) -> list[dict[str, str]]:
    crumbs: list[dict[str, str]] = []
    current = folder
    guard = 0
    while current is not None and guard < 100:
        crumbs.append({"id": str(current.pk), "name": current.name})
        current = current.parent
        guard += 1
    crumbs.reverse()
    return crumbs


def folder_depth(folder: ResearchFolder | None) -> int:
    return len(folder_breadcrumbs(folder))


def subtree_height(folder: ResearchFolder) -> int:
    """Number of folder levels in the subtree rooted at folder (1 = no subfolders)."""
    height = 1
    level = [folder.pk]
    while level and height < 100:
        level = list(
            ResearchFolder.objects.filter(parent_id__in=level, deleted_at__isnull=True).values_list("pk", flat=True)
        )
        if level:
            height += 1
    return height


def sibling_name_taken(workspace, parent, name: str, *, exclude_id=None) -> bool:
    qs = active_folders(workspace).filter(parent=parent, name__iexact=name)
    if exclude_id:
        qs = qs.exclude(pk=exclude_id)
    return qs.exists()


def validate_folder_move(folder: ResearchFolder, new_parent: ResearchFolder | None) -> None:
    if new_parent is None:
        return
    if new_parent.workspace_id != folder.workspace_id:
        raise FolderError("Folders can only be moved within the same workspace.")
    current = new_parent
    guard = 0
    while current is not None and guard < 100:
        if current.pk == folder.pk:
            raise FolderError("A folder cannot be moved into itself or one of its subfolders.")
        current = current.parent
        guard += 1
    max_depth = int(getattr(settings, "MY_RESEARCH_MAX_FOLDER_DEPTH", 20) or 20)
    if folder_depth(new_parent) + subtree_height(folder) > max_depth:
        raise FolderError(f"Folders can be nested at most {max_depth} levels deep.")


def descendant_folder_ids(folder: ResearchFolder) -> list:
    ids = [folder.pk]
    level = [folder.pk]
    guard = 0
    while level and guard < 100:
        level = list(
            ResearchFolder.objects.filter(parent_id__in=level, deleted_at__isnull=True).values_list("pk", flat=True)
        )
        ids.extend(level)
        guard += 1
    return ids


def soft_delete_folder(folder: ResearchFolder, actor) -> dict[str, int]:
    """Soft-delete a folder subtree and its files. S3 objects are retained (no destructive cascade)."""
    now = timezone.now()
    ids = descendant_folder_ids(folder)
    files = ResearchFile.objects.filter(folder_id__in=ids, status__in=IN_FLIGHT_STATUSES)
    file_count = files.count()
    files.update(status=FileStatus.DELETED, deleted_at=now, deleted_by=actor)
    folder_count = ResearchFolder.objects.filter(pk__in=ids, deleted_at__isnull=True).update(
        deleted_at=now, deleted_by=actor
    )
    return {"folders": folder_count, "files": file_count}


def serialize_folder(folder: ResearchFolder) -> dict[str, Any]:
    return {
        "id": str(folder.pk),
        "name": folder.name,
        "parent_id": str(folder.parent_id) if folder.parent_id else None,
        "has_children": bool(getattr(folder, "child_count", 0)),
        "file_count": getattr(folder, "file_count", None),
        "created_at": folder.created_at,
        "updated_at": folder.updated_at,
    }


def annotate_folder_counts(queryset):
    return queryset.annotate(
        child_count=Count("children", filter=Q(children__deleted_at__isnull=True), distinct=True),
        file_count=Count("files", filter=Q(files__status=FileStatus.AVAILABLE), distinct=True),
    )


# ---------------------------------------------------------------- bookings


def annotate_booking_timing(queryset):
    return queryset.select_related("equipment", "equipment__internal_department").annotate(
        first_slot_at=Min("daily_slots__start_datetime")
    )


def serialize_booking_safe(booking) -> dict[str, Any]:
    """Research-relevant booking facts only; no charges, wallet, contact or admin data."""
    equipment = booking.equipment
    department = getattr(equipment, "internal_department", None)
    return {
        "booking_id": booking.booking_id,
        "display_id": booking_display_id_for_email(booking),
        "status": booking.status,
        "status_display": booking.get_status_display(),
        "equipment_id": equipment.equipment_id,
        "equipment_code": equipment.code,
        "equipment_name": equipment.name,
        "department_name": department.name if department else None,
        "booking_date": getattr(booking, "first_slot_at", None),
        "completed_at": booking.completed_at,
    }


# ---------------------------------------------------------------- publications


def serialize_publication(claim) -> dict[str, Any]:
    return {
        "claim_id": claim.claim_id,
        "title": claim.title,
        "authors": claim.authors,
        "journal": claim.journal,
        "year": claim.year,
        "doi": claim.doi,
        "url": claim.url,
        "status": claim.status,
        "status_display": claim.get_status_display(),
        "equipment": [{"equipment_id": e.equipment_id, "name": e.name, "code": e.code} for e in claim.equipments.all()],
    }


# ---------------------------------------------------------------- files


def serialize_file(research_file: ResearchFile) -> dict[str, Any]:
    booking = research_file.booking if research_file.booking_id else None
    equipment = booking.equipment if booking is not None else None
    return {
        "id": str(research_file.pk),
        "workspace_id": str(research_file.workspace_id),
        "folder_id": str(research_file.folder_id) if research_file.folder_id else None,
        "name": research_file.display_name,
        "original_name": research_file.original_name,
        "size_bytes": research_file.size_bytes,
        "status": research_file.status,
        "detected_type": research_file.detected_type,
        "declared_content_type": research_file.declared_content_type,
        "preview_kind": file_policy.preview_kind(research_file.detected_type, research_file.display_name),
        "checksum_sha256": research_file.checksum_sha256 or None,
        "checksum_verified": research_file.checksum_verified,
        "uploaded_by": user_summary(research_file.uploaded_by) if research_file.uploaded_by_id else None,
        "created_at": research_file.created_at,
        "uploaded_at": research_file.uploaded_at,
        "booking": (
            {
                "booking_id": booking.booking_id,
                "display_id": booking_display_id_for_email(booking),
                "equipment_id": equipment.equipment_id if equipment else None,
                "equipment_name": equipment.name if equipment else None,
                "equipment_code": equipment.code if equipment else None,
            }
            if booking is not None
            else None
        ),
    }


def file_queryset():
    return ResearchFile.objects.select_related("uploaded_by", "uploaded_by__department", "booking", "booking__equipment")


def mark_failed(research_file: ResearchFile, reason: str, *, delete_object: bool) -> None:
    if delete_object:
        try:
            storage.delete_object(research_file.storage_key)
        except storage.ResearchStorageError:
            logger.exception("my_research: failed to delete rejected object file=%s", research_file.pk)
    research_file.status = FileStatus.FAILED
    research_file.failure_reason = reason[:255]
    research_file.save(update_fields=["status", "failure_reason", "updated_at"])


def finalize_upload(research_file: ResearchFile, actor) -> ResearchFile:
    """
    Verify the object in S3 and mark the file AVAILABLE.

    Raises UploadVerificationError (file marked FAILED) when the object is incomplete or disallowed,
    storage.ObjectNotFound when nothing was uploaded yet, and storage.ResearchStorageError on a
    temporary S3 problem (the file stays PENDING_UPLOAD so nothing valid is discarded).
    """
    head = storage.head_object(research_file.storage_key, with_checksum=bool(research_file.checksum_sha256))
    actual_size = int(head.get("ContentLength") or 0)
    if actual_size != research_file.size_bytes:
        mark_failed(
            research_file,
            f"Uploaded size {actual_size} does not match the expected {research_file.size_bytes} bytes.",
            delete_object=True,
        )
        raise UploadVerificationError(research_file.failure_reason)

    checksum_verified = False
    if research_file.checksum_sha256:
        stored = head.get("ChecksumSHA256") or ""
        if stored and stored != research_file.checksum_sha256:
            mark_failed(research_file, "Stored checksum does not match the uploaded file.", delete_object=True)
            raise UploadVerificationError(research_file.failure_reason)
        checksum_verified = bool(stored)

    detected = file_policy.sniff_type(storage.read_prefix(research_file.storage_key, file_policy.SNIFF_BYTES))
    reason = file_policy.rejection_reason(detected)
    if reason:
        mark_failed(research_file, reason, delete_object=True)
        raise UploadVerificationError(reason)

    etag = str(head.get("ETag") or "").strip('"')
    research_file.detected_type = detected
    research_file.etag = etag[:200]
    research_file.etag_is_md5 = bool(etag) and "-" not in etag and not storage.uses_kms()
    research_file.checksum_verified = checksum_verified
    research_file.multipart_upload_id = ""
    research_file.status = FileStatus.AVAILABLE
    research_file.uploaded_at = timezone.now()
    research_file.failure_reason = ""
    research_file.save(
        update_fields=[
            "detected_type",
            "etag",
            "etag_is_md5",
            "checksum_verified",
            "multipart_upload_id",
            "status",
            "uploaded_at",
            "failure_reason",
            "updated_at",
        ]
    )
    record_activity(
        research_file.workspace,
        actor,
        ActivityAction.FILE_UPLOADED,
        target_type="file",
        target_id=research_file.pk,
        target_label=research_file.display_name,
        details={"size_bytes": research_file.size_bytes, "booking_id": research_file.booking_id},
    )
    return research_file


def link_booking(
    workspace: ResearchWorkspace, booking, actor, folder: ResearchFolder | None = None
) -> tuple[ResearchWorkspaceBooking, bool]:
    link, created = ResearchWorkspaceBooking.objects.get_or_create(
        workspace=workspace, booking=booking, defaults={"added_by": actor, "folder": folder}
    )
    if not created and folder is not None and link.folder_id != folder.pk:
        link.folder = folder
        link.save(update_fields=["folder"])
    if created:
        record_activity(
            workspace,
            actor,
            ActivityAction.BOOKING_LINKED,
            target_type="booking",
            target_id=booking.booking_id,
            target_label=f"{booking.equipment.name} · {booking_display_id_for_email(booking)}",
            details={"folder_id": str(folder.pk), "folder_name": folder.name} if folder else None,
        )
    return link, created


# ---------------------------------------------------------------- search


def search_workspace(workspace: ResearchWorkspace, q: str, limit: int = 25) -> dict[str, list]:
    folders = active_folders(workspace).filter(name__icontains=q).select_related("parent").order_by("name")[:limit]
    files = (
        file_queryset()
        .filter(workspace=workspace, status=FileStatus.AVAILABLE)
        .filter(Q(display_name__icontains=q) | Q(original_name__icontains=q))
        .select_related("folder")
        .order_by("display_name")[: limit * 2]
    )
    booking_match = (
        Q(booking__equipment__name__icontains=q)
        | Q(booking__equipment__code__icontains=q)
        | Q(booking__virtual_booking_id__icontains=q)
    )
    if q.isdigit():
        booking_match |= Q(booking__booking_id=int(q))
    booking_ids = (
        ResearchWorkspaceBooking.objects.filter(workspace=workspace)
        .filter(booking_match)
        .values_list("booking_id", flat=True)[:limit]
    )
    from iic_booking.equipment.models import Booking

    bookings = annotate_booking_timing(Booking.objects.filter(booking_id__in=list(booking_ids)))
    publications = (
        ResearchWorkspacePublication.objects.filter(workspace=workspace)
        .filter(
            Q(claim__title__icontains=q)
            | Q(claim__doi__icontains=q)
            | Q(claim__journal__icontains=q)
            | Q(claim__authors__icontains=q)
        )
        .select_related("claim")
        .prefetch_related("claim__equipments")[:limit]
    )
    return {
        "folders": [
            {**serialize_folder(f), "path": [c["name"] for c in folder_breadcrumbs(f)]} for f in folders
        ],
        "files": [
            {**serialize_file(f), "folder_path": [c["name"] for c in folder_breadcrumbs(f.folder)]} for f in files
        ],
        "bookings": [serialize_booking_safe(b) for b in bookings],
        "publications": [serialize_publication(link.claim) for link in publications],
    }


# ---------------------------------------------------------------- notifications


WORKSPACE_SHARED_TITLE = "You have been given read-only access to a Research Workspace"


def notify_viewer_added(member: ResearchWorkspaceMember) -> None:
    from iic_booking.communication.email_branding import COLOR_PRIMARY, user_display_name
    from iic_booking.communication.service import CommunicationService
    from iic_booking.communication.styled_transactional_emails import _send, _shell
    from iic_booking.communication.utils import get_frontend_absolute_url

    workspace = member.workspace
    owner = workspace.owner
    recipient = member.user
    link = get_frontend_absolute_url(f"/my-research/{workspace.pk}")
    owner_name = user_display_name(owner)
    message = f"{owner_name} gave you read-only access to the research workspace \"{workspace.name}\"."
    try:
        CommunicationService.send_push_notification(
            recipient=recipient,
            title=WORKSPACE_SHARED_TITLE,
            message=message,
            metadata={
                "notification_type": "info",
                "link": link,
                "research_workspace_id": str(workspace.pk),
                "event": "research_workspace.shared",
            },
        )
    except Exception:
        logger.exception("my_research viewer push failed member=%s", member.pk)
    try:
        body = (
            f"<p style='margin:0 0 12px 0;'>{escape(message)}</p>"
            f"<p style='margin:0 0 8px 0;'><b>Workspace:</b> {escape(workspace.name)}</p>"
            f"<p style='margin:0 0 8px 0;'><b>Owner:</b> {escape(owner_name)} ({escape(owner.email)})</p>"
            f"<p style='margin:0 0 8px 0;'><b>Access:</b> Read-only (Viewer)</p>"
            f"<p style='margin:16px 0 0 0;'><a href='{escape(link)}' style='background:{COLOR_PRIMARY};color:#fff;"
            f"padding:10px 14px;border-radius:8px;text-decoration:none;font-weight:700;'>Open Workspace</a></p>"
        )
        text = f"{message}\nWorkspace: {workspace.name}\nOwner: {owner_name}\nOpen: {link}"
        _send(
            recipient.email,
            "Read-only access to a Research Workspace",
            text,
            _shell("Research Workspace shared", workspace.name, body),
        )
    except Exception:
        logger.exception("my_research viewer email failed member=%s", member.pk)


def notify_viewer_removed(member: ResearchWorkspaceMember) -> None:
    from iic_booking.communication.service import CommunicationService

    try:
        CommunicationService.send_push_notification(
            recipient=member.user,
            title="Research Workspace access removed",
            message=f"Your read-only access to the research workspace \"{member.workspace.name}\" was removed.",
            metadata={
                "notification_type": "info",
                "research_workspace_id": str(member.workspace_id),
                "event": "research_workspace.revoked",
            },
        )
    except Exception:
        logger.exception("my_research viewer removal push failed member=%s", member.pk)


def bulk_serialize_files(files: Iterable[ResearchFile]) -> list[dict[str, Any]]:
    return [serialize_file(f) for f in files]

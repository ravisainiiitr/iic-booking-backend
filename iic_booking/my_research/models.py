"""
My Research data model.

File bytes live only in private S3; these tables hold metadata. Workspace, folder and file
foreign keys use PROTECT so routine parent deletions can never silently orphan S3 objects.
Deleting a booking only drops its workspace link; research files keep their data (SET_NULL).
"""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


class WorkspaceStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    ARCHIVED = "ARCHIVED", "Archived"


class MemberRole(models.TextChoices):
    OWNER = "OWNER", "Owner"
    VIEWER = "VIEWER", "Viewer"


class FileStatus(models.TextChoices):
    PENDING_UPLOAD = "PENDING_UPLOAD", "Pending upload"
    AVAILABLE = "AVAILABLE", "Available"
    FAILED = "FAILED", "Failed"
    DELETED = "DELETED", "Deleted"


class ActivityAction(models.TextChoices):
    WORKSPACE_CREATED = "WORKSPACE_CREATED", "Workspace created"
    WORKSPACE_UPDATED = "WORKSPACE_UPDATED", "Workspace updated"
    WORKSPACE_ARCHIVED = "WORKSPACE_ARCHIVED", "Workspace archived"
    WORKSPACE_RESTORED = "WORKSPACE_RESTORED", "Workspace restored"
    FOLDER_CREATED = "FOLDER_CREATED", "Folder created"
    FOLDER_RENAMED = "FOLDER_RENAMED", "Folder renamed"
    FOLDER_MOVED = "FOLDER_MOVED", "Folder moved"
    FOLDER_DELETED = "FOLDER_DELETED", "Folder deleted"
    FILE_UPLOADED = "FILE_UPLOADED", "File uploaded"
    FILE_RENAMED = "FILE_RENAMED", "File renamed"
    FILE_MOVED = "FILE_MOVED", "File moved"
    FILE_DELETED = "FILE_DELETED", "File deleted"
    BOOKING_LINKED = "BOOKING_LINKED", "Booking associated"
    BOOKING_UNLINKED = "BOOKING_UNLINKED", "Booking removed"
    PUBLICATION_LINKED = "PUBLICATION_LINKED", "Publication associated"
    PUBLICATION_UNLINKED = "PUBLICATION_UNLINKED", "Publication removed"
    MEMBER_ADDED = "MEMBER_ADDED", "Viewer added"
    MEMBER_REMOVED = "MEMBER_REMOVED", "Viewer removed"


class ResearchWorkspace(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="research_workspaces"
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    status = models.CharField(max_length=20, choices=WorkspaceStatus.choices, default=WorkspaceStatus.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    last_activity_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-last_activity_at"]
        indexes = [
            models.Index(fields=["owner", "status"], name="mr_ws_owner_status_idx"),
            models.Index(fields=["-last_activity_at"], name="mr_ws_activity_idx"),
        ]

    def __str__(self):
        return self.name

    @property
    def is_archived(self) -> bool:
        return self.status == WorkspaceStatus.ARCHIVED


class ResearchWorkspaceMember(models.Model):
    workspace = models.ForeignKey(ResearchWorkspace, on_delete=models.CASCADE, related_name="members")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="research_memberships"
    )
    role = models.CharField(max_length=10, choices=MemberRole.choices, default=MemberRole.VIEWER)
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    added_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["added_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "user"],
                condition=Q(revoked_at__isnull=True),
                name="mr_uniq_active_member",
            ),
            models.UniqueConstraint(
                fields=["workspace"],
                condition=Q(role="OWNER", revoked_at__isnull=True),
                name="mr_uniq_active_owner",
            ),
        ]
        indexes = [models.Index(fields=["user", "revoked_at"], name="mr_member_user_idx")]


class ResearchFolder(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(ResearchWorkspace, on_delete=models.PROTECT, related_name="folders")
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children")
    name = models.CharField(max_length=255)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "parent", "name"],
                condition=Q(deleted_at__isnull=True, parent__isnull=False),
                name="mr_uniq_folder_name_in_parent",
            ),
            models.UniqueConstraint(
                fields=["workspace", "name"],
                condition=Q(deleted_at__isnull=True, parent__isnull=True),
                name="mr_uniq_folder_name_at_root",
            ),
        ]
        indexes = [models.Index(fields=["workspace", "parent", "deleted_at"], name="mr_folder_parent_idx")]

    def __str__(self):
        return self.name


class ResearchFile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(ResearchWorkspace, on_delete=models.PROTECT, related_name="files")
    folder = models.ForeignKey(ResearchFolder, null=True, blank=True, on_delete=models.PROTECT, related_name="files")
    booking = models.ForeignKey(
        "equipment.Booking", null=True, blank=True, on_delete=models.SET_NULL, related_name="research_files"
    )
    original_name = models.CharField(max_length=255)
    display_name = models.CharField(max_length=255)
    storage_key = models.CharField(max_length=1024, unique=True)
    declared_content_type = models.CharField(max_length=150, blank=True, default="")
    detected_type = models.CharField(max_length=30, blank=True, default="")
    size_bytes = models.BigIntegerField()
    checksum_sha256 = models.CharField(max_length=64, blank=True, default="")
    checksum_verified = models.BooleanField(default=False)
    etag = models.CharField(max_length=200, blank=True, default="")
    etag_is_md5 = models.BooleanField(default=False)
    multipart_upload_id = models.CharField(max_length=1024, blank=True, default="")
    status = models.CharField(max_length=20, choices=FileStatus.choices, default=FileStatus.PENDING_UPLOAD)
    failure_reason = models.CharField(max_length=255, blank=True, default="")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="research_files"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    uploaded_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["display_name"]
        indexes = [
            models.Index(fields=["workspace", "folder", "status"], name="mr_file_folder_idx"),
            models.Index(fields=["workspace", "status", "-created_at"], name="mr_file_recent_idx"),
            models.Index(fields=["status", "created_at"], name="mr_file_cleanup_idx"),
            models.Index(fields=["uploaded_by", "status"], name="mr_file_uploader_idx"),
            models.Index(fields=["booking"], name="mr_file_booking_idx"),
        ]

    def __str__(self):
        return self.display_name


class ResearchWorkspaceBooking(models.Model):
    """Optional link between an existing booking and a workspace (bookings are never modified)."""

    workspace = models.ForeignKey(ResearchWorkspace, on_delete=models.CASCADE, related_name="booking_links")
    booking = models.ForeignKey("equipment.Booking", on_delete=models.CASCADE, related_name="research_workspace_links")
    folder = models.ForeignKey(
        ResearchFolder, null=True, blank=True, on_delete=models.SET_NULL, related_name="booking_links"
    )
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-added_at"]
        constraints = [models.UniqueConstraint(fields=["workspace", "booking"], name="mr_uniq_workspace_booking")]


class ResearchWorkspacePublication(models.Model):
    workspace = models.ForeignKey(ResearchWorkspace, on_delete=models.CASCADE, related_name="publication_links")
    claim = models.ForeignKey(
        "equipment.EquipmentPublicationClaim", on_delete=models.CASCADE, related_name="research_workspace_links"
    )
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-added_at"]
        constraints = [models.UniqueConstraint(fields=["workspace", "claim"], name="mr_uniq_workspace_publication")]


class ResearchActivity(models.Model):
    workspace = models.ForeignKey(ResearchWorkspace, on_delete=models.CASCADE, related_name="activities")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    action = models.CharField(max_length=40, choices=ActivityAction.choices)
    target_type = models.CharField(max_length=20, blank=True, default="")
    target_id = models.CharField(max_length=64, blank=True, default="")
    target_label = models.CharField(max_length=300, blank=True, default="")
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["workspace", "-created_at"], name="mr_activity_ws_idx")]


from .group_models import *  # noqa: E402,F401,F403

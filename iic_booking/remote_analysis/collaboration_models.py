"""Milestone 7 — Collaboration, notifications, assistance models."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from iic_booking.remote_analysis.constants import (
    ActivityVerb,
    AssistancePriority,
    AssistanceStatus,
    InvitationKind,
    InvitationStatus,
    NoteVisibility,
    NotificationChannel,
    NotificationStatus,
    NotificationType,
    SharePermissionLevel,
)


class SessionComment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        "remote_analysis.RemoteDesktopSession",
        on_delete=models.CASCADE,
        related_name="comments",
    )
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ra_session_comments")
    body = models.TextField()
    is_markdown = models.BooleanField(default=True)
    pinned = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted = models.BooleanField(default=False)

    class Meta:
        ordering = ["-pinned", "-created_at"]


class WorkspaceComment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "remote_analysis.AnalysisWorkspace",
        on_delete=models.CASCADE,
        related_name="comments",
    )
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ra_workspace_comments")
    body = models.TextField()
    is_markdown = models.BooleanField(default=True)
    pinned = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted = models.BooleanField(default=False)

    class Meta:
        ordering = ["-pinned", "-created_at"]


class SessionNote(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        "remote_analysis.RemoteDesktopSession",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="notes",
    )
    workspace = models.ForeignKey(
        "remote_analysis.AnalysisWorkspace",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="notes",
    )
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ra_session_notes")
    title = models.CharField(max_length=255, blank=True, default="")
    body = models.TextField()
    visibility = models.CharField(max_length=16, choices=NoteVisibility.choices, default=NoteVisibility.PRIVATE)
    pinned = models.BooleanField(default=False)
    is_markdown = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-pinned", "-created_at"]


class SharedWorkspace(models.Model):
    """Named share grant for a workspace (no anonymous access)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "remote_analysis.AnalysisWorkspace",
        on_delete=models.CASCADE,
        related_name="shared_entries",
    )
    name = models.CharField(max_length=255, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ra_shared_workspaces_created",
    )
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class WorkspaceSharePermission(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    shared_workspace = models.ForeignKey(SharedWorkspace, on_delete=models.CASCADE, related_name="permissions")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="ra_workspace_share_permissions",
    )
    department = models.ForeignKey(
        "users.Department",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="ra_workspace_share_permissions",
    )
    permission = models.CharField(max_length=16, choices=SharePermissionLevel.choices, default=SharePermissionLevel.READ)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["permission"]


class SessionInvitation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        "remote_analysis.RemoteDesktopSession",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="invitations",
    )
    reservation = models.ForeignKey(
        "remote_analysis.AnalysisReservation",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="invitations",
    )
    workspace = models.ForeignKey(
        "remote_analysis.AnalysisWorkspace",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="invitations",
    )
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ra_invitations_sent",
    )
    invited_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="ra_invitations_received",
    )
    invited_email = models.EmailField(blank=True, default="")
    kind = models.CharField(max_length=32, choices=InvitationKind.choices, default=InvitationKind.COLLABORATOR)
    status = models.CharField(max_length=16, choices=InvitationStatus.choices, default=InvitationStatus.PENDING)
    message = models.TextField(blank=True, default="")
    expires_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "expires_at"]),
            models.Index(fields=["invited_user", "status"]),
        ]


class Notification(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ra_notifications")
    notification_type = models.CharField(max_length=64, choices=NotificationType.choices, db_index=True)
    channel = models.CharField(max_length=16, choices=NotificationChannel.choices, default=NotificationChannel.PORTAL)
    status = models.CharField(max_length=16, choices=NotificationStatus.choices, default=NotificationStatus.PENDING)
    title = models.CharField(max_length=255)
    body = models.TextField(blank=True, default="")
    link = models.CharField(max_length=1024, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "status", "created_at"])]


class NotificationPreference(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ra_notification_preference",
    )
    portal_enabled = models.BooleanField(default=True)
    email_enabled = models.BooleanField(default=True)
    sms_enabled = models.BooleanField(default=False)  # future
    whatsapp_enabled = models.BooleanField(default=False)  # future
    push_enabled = models.BooleanField(default=False)  # future
    quiet_hours_start = models.TimeField(null=True, blank=True)
    quiet_hours_end = models.TimeField(null=True, blank=True)
    reminder_minutes_before = models.PositiveIntegerField(default=30)
    digest_frequency = models.CharField(max_length=32, blank=True, default="none")  # none|daily|weekly
    disabled_types = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class ActivityFeed(models.Model):
    """Per-user or global activity stream container."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="ra_activity_feeds",
        help_text=_("Null = platform-wide feed"),
    )
    name = models.CharField(max_length=128, blank=True, default="default")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("user", "name")]


class ActivityEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    feed = models.ForeignKey(ActivityFeed, on_delete=models.CASCADE, related_name="events")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ra_activity_events",
    )
    verb = models.CharField(max_length=32, choices=ActivityVerb.choices, db_index=True)
    summary = models.CharField(max_length=512)
    details = models.TextField(blank=True, default="")
    session = models.ForeignKey(
        "remote_analysis.RemoteDesktopSession",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="activity_events",
    )
    workspace = models.ForeignKey(
        "remote_analysis.AnalysisWorkspace",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="activity_events",
    )
    reservation = models.ForeignKey(
        "remote_analysis.AnalysisReservation",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="activity_events",
    )
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["feed", "created_at"]),
            models.Index(fields=["verb", "created_at"]),
        ]


class SessionAssistanceRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        "remote_analysis.RemoteDesktopSession",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="assistance_requests",
    )
    reservation = models.ForeignKey(
        "remote_analysis.AnalysisReservation",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="assistance_requests",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ra_assistance_requested",
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ra_assistance_assigned",
    )
    status = models.CharField(max_length=16, choices=AssistanceStatus.choices, default=AssistanceStatus.REQUESTED, db_index=True)
    priority = models.CharField(max_length=16, choices=AssistancePriority.choices, default=AssistancePriority.NORMAL)
    subject = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    resolution = models.TextField(blank=True, default="")
    attachment_name = models.CharField(max_length=512, blank=True, default="")  # future screenshots
    created_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["assigned_to", "status"]),
            models.Index(fields=["requested_by", "status"]),
        ]


class SessionAssistanceEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(SessionAssistanceRequest, on_delete=models.CASCADE, related_name="events")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    from_status = models.CharField(max_length=16, blank=True, default="")
    to_status = models.CharField(max_length=16, choices=AssistanceStatus.choices)
    note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class Announcement(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=255)
    body = models.TextField()
    is_markdown = models.BooleanField(default=True)
    active = models.BooleanField(default=True)
    department = models.ForeignKey(
        "users.Department",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="ra_announcements",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class Bookmark(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ra_bookmarks")
    label = models.CharField(max_length=255)
    target_type = models.CharField(max_length=64)  # session|workspace|reservation|url
    target_id = models.CharField(max_length=64, blank=True, default="")
    url = models.CharField(max_length=1024, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class FavoriteWorkstation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ra_favorite_workstations")
    workstation = models.ForeignKey(
        "remote_analysis.AnalysisWorkstation",
        on_delete=models.CASCADE,
        related_name="favorited_by",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("user", "workstation")]
        ordering = ["-created_at"]


class RecentWorkspace(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ra_recent_workspaces")
    workspace = models.ForeignKey(
        "remote_analysis.AnalysisWorkspace",
        on_delete=models.CASCADE,
        related_name="recent_views",
    )
    last_accessed_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("user", "workspace")]
        ordering = ["-last_accessed_at"]


class CollaborationTelemetry(models.Model):
    id = models.BigAutoField(primary_key=True)
    metric_name = models.CharField(max_length=128, db_index=True)
    value = models.FloatField()
    unit = models.CharField(max_length=32, blank=True, default="")
    tags = models.JSONField(default=dict, blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-recorded_at"]

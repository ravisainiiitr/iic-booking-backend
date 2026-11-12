"""Communities, membership, Connect-wide roles and the membership audit trail."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from .base import AppendOnlyModel


class CommunityKind(models.TextChoices):
    GROUP = "group", "Equipment Group community"
    GENERAL = "general", "General IITR Discussions"


class CommunityStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    # The equipment group was removed: the community and its discussions are kept, read-only.
    ARCHIVED = "archived", "Archived"


class Community(models.Model):
    """One per Equipment Group (kind=group) plus exactly one General IITR Discussions (kind=general).

    ``equipment_group`` is SET_NULL so removing a group never deletes the community; ``equipment_group_ref_id``
    keeps the original group id (the uniqueness key) and ``group_name_snapshot`` its last known name.
    """

    kind = models.CharField(max_length=16, choices=CommunityKind.choices, db_index=True)
    slug = models.SlugField(max_length=80, unique=True)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    equipment_group = models.ForeignKey(
        "equipment.EquipmentGroup",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="connect_communities",
    )
    equipment_group_ref_id = models.IntegerField(null=True, blank=True, db_index=True)
    group_name_snapshot = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=16, choices=CommunityStatus.choices, default=CommunityStatus.ACTIVE)
    # Every instrument of the group is test-only equipment: only test accounts and the Main Admin may enter.
    is_test_only = models.BooleanField(default=False)
    archived_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Connect community"
        verbose_name_plural = "Connect communities"
        ordering = ["kind", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["equipment_group_ref_id"],
                condition=Q(kind="group"),
                name="connect_one_community_per_group",
            ),
            models.UniqueConstraint(fields=["kind"], condition=Q(kind="general"), name="connect_single_general"),
            models.CheckConstraint(
                condition=Q(kind="general") | Q(equipment_group_ref_id__isnull=False),
                name="connect_group_community_has_group_ref",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def is_general(self) -> bool:
        return self.kind == CommunityKind.GENERAL


class MembershipRole(models.TextChoices):
    MEMBER = "member", "Member"
    MODERATOR = "moderator", "Group Moderator"


class MembershipStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    # Only staff sources existed and they all ended: no access, history kept.
    LAPSED = "lapsed", "Lapsed"
    # Posting suspended by a moderator: can read, cannot post or react.
    SUSPENDED = "suspended", "Posting suspended"
    # Access revoked by an administrator.
    REVOKED = "revoked", "Revoked"


MANUAL_STATUSES = (MembershipStatus.SUSPENDED, MembershipStatus.REVOKED)
READ_STATUSES = (MembershipStatus.ACTIVE, MembershipStatus.SUSPENDED)


class CommunityMembership(models.Model):
    """One row per user per community. Sources are recomputed idempotently by the sync service.

    General IITR Discussions has implicit access for every internal user, so rows exist there only for
    moderators and suspensions/revocations.
    """

    community = models.ForeignKey(Community, on_delete=models.PROTECT, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="connect_memberships")
    status = models.CharField(max_length=16, choices=MembershipStatus.choices, default=MembershipStatus.ACTIVE)
    via_booking = models.BooleanField(default=False)
    via_oic = models.BooleanField(default=False)
    via_operator = models.BooleanField(default=False)
    via_manual = models.BooleanField(default=False)
    # None = default (OIC and Lab Operators moderate); True = appointed moderator; False = moderation removed.
    moderator_override = models.BooleanField(null=True, blank=True)
    booking_count = models.PositiveIntegerField(default=0)
    first_booked_at = models.DateTimeField(null=True, blank=True)
    last_booked_at = models.DateTimeField(null=True, blank=True)
    status_reason = models.CharField(max_length=500, blank=True, default="")
    status_changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    status_changed_at = models.DateTimeField(null=True, blank=True)
    suspended_until = models.DateTimeField(null=True, blank=True)
    joined_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Connect community membership"
        constraints = [
            models.UniqueConstraint(fields=["community", "user"], name="connect_membership_unique"),
        ]
        indexes = [
            models.Index(fields=["user", "status"], name="connect_member_user_status"),
            models.Index(fields=["community", "status"], name="connect_member_comm_status"),
        ]

    def __str__(self) -> str:
        return f"{self.community_id}:{self.user_id} ({self.status})"

    @property
    def is_staff_source(self) -> bool:
        return self.via_oic or self.via_operator

    @property
    def role(self) -> str:
        if self.moderator_override is True:
            return MembershipRole.MODERATOR
        if self.moderator_override is False:
            return MembershipRole.MEMBER
        return MembershipRole.MODERATOR if self.is_staff_source else MembershipRole.MEMBER


class ConnectRole(models.TextChoices):
    COMMUNITY_ADMIN = "community_admin", "Community Admin"


class ConnectRoleAssignment(models.Model):
    """Connect-wide roles granted by the Portal Admin (Main Administrator). Never self-assigned."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="connect_roles")
    role = models.CharField(max_length=32, choices=ConnectRole.choices)
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    granted_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "role"], condition=Q(revoked_at__isnull=True), name="connect_role_active_unique"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} {self.role}"


class CommunitySyncRun(models.Model):
    """One run of the membership sync (command, signal batch or admin action) with its report."""

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    dry_run = models.BooleanField(default=True)
    scope = models.CharField(max_length=64, default="all")
    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    source = models.CharField(max_length=32, default="command")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING)
    report = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return f"sync {self.pk} {self.scope} dry={self.dry_run} {self.status}"


class MembershipAuditAction(models.TextChoices):
    JOINED = "joined", "Joined"
    SOURCE_ADDED = "source_added", "Source added"
    SOURCE_REMOVED = "source_removed", "Source removed"
    LAPSED = "lapsed", "Lapsed"
    REACTIVATED = "reactivated", "Reactivated"
    SUSPENDED = "suspended", "Posting suspended"
    REVOKED = "revoked", "Access revoked"
    RESTORED = "restored", "Restored"
    ROLE_CHANGED = "role_changed", "Role changed"
    MANUAL_ADD = "manual_add", "Added manually"


class MembershipAuditLog(AppendOnlyModel):
    community = models.ForeignKey(Community, on_delete=models.PROTECT, related_name="membership_audit")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    action = models.CharField(max_length=32, choices=MembershipAuditAction.choices)
    detail = models.JSONField(default=dict, blank=True)
    sync_run = models.ForeignKey(
        CommunitySyncRun, on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_rows"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["community", "user", "created_at"], name="connect_member_audit_idx")]


class DiscussionCategory(models.Model):
    """Admin-managed categories. General IITR Discussions is seeded with the institutional list."""

    community = models.ForeignKey(Community, on_delete=models.PROTECT, related_name="categories")
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=80)
    description = models.CharField(max_length=500, blank=True, default="")
    position = models.PositiveSmallIntegerField(default=100)
    is_active = models.BooleanField(default=True)
    # Only staff (Portal / Community Admin, moderators, OIC, Lab Operator) may start threads (e.g. Announcements).
    staff_only_posting = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Discussion categories"
        ordering = ["community_id", "position", "name"]
        constraints = [
            models.UniqueConstraint(fields=["community", "slug"], name="connect_category_slug_unique"),
        ]

    def __str__(self) -> str:
        return self.name

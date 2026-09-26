"""
Research Groups: people, research activities, progress and update requests.

A group is deliberately separate from a workspace. Group membership never grants access to a
workspace or its files; linking a workspace, booking or publication to a group only records the
association. Rows are never hard-deleted by the API (members leave, categories are deactivated,
update requests are cancelled) so the history stays intact.
"""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class GroupStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    ARCHIVED = "ARCHIVED", "Archived"


class GroupRole(models.TextChoices):
    OWNER = "OWNER", "Owner"
    MANAGER = "MANAGER", "Manager"
    MEMBER = "MEMBER", "Member"


class GroupMemberType(models.TextChoices):
    PHD = "PHD", "Ph.D."
    MTECH = "MTECH", "M.Tech"
    BTECH = "BTECH", "B.Tech"
    RESEARCH_ASSOCIATE = "RESEARCH_ASSOCIATE", "Research Associate"
    JRF = "JRF", "JRF"
    SRF = "SRF", "SRF"
    PROJECT_STAFF = "PROJECT_STAFF", "Project Staff"
    INTERN = "INTERN", "Intern"
    OTHER = "OTHER", "Other"


class GroupMemberStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    LEFT = "LEFT", "Left"


class GroupActivityStatus(models.TextChoices):
    NOT_STARTED = "NOT_STARTED", "Not started"
    IN_PROGRESS = "IN_PROGRESS", "In progress"
    WAITING = "WAITING", "Waiting"
    SUBMITTED = "SUBMITTED", "Submitted"
    UNDER_REVIEW = "UNDER_REVIEW", "Under review"
    COMPLETED = "COMPLETED", "Completed"
    CANCELLED = "CANCELLED", "Cancelled"


class GroupActivityPriority(models.TextChoices):
    LOW = "LOW", "Low"
    NORMAL = "NORMAL", "Normal"
    HIGH = "HIGH", "High"


class UpdateRequestStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    SUBMITTED = "SUBMITTED", "Submitted"
    REVIEWED = "REVIEWED", "Reviewed"
    CANCELLED = "CANCELLED", "Cancelled"
    OVERDUE = "OVERDUE", "Overdue"


class UpdateRecurrence(models.TextChoices):
    """Only NONE is accepted in v1; the other values reserve the design for scheduled requests."""

    NONE = "NONE", "One-time"
    WEEKLY = "WEEKLY", "Weekly"
    FORTNIGHTLY = "FORTNIGHTLY", "Fortnightly"
    MONTHLY = "MONTHLY", "Monthly"


class AttachmentStatus(models.TextChoices):
    PENDING_UPLOAD = "PENDING_UPLOAD", "Pending upload"
    AVAILABLE = "AVAILABLE", "Available"
    FAILED = "FAILED", "Failed"
    DELETED = "DELETED", "Deleted"


class GroupEventAction(models.TextChoices):
    GROUP_CREATED = "GROUP_CREATED", "Group created"
    GROUP_UPDATED = "GROUP_UPDATED", "Group updated"
    GROUP_ARCHIVED = "GROUP_ARCHIVED", "Group archived"
    MEMBER_ADDED = "MEMBER_ADDED", "Member added"
    MEMBER_UPDATED = "MEMBER_UPDATED", "Member updated"
    MEMBER_REMOVED = "MEMBER_REMOVED", "Member removed"
    CATEGORY_CREATED = "CATEGORY_CREATED", "Category created"
    CATEGORY_UPDATED = "CATEGORY_UPDATED", "Category updated"
    ACTIVITY_CREATED = "ACTIVITY_CREATED", "Activity created"
    ACTIVITY_ASSIGNED = "ACTIVITY_ASSIGNED", "Activity assigned"
    ACTIVITY_UNASSIGNED = "ACTIVITY_UNASSIGNED", "Activity unassigned"
    ACTIVITY_UPDATED = "ACTIVITY_UPDATED", "Activity updated"
    ACTIVITY_COMPLETED = "ACTIVITY_COMPLETED", "Activity completed"
    PROGRESS_UPDATED = "PROGRESS_UPDATED", "Progress updated"
    UPDATE_REQUESTED = "UPDATE_REQUESTED", "Update requested"
    UPDATE_SUBMITTED = "UPDATE_SUBMITTED", "Update submitted"
    UPDATE_REVIEWED = "UPDATE_REVIEWED", "Update reviewed"
    UPDATE_CANCELLED = "UPDATE_CANCELLED", "Update request cancelled"
    WORKSPACE_LINKED = "WORKSPACE_LINKED", "Workspace linked"
    WORKSPACE_UNLINKED = "WORKSPACE_UNLINKED", "Workspace unlinked"
    PUBLICATION_LINKED = "PUBLICATION_LINKED", "Publication linked"
    PUBLICATION_UNLINKED = "PUBLICATION_UNLINKED", "Publication unlinked"


class ResearchGroup(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    short_code = models.CharField(max_length=20, blank=True, default="")
    description = models.TextField(blank=True, default="")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="owned_research_groups")
    status = models.CharField(max_length=20, choices=GroupStatus.choices, default=GroupStatus.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["owner", "status"], name="mr_grp_owner_status_idx"),
            models.Index(fields=["status"], name="mr_grp_status_idx"),
        ]

    def __str__(self):
        return self.name

    @property
    def is_archived(self) -> bool:
        return self.status == GroupStatus.ARCHIVED


class ResearchGroupCategory(models.Model):
    group = models.ForeignKey(ResearchGroup, on_delete=models.PROTECT, related_name="categories")
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=500, blank=True, default="")
    display_order = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_order", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["group", "name"], condition=Q(active=True), name="mr_grp_uniq_active_category"
            ),
        ]
        indexes = [models.Index(fields=["group", "active", "display_order"], name="mr_grp_category_idx")]

    def __str__(self):
        return self.name


class ResearchGroupMember(models.Model):
    group = models.ForeignKey(ResearchGroup, on_delete=models.PROTECT, related_name="members")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="research_group_memberships")
    role = models.CharField(max_length=10, choices=GroupRole.choices, default=GroupRole.MEMBER)
    member_type = models.CharField(max_length=30, choices=GroupMemberType.choices, default=GroupMemberType.OTHER)
    category = models.ForeignKey(
        ResearchGroupCategory, null=True, blank=True, on_delete=models.SET_NULL, related_name="members"
    )
    status = models.CharField(max_length=10, choices=GroupMemberStatus.choices, default=GroupMemberStatus.ACTIVE)
    joined_at = models.DateTimeField(auto_now_add=True)
    left_at = models.DateTimeField(null=True, blank=True)
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    removed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["joined_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["group", "user"], condition=Q(status="ACTIVE"), name="mr_grp_uniq_active_member"
            ),
        ]
        indexes = [
            models.Index(fields=["user", "status"], name="mr_grp_member_user_idx"),
            models.Index(fields=["group", "status"], name="mr_grp_member_group_idx"),
        ]


class ResearchGroupWorkspace(models.Model):
    """Association only: it never grants access to the workspace."""

    group = models.ForeignKey(ResearchGroup, on_delete=models.CASCADE, related_name="workspace_links")
    workspace = models.ForeignKey(
        "my_research.ResearchWorkspace", on_delete=models.CASCADE, related_name="group_links"
    )
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-added_at"]
        constraints = [models.UniqueConstraint(fields=["group", "workspace"], name="mr_grp_uniq_workspace")]


class ResearchGroupPublication(models.Model):
    group = models.ForeignKey(ResearchGroup, on_delete=models.CASCADE, related_name="publication_links")
    claim = models.ForeignKey(
        "equipment.EquipmentPublicationClaim", on_delete=models.CASCADE, related_name="research_group_links"
    )
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-added_at"]
        constraints = [models.UniqueConstraint(fields=["group", "claim"], name="mr_grp_uniq_publication")]


class ResearchGroupActivity(models.Model):
    """A research activity (work item). Named apart from the existing workspace log `ResearchActivity`."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    group = models.ForeignKey(ResearchGroup, on_delete=models.PROTECT, related_name="activities")
    title = models.CharField(max_length=250)
    description = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    category = models.ForeignKey(
        ResearchGroupCategory, null=True, blank=True, on_delete=models.SET_NULL, related_name="activities"
    )
    status = models.CharField(max_length=20, choices=GroupActivityStatus.choices, default=GroupActivityStatus.NOT_STARTED)
    priority = models.CharField(max_length=10, choices=GroupActivityPriority.choices, default=GroupActivityPriority.NORMAL)
    progress_percent = models.PositiveSmallIntegerField(default=0)
    start_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    workspace = models.ForeignKey(
        "my_research.ResearchWorkspace", null=True, blank=True, on_delete=models.SET_NULL, related_name="group_activities"
    )
    equipment = models.ForeignKey(
        "equipment.Equipment", null=True, blank=True, on_delete=models.SET_NULL, related_name="research_group_activities"
    )
    booking = models.ForeignKey(
        "equipment.Booking", null=True, blank=True, on_delete=models.SET_NULL, related_name="research_group_activities"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["group", "status"], name="mr_grp_act_status_idx"),
            models.Index(fields=["group", "due_date"], name="mr_grp_act_due_idx"),
        ]

    def __str__(self):
        return self.title


class ResearchGroupActivityAssignee(models.Model):
    """Individual assignment: each person keeps their own status and progress."""

    activity = models.ForeignKey(ResearchGroupActivity, on_delete=models.CASCADE, related_name="assignees")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="research_group_assignments")
    status = models.CharField(max_length=20, choices=GroupActivityStatus.choices, default=GroupActivityStatus.NOT_STARTED)
    progress_percent = models.PositiveSmallIntegerField(default=0)
    note = models.TextField(blank=True, default="")
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    assigned_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    removed_at = models.DateTimeField(null=True, blank=True)
    due_reminder_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["assigned_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["activity", "user"], condition=Q(removed_at__isnull=True), name="mr_grp_uniq_active_assignee"
            ),
        ]
        indexes = [models.Index(fields=["user", "removed_at"], name="mr_grp_assignee_user_idx")]


class ResearchUpdateRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    group = models.ForeignKey(ResearchGroup, on_delete=models.PROTECT, related_name="update_requests")
    activity = models.ForeignKey(
        ResearchGroupActivity, null=True, blank=True, on_delete=models.SET_NULL, related_name="update_requests"
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="research_update_requests"
    )
    title = models.CharField(max_length=250)
    instructions = models.TextField(blank=True, default="")
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=UpdateRequestStatus.choices, default=UpdateRequestStatus.PENDING)
    recurrence = models.CharField(max_length=20, choices=UpdateRecurrence.choices, default=UpdateRecurrence.NONE)
    recurrence_parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="recurrences"
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    review_comment = models.TextField(blank=True, default="")
    cancelled_at = models.DateTimeField(null=True, blank=True)
    overdue_notified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-requested_at"]
        indexes = [
            models.Index(fields=["group", "status"], name="mr_grp_req_status_idx"),
            models.Index(fields=["assigned_to", "status"], name="mr_grp_req_assignee_idx"),
            models.Index(fields=["status", "due_date"], name="mr_grp_req_due_idx"),
        ]


class ResearchUpdate(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.OneToOneField(ResearchUpdateRequest, on_delete=models.PROTECT, related_name="submission")
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    work_completed = models.TextField(blank=True, default="")
    current_status = models.TextField(blank=True, default="")
    blockers = models.TextField(blank=True, default="")
    next_steps = models.TextField(blank=True, default="")
    progress_percent = models.PositiveSmallIntegerField(null=True, blank=True)
    expected_completion_date = models.DateField(null=True, blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)


class ResearchUpdateAttachment(models.Model):
    """Stored in the existing private My Research bucket under the configured research prefix."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(ResearchUpdateRequest, on_delete=models.PROTECT, related_name="attachments")
    update = models.ForeignKey(
        ResearchUpdate, null=True, blank=True, on_delete=models.PROTECT, related_name="attachments"
    )
    original_name = models.CharField(max_length=255)
    display_name = models.CharField(max_length=255)
    storage_key = models.CharField(max_length=1024, unique=True)
    declared_content_type = models.CharField(max_length=150, blank=True, default="")
    detected_type = models.CharField(max_length=30, blank=True, default="")
    size_bytes = models.BigIntegerField()
    status = models.CharField(max_length=20, choices=AttachmentStatus.choices, default=AttachmentStatus.PENDING_UPLOAD)
    failure_reason = models.CharField(max_length=255, blank=True, default="")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    uploaded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["request", "status"], name="mr_grp_attach_req_idx")]


class ResearchGroupEvent(models.Model):
    """Group activity feed. `subject_user` is the person an event concerns (None = group-wide)."""

    group = models.ForeignKey(ResearchGroup, on_delete=models.CASCADE, related_name="events")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    subject_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    action = models.CharField(max_length=40, choices=GroupEventAction.choices)
    target_type = models.CharField(max_length=20, blank=True, default="")
    target_id = models.CharField(max_length=64, blank=True, default="")
    target_label = models.CharField(max_length=300, blank=True, default="")
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["group", "-created_at"], name="mr_grp_event_idx"),
            models.Index(fields=["subject_user", "-created_at"], name="mr_grp_event_subject_idx"),
        ]

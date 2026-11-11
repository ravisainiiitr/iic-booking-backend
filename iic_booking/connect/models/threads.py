"""Threads, posts, tags, attachments, accepted solutions, polls and drafts."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models
from django.db.models import Q

from .base import AppendOnlyModel
from .base import Visibility
from .communities import Community
from .communities import DiscussionCategory


class ThreadType(models.TextChoices):
    QUESTION = "question", "Question"
    TECHNICAL_ISSUE = "technical_issue", "Technical Issue"
    SOLUTION = "solution", "Solution / Knowledge Sharing"
    GENERAL = "general", "General Discussion"
    SUGGESTION = "suggestion", "Suggestion"
    FEATURE_REQUEST = "feature_request", "Feature Request"
    ANNOUNCEMENT = "announcement", "Announcement"
    RESEARCH = "research", "Research Discussion"
    POLL = "poll", "Poll"


SOLVABLE_TYPES = frozenset({ThreadType.QUESTION, ThreadType.TECHNICAL_ISSUE})


class ThreadStatus(models.TextChoices):
    OPEN = "open", "Open"
    LOCKED = "locked", "Locked"


class DiscussionTag(models.Model):
    name = models.CharField(max_length=40)
    slug = models.SlugField(max_length=48, unique=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class DiscussionThread(models.Model):
    community = models.ForeignKey(Community, on_delete=models.PROTECT, related_name="threads")
    category = models.ForeignKey(
        DiscussionCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name="threads"
    )
    # Permanent link by equipment id: the FK may be nulled, ``equipment_ref_id`` never is.
    equipment = models.ForeignKey(
        "equipment.Equipment", on_delete=models.SET_NULL, null=True, blank=True, related_name="connect_threads"
    )
    equipment_ref_id = models.IntegerField(null=True, blank=True)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="connect_threads")
    type = models.CharField(max_length=24, choices=ThreadType.choices, default=ThreadType.GENERAL)
    title = models.CharField(max_length=200)
    status = models.CharField(max_length=12, choices=ThreadStatus.choices, default=ThreadStatus.OPEN)
    visibility = models.CharField(max_length=12, choices=Visibility.choices, default=Visibility.VISIBLE)
    tags = models.ManyToManyField(DiscussionTag, through="ThreadTag", related_name="threads", blank=True)

    is_pinned = models.BooleanField(default=False)
    pinned_at = models.DateTimeField(null=True, blank=True)
    pinned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    # Operator / OIC tools (never touch equipment availability).
    is_known_problem = models.BooleanField(default=False)
    has_workaround = models.BooleanField(default=False)
    is_outdated = models.BooleanField(default=False)
    marked_resolved_at = models.DateTimeField(null=True, blank=True)
    marked_resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    solved_at = models.DateTimeField(null=True, blank=True)
    accepted_post = models.ForeignKey(
        "DiscussionPost", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    poll_closes_at = models.DateTimeField(null=True, blank=True)

    reply_count = models.PositiveIntegerField(default=0)
    view_count = models.PositiveIntegerField(default=0)
    helpful_count = models.PositiveIntegerField(default=0)
    participant_count = models.PositiveIntegerField(default=1)
    last_activity_at = models.DateTimeField(db_index=True)
    last_post = models.ForeignKey("DiscussionPost", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    search_vector = SearchVectorField(null=True, editable=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    edited_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-last_activity_at"]
        indexes = [
            models.Index(fields=["community", "visibility", "-last_activity_at"], name="connect_thread_feed"),
            models.Index(fields=["equipment_ref_id", "-last_activity_at"], name="connect_thread_equipment"),
            models.Index(fields=["community", "type", "solved_at"], name="connect_thread_solved"),
            models.Index(fields=["author", "-created_at"], name="connect_thread_author"),
            GinIndex(fields=["search_vector"], name="connect_thread_search"),
        ]

    def __str__(self) -> str:
        return self.title

    @property
    def is_solvable(self) -> bool:
        return self.type in SOLVABLE_TYPES


class ThreadTag(models.Model):
    thread = models.ForeignKey(DiscussionThread, on_delete=models.CASCADE, related_name="thread_tags")
    tag = models.ForeignKey(DiscussionTag, on_delete=models.CASCADE, related_name="thread_tags")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["thread", "tag"], name="connect_thread_tag_unique")]
        indexes = [models.Index(fields=["tag", "thread"], name="connect_thread_tag_tag")]


class DiscussionPost(models.Model):
    """Post #1 of a thread (``is_opening``) holds the thread body; replies may answer another post (``parent``)."""

    thread = models.ForeignKey(DiscussionThread, on_delete=models.PROTECT, related_name="posts")
    parent = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="replies")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="connect_posts")
    is_opening = models.BooleanField(default=False)
    body_html = models.TextField()
    body_text = models.TextField(blank=True, default="")
    visibility = models.CharField(max_length=12, choices=Visibility.choices, default=Visibility.VISIBLE)
    # Verified role badge of the author in this community when the post was written (never user-chosen).
    author_badge = models.CharField(max_length=40, blank=True, default="")
    is_verified_response = models.BooleanField(default=False)
    helpful_count = models.PositiveIntegerField(default=0)
    edit_count = models.PositiveIntegerField(default=0)
    edited_at = models.DateTimeField(null=True, blank=True)
    search_vector = SearchVectorField(null=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["thread"], condition=Q(is_opening=True), name="connect_one_opening_post"),
        ]
        indexes = [
            models.Index(fields=["thread", "created_at"], name="connect_post_thread"),
            models.Index(fields=["author", "-created_at"], name="connect_post_author"),
            GinIndex(fields=["search_vector"], name="connect_post_search"),
        ]

    def __str__(self) -> str:
        return f"post {self.pk} in thread {self.thread_id}"


class DiscussionPostRevision(AppendOnlyModel):
    """Edit history: the content *before* each edit (title only for opening posts)."""

    post = models.ForeignKey(DiscussionPost, on_delete=models.PROTECT, related_name="revisions")
    editor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    title = models.CharField(max_length=200, blank=True, default="")
    body_html = models.TextField()
    reason = models.CharField(max_length=300, blank=True, default="")
    by_moderator = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class DiscussionDraft(models.Model):
    """Autosaved editor content. ``context_key`` is ``new``, ``new:equipment:<id>`` or ``reply:<thread id>``."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="connect_drafts")
    context_key = models.CharField(max_length=80)
    payload = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "context_key"], name="connect_draft_unique")]


class DiscussionAttachment(models.Model):
    """Privately stored upload. Served only through the authorised download endpoint, never by public URL.

    Uploaded first (no post yet), then bound to a post on submit. Unbound uploads older than a day are purged.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    uploader = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="connect_uploads")
    # Community chosen at upload time; binding to a post of another community is refused.
    community = models.ForeignKey(Community, on_delete=models.PROTECT, related_name="attachments")
    post = models.ForeignKey(DiscussionPost, on_delete=models.PROTECT, null=True, blank=True, related_name="attachments")
    storage_key = models.CharField(max_length=512, unique=True)
    original_name = models.CharField(max_length=200)
    content_type = models.CharField(max_length=100)
    sniffed_kind = models.CharField(max_length=20, blank=True, default="")
    size = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    is_inline_image = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    attached_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["post"], name="connect_attachment_post"),
            models.Index(fields=["uploader", "attached_at"], name="connect_attachment_unbound"),
        ]

    def __str__(self) -> str:
        return self.original_name


class AcceptedSolution(models.Model):
    """The current accepted answer of a Question / Technical Issue thread (history in ModerationAction)."""

    thread = models.OneToOneField(DiscussionThread, on_delete=models.CASCADE, related_name="accepted_solution")
    post = models.ForeignKey(DiscussionPost, on_delete=models.PROTECT, related_name="+")
    accepted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    accepted_at = models.DateTimeField(auto_now_add=True)


class DiscussionPollOption(models.Model):
    thread = models.ForeignKey(DiscussionThread, on_delete=models.CASCADE, related_name="poll_options")
    label = models.CharField(max_length=200)
    position = models.PositiveSmallIntegerField(default=0)
    vote_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]


class DiscussionPollVote(models.Model):
    thread = models.ForeignKey(DiscussionThread, on_delete=models.CASCADE, related_name="poll_votes")
    option = models.ForeignKey(DiscussionPollOption, on_delete=models.CASCADE, related_name="votes")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["thread", "user"], name="connect_one_vote_per_poll")]

"""Knowledge Hub articles (kept separate from discussions) and community announcements."""

from __future__ import annotations

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models

from .base import AppendOnlyModel
from .communities import Community
from .communities import DiscussionCategory
from .threads import DiscussionPost
from .threads import DiscussionTag
from .threads import DiscussionThread


class ArticleStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    UNDER_REVIEW = "under_review", "Under Review"
    PUBLISHED = "published", "Published"
    ARCHIVED = "archived", "Archived"


class KnowledgeArticle(models.Model):
    """Structured article promoted from a discussion or accepted answer.

    ``community`` is copied from the source thread and never changed afterwards, so an article can never be
    readable by anyone who could not read its source (access is inherited, never widened).
    """

    community = models.ForeignKey(Community, on_delete=models.PROTECT, related_name="articles")
    source_thread = models.ForeignKey(
        DiscussionThread, on_delete=models.SET_NULL, null=True, blank=True, related_name="articles"
    )
    source_post = models.ForeignKey(
        DiscussionPost, on_delete=models.SET_NULL, null=True, blank=True, related_name="articles"
    )
    category = models.ForeignKey(
        DiscussionCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name="articles"
    )
    equipment = models.ForeignKey(
        "equipment.Equipment", on_delete=models.SET_NULL, null=True, blank=True, related_name="connect_articles"
    )
    equipment_ref_id = models.IntegerField(null=True, blank=True, db_index=True)
    tags = models.ManyToManyField(DiscussionTag, blank=True, related_name="articles")
    title = models.CharField(max_length=200)
    summary = models.CharField(max_length=600, blank=True, default="")
    body_html = models.TextField()
    body_text = models.TextField(blank=True, default="")
    status = models.CharField(max_length=16, choices=ArticleStatus.choices, default=ArticleStatus.DRAFT)
    # Shown as "Reviewed institutional guidance" only after an approved review by an authorised reviewer.
    is_institutional_guidance = models.BooleanField(default=False)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="connect_articles")
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    version = models.PositiveIntegerField(default=1)
    view_count = models.PositiveIntegerField(default=0)
    published_at = models.DateTimeField(null=True, blank=True)
    last_reviewed_at = models.DateTimeField(null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    search_vector = SearchVectorField(null=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["community", "status", "-published_at"], name="connect_article_feed"),
            GinIndex(fields=["search_vector"], name="connect_article_search"),
        ]

    def __str__(self) -> str:
        return self.title


class KnowledgeArticleRevision(AppendOnlyModel):
    article = models.ForeignKey(KnowledgeArticle, on_delete=models.PROTECT, related_name="revisions")
    version = models.PositiveIntegerField()
    title = models.CharField(max_length=200)
    summary = models.CharField(max_length=600, blank=True, default="")
    body_html = models.TextField()
    status = models.CharField(max_length=16)
    editor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    change_note = models.CharField(max_length=300, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version"]


class ReviewDecision(models.TextChoices):
    APPROVED = "approved", "Approved"
    CHANGES_REQUESTED = "changes_requested", "Changes requested"
    REJECTED = "rejected", "Rejected"


class KnowledgeArticleReview(AppendOnlyModel):
    article = models.ForeignKey(KnowledgeArticle, on_delete=models.PROTECT, related_name="reviews")
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    decision = models.CharField(max_length=20, choices=ReviewDecision.choices)
    comment = models.TextField(blank=True, default="")
    article_version = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class AnnouncementPriority(models.TextChoices):
    NORMAL = "normal", "Normal"
    IMPORTANT = "important", "Important"
    CRITICAL = "critical", "Critical"


class CommunityAnnouncement(models.Model):
    """Extra fields of an Announcement thread. Announcements never change bookings or availability."""

    thread = models.OneToOneField(DiscussionThread, on_delete=models.CASCADE, related_name="announcement")
    priority = models.CharField(max_length=12, choices=AnnouncementPriority.choices, default=AnnouncementPriority.NORMAL)
    expires_at = models.DateTimeField(null=True, blank=True)
    requires_acknowledgement = models.BooleanField(default=False)
    # Optional narrower audience inside the community: users who booked this equipment (plus its staff).
    target_equipment = models.ForeignKey(
        "equipment.Equipment", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    notified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["expires_at"], name="connect_announcement_expiry")]


class AnnouncementAcknowledgement(models.Model):
    announcement = models.ForeignKey(CommunityAnnouncement, on_delete=models.CASCADE, related_name="acknowledgements")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    acknowledged_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["announcement", "user"], name="connect_ack_unique")]

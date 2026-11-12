"""Per-user actions: reactions, follows, bookmarks, read state and reports."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from .communities import Community
from .threads import DiscussionPost
from .threads import DiscussionThread


class ReactionKind(models.TextChoices):
    HELPFUL = "helpful", "Helpful"
    INSIGHTFUL = "insightful", "Insightful"
    THANKS = "thanks", "Thanks"


class DiscussionReaction(models.Model):
    post = models.ForeignKey(DiscussionPost, on_delete=models.CASCADE, related_name="reactions")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="connect_reactions")
    kind = models.CharField(max_length=16, choices=ReactionKind.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["post", "user", "kind"], name="connect_reaction_unique")]
        indexes = [models.Index(fields=["user", "created_at"], name="connect_reaction_user")]


class SubscriptionLevel(models.TextChoices):
    WATCHING = "watching", "Watching (all activity)"
    MUTED = "muted", "Muted"


class DiscussionSubscription(models.Model):
    """Follow a thread or a whole community (exactly one target)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="connect_subscriptions")
    thread = models.ForeignKey(
        DiscussionThread, on_delete=models.CASCADE, null=True, blank=True, related_name="subscriptions"
    )
    community = models.ForeignKey(
        Community, on_delete=models.CASCADE, null=True, blank=True, related_name="subscriptions"
    )
    level = models.CharField(max_length=12, choices=SubscriptionLevel.choices, default=SubscriptionLevel.WATCHING)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(Q(thread__isnull=False) & Q(community__isnull=True))
                | (Q(thread__isnull=True) & Q(community__isnull=False)),
                name="connect_subscription_one_target",
            ),
            models.UniqueConstraint(
                fields=["user", "thread"], condition=Q(thread__isnull=False), name="connect_sub_thread_unique"
            ),
            models.UniqueConstraint(
                fields=["user", "community"], condition=Q(community__isnull=False), name="connect_sub_comm_unique"
            ),
        ]


class DiscussionBookmark(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="connect_bookmarks")
    thread = models.ForeignKey(DiscussionThread, on_delete=models.CASCADE, related_name="bookmarks")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "thread"], name="connect_bookmark_unique")]


class ThreadReadState(models.Model):
    """Last visit per user per thread: dedupes view counts and drives unread markers."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    thread = models.ForeignKey(DiscussionThread, on_delete=models.CASCADE, related_name="read_states")
    last_viewed_at = models.DateTimeField()
    last_read_post_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "thread"], name="connect_read_state_unique")]


class ReportReason(models.TextChoices):
    SPAM = "spam", "Spam or advertising"
    ABUSE = "abuse", "Abusive or disrespectful"
    OFF_TOPIC = "off_topic", "Off-topic"
    UNSAFE = "unsafe", "Unsafe or incorrect technical advice"
    CONFIDENTIAL = "confidential", "Confidential or personal information"
    OTHER = "other", "Other"


class ReportStatus(models.TextChoices):
    OPEN = "open", "Open"
    ACTIONED = "actioned", "Actioned"
    DISMISSED = "dismissed", "Dismissed"


class DiscussionReport(models.Model):
    post = models.ForeignKey(DiscussionPost, on_delete=models.CASCADE, related_name="reports")
    reporter = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    reason = models.CharField(max_length=16, choices=ReportReason.choices)
    details = models.CharField(max_length=1000, blank=True, default="")
    status = models.CharField(max_length=12, choices=ReportStatus.choices, default=ReportStatus.OPEN)
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    handled_at = models.DateTimeField(null=True, blank=True)
    resolution_note = models.CharField(max_length=1000, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["post", "reporter"], condition=Q(status="open"), name="connect_one_open_report"
            ),
        ]
        indexes = [models.Index(fields=["status", "-created_at"], name="connect_report_queue")]

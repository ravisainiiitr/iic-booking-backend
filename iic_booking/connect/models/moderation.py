"""Moderation audit, Connect notifications and preferences, search log, recognition and module settings."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from .base import AppendOnlyModel
from .communities import Community
from .threads import DiscussionPost
from .threads import DiscussionThread


class ModerationAction(AppendOnlyModel):
    """Every moderator / admin / author-policy action, with the state before and after."""

    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    community = models.ForeignKey(Community, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    action = models.CharField(max_length=40)
    target_type = models.CharField(max_length=24)
    target_id = models.CharField(max_length=64)
    reason = models.CharField(max_length=1000, blank=True, default="")
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["target_type", "target_id"], name="connect_modaction_target"),
            models.Index(fields=["community", "-created_at"], name="connect_modaction_comm"),
        ]


class NotificationEvent(models.TextChoices):
    REPLY = "reply", "New reply in a thread you started"
    REPLY_TO_ME = "reply_to_me", "Reply to your post"
    MENTION = "mention", "You were mentioned"
    ACCEPTED = "accepted", "Your answer was accepted"
    FOLLOWED = "followed", "Activity in something you follow"
    ANNOUNCEMENT = "announcement", "Announcement"
    ARTICLE_PUBLISHED = "article_published", "Knowledge article published"
    MODERATION = "moderation", "Moderation of your content"


class EmailMode(models.TextChoices):
    IMMEDIATE = "immediate", "Immediately"
    DAILY = "daily", "Daily digest"
    WEEKLY = "weekly", "Weekly digest"
    OFF = "off", "No email"


class DiscussionNotification(models.Model):
    """Connect's own notification record. In-app delivery goes through the portal bell; email per preference.

    Access is re-checked when the notification is delivered and when a digest is built.
    """

    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="connect_notifications")
    event = models.CharField(max_length=24, choices=NotificationEvent.choices)
    community = models.ForeignKey(Community, on_delete=models.CASCADE, related_name="+")
    thread = models.ForeignKey(DiscussionThread, on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    post = models.ForeignKey(DiscussionPost, on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    article_id = models.BigIntegerField(null=True, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    email_mode = models.CharField(max_length=12, choices=EmailMode.choices, default=EmailMode.OFF)
    in_app_sent_at = models.DateTimeField(null=True, blank=True)
    email_sent_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["recipient", "event", "post"], condition=Q(post__isnull=False), name="connect_notif_once_per_post"
            ),
        ]
        indexes = [
            models.Index(fields=["recipient", "read_at", "-created_at"], name="connect_notif_inbox"),
            models.Index(fields=["email_mode", "email_sent_at"], name="connect_notif_digest"),
        ]


class ConnectNotificationPreference(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="connect_preference")
    email_mode = models.CharField(max_length=12, choices=EmailMode.choices, default=EmailMode.DAILY)
    in_app_enabled = models.BooleanField(default=True)
    # Events the user switched off (all on by default).
    muted_events = models.JSONField(default=list, blank=True)
    auto_follow_own_threads = models.BooleanField(default=True)
    auto_follow_replied_threads = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)


class ConnectSearchLog(models.Model):
    """Normalised search queries for "top searches" (admins only, aggregated with a minimum count)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    query = models.CharField(max_length=200)
    kind = models.CharField(max_length=16, default="search")  # search | suggest
    result_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["-created_at"], name="connect_searchlog_time")]


class RecognitionBadge(models.Model):
    code = models.SlugField(max_length=40, unique=True)
    name = models.CharField(max_length=80)
    description = models.CharField(max_length=300)
    # e.g. {"accepted_answers": 5} / {"distinct_helpful_users": 10} / {"approved_articles": 2}
    criteria = models.JSONField(default=dict)
    is_active = models.BooleanField(default=True)
    position = models.PositiveSmallIntegerField(default=100)

    class Meta:
        ordering = ["position", "name"]

    def __str__(self) -> str:
        return self.name


class UserBadgeAward(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="connect_badges")
    badge = models.ForeignKey(RecognitionBadge, on_delete=models.CASCADE, related_name="awards")
    # Null = earned across all of Connect.
    community = models.ForeignKey(Community, on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    evidence = models.JSONField(default=dict, blank=True)
    awarded_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "badge", "community"], nulls_distinct=False, name="connect_badge_award_unique"
            ),
        ]


class ConnectSettings(models.Model):
    """Singleton (pk=1) of Main Administrator settings. Defaults are conservative."""

    recognition_enabled = models.BooleanField(default=False)
    # Authors may edit their own posts for this long (moderators any time, with audit). 0 = no limit.
    edit_window_minutes = models.PositiveIntegerField(default=1440)
    # Authors may delete their own post while it has no replies, or within this window.
    delete_window_minutes = models.PositiveIntegerField(default=60)
    max_attachment_mb = models.PositiveSmallIntegerField(default=20)
    max_attachments_per_post = models.PositiveSmallIntegerField(default=10)
    min_account_age_hours_to_post = models.PositiveIntegerField(default=0)
    weekly_digest_weekday = models.PositiveSmallIntegerField(default=0)  # 0 = Monday
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "IITR Connect settings"
        verbose_name_plural = "IITR Connect settings"

    @classmethod
    def load(cls) -> "ConnectSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

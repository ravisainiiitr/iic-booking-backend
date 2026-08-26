"""Research Copilot domain models — conversations, messages, audit, knowledge gaps."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class ConversationAccessMode(models.TextChoices):
    PUBLIC = "public", _("Public")
    AUTHENTICATED = "authenticated", _("Authenticated")


class Conversation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="research_copilot_conversations",
        null=True,
        blank=True,
    )
    title = models.CharField(max_length=255, blank=True, default="")
    user_role_snapshot = models.CharField(max_length=64, blank=True, default="")
    department_id_snapshot = models.IntegerField(null=True, blank=True)
    # Present on production DB (NOT NULL). Model alignment so creates do not omit columns.
    access_mode = models.CharField(
        max_length=32,
        choices=ConversationAccessMode.choices,
        default=ConversationAccessMode.AUTHENTICATED,
    )
    anonymous_session_key = models.CharField(max_length=64, blank=True, default="")
    is_archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["user", "-updated_at"]),
        ]

    def __str__(self) -> str:
        return self.title or f"Conversation {self.id}"


class MessageRole(models.TextChoices):
    USER = "user", _("User")
    ASSISTANT = "assistant", _("Assistant")
    SYSTEM = "system", _("System")


class Message(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    role = models.CharField(max_length=16, choices=MessageRole.choices)
    content = models.TextField()
    confidence = models.FloatField(null=True, blank=True)
    citations = models.JSONField(default=list, blank=True)
    suggested_actions = models.JSONField(default=list, blank=True)
    escalate_hint = models.BooleanField(default=False)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["conversation", "created_at"]),
        ]


class FeedbackRating(models.TextChoices):
    UP = "up", _("Thumbs up")
    DOWN = "down", _("Thumbs down")


class MessageFeedback(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name="feedback",
    )
    message = models.ForeignKey(
        Message,
        on_delete=models.CASCADE,
        related_name="feedback",
        null=True,
        blank=True,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="research_copilot_feedback",
    )
    rating = models.CharField(max_length=8, choices=FeedbackRating.choices)
    comment = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)


class AuditAction(models.TextChoices):
    CONVERSATION_CREATED = "conversation_created", _("Conversation Created")
    MESSAGE_SENT = "message_sent", _("Message Sent")
    MESSAGE_REPLIED = "message_replied", _("Message Replied")
    STREAM_STARTED = "stream_started", _("Stream Started")
    FEEDBACK = "feedback", _("Feedback")
    ESCALATE_HINT = "escalate_hint", _("Escalate Hint")
    FEATURE_DISABLED = "feature_disabled", _("Feature Disabled")
    TOOL_EXECUTED = "tool_executed", _("Tool Executed")
    TOOL_DENIED = "tool_denied", _("Tool Denied")
    ERROR = "error", _("Error")
    PROVIDER_UNAVAILABLE = "provider_unavailable", _("Provider Unavailable")
    TIMEOUT = "timeout", _("Timeout")
    BUSY = "busy", _("Busy")


class CopilotAuditEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    action = models.CharField(max_length=32, choices=AuditAction.choices, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="research_copilot_audit_events",
    )
    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_events",
    )
    message = models.CharField(max_length=512, blank=True, default="")
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]


class KnowledgeGap(models.Model):
    """Capture unresolved / low-confidence topics for FAQ suggestions (AI.2 / AI.8)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="knowledge_gaps",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    query_summary = models.CharField(max_length=512, blank=True, default="")
    reason = models.CharField(max_length=64, blank=True, default="")
    suggested_faq = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)


class SecurityLevel(models.TextChoices):
    PUBLIC = "public", _("Public")
    AUTHENTICATED = "authenticated", _("Authenticated Users")
    OPERATOR = "operator", _("Operator / Lab Staff")
    DEPT_ADMIN = "dept_admin", _("Department Admin")
    ADMIN = "admin", _("Institute Administrator")


class DocumentCategory(models.TextChoices):
    USER_GUIDE = "user_guide", _("User Guide")
    OPERATOR_MANUAL = "operator_manual", _("Operator Manual")
    SOP = "sop", _("SOP")
    TROUBLESHOOTING = "troubleshooting", _("Troubleshooting")
    DEPLOYMENT = "deployment", _("Deployment Guide")
    RELEASE_NOTES = "release_notes", _("Release Notes")
    FAQ = "faq", _("FAQ")
    POLICY = "policy", _("Policy")
    TRAINING = "training", _("Training Material")
    EQUIPMENT = "equipment", _("Equipment Knowledge")
    KNOWN_ISSUES = "known_issues", _("Known Issues")
    OTHER = "other", _("Other")


class DocumentStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    ACTIVE = "active", _("Active")
    ARCHIVED = "archived", _("Archived")
    FAILED = "failed", _("Failed")


class IndexStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    INDEXING = "indexing", _("Indexing")
    INDEXED = "indexed", _("Indexed")
    STALE = "stale", _("Stale")
    FAILED = "failed", _("Failed")


class KnowledgeDocument(models.Model):
    """Ingested knowledge article or uploaded document (Phase AI.2)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=512)
    source_type = models.CharField(max_length=32, default="markdown")  # pdf|docx|md|html|txt|csv|json|article
    category = models.CharField(max_length=32, choices=DocumentCategory.choices, default=DocumentCategory.OTHER)
    security_level = models.CharField(
        max_length=32, choices=SecurityLevel.choices, default=SecurityLevel.AUTHENTICATED, db_index=True
    )
    status = models.CharField(max_length=16, choices=DocumentStatus.choices, default=DocumentStatus.ACTIVE)
    index_status = models.CharField(max_length=16, choices=IndexStatus.choices, default=IndexStatus.PENDING)
    version = models.CharField(max_length=64, blank=True, default="1.0")
    language = models.CharField(max_length=16, blank=True, default="en")
    tags = models.JSONField(default=list, blank=True)
    department_id = models.IntegerField(null=True, blank=True, db_index=True)
    equipment_id = models.IntegerField(null=True, blank=True, db_index=True)
    source_uri = models.CharField(max_length=1024, blank=True, default="")
    external_url = models.URLField(blank=True, default="")
    content_text = models.TextField(blank=True, default="")
    content_hash = models.CharField(max_length=64, blank=True, default="", db_index=True)
    embedding_version = models.CharField(max_length=64, blank=True, default="")
    chunk_count = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="knowledge_documents_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    indexed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["category", "security_level", "status"]),
            models.Index(fields=["index_status", "status"]),
        ]

    def __str__(self) -> str:
        return self.title


class KnowledgeChunk(models.Model):
    """Chunk of a knowledge document with optional embedding vector (JSON for portable store)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(KnowledgeDocument, on_delete=models.CASCADE, related_name="chunks")
    chunk_index = models.PositiveIntegerField(default=0)
    content = models.TextField()
    token_estimate = models.PositiveIntegerField(default=0)
    metadata = models.JSONField(default=dict, blank=True)
    # Portable vector representation — provider adapters may sync to pgvector/Qdrant/etc.
    embedding = models.JSONField(default=list, blank=True)
    embedding_model = models.CharField(max_length=128, blank=True, default="")
    embedding_version = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["document_id", "chunk_index"]
        indexes = [
            models.Index(fields=["document", "chunk_index"]),
        ]


class EmbeddingJob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(
        KnowledgeDocument, on_delete=models.CASCADE, related_name="embedding_jobs", null=True, blank=True
    )
    job_type = models.CharField(max_length=32, default="index")  # index|reindex|rebuild_all
    status = models.CharField(max_length=16, choices=IndexStatus.choices, default=IndexStatus.PENDING)
    provider = models.CharField(max_length=64, blank=True, default="")
    detail = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)


class SearchQueryLog(models.Model):
    """Search analytics + self-improvement signals."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    conversation = models.ForeignKey(
        Conversation, on_delete=models.SET_NULL, null=True, blank=True
    )
    query = models.CharField(max_length=1024)
    intent = models.CharField(max_length=64, blank=True, default="")
    role_bucket = models.CharField(max_length=32, blank=True, default="")
    hit_count = models.PositiveIntegerField(default=0)
    top_score = models.FloatField(null=True, blank=True)
    latency_ms = models.PositiveIntegerField(default=0)
    citation_ids = models.JSONField(default=list, blank=True)
    low_confidence = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]


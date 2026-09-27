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
    )
    title = models.CharField(max_length=255, blank=True, default="")
    user_role_snapshot = models.CharField(max_length=64, blank=True, default="")
    department_id_snapshot = models.IntegerField(null=True, blank=True)
    # Production columns are NOT NULL without a database default, so every insert must supply them.
    access_mode = models.CharField(
        max_length=32,
        choices=ConversationAccessMode.choices,
        default=ConversationAccessMode.AUTHENTICATED,
    )
    anonymous_session_key = models.CharField(max_length=64, blank=True, default="", db_index=True)
    is_archived = models.BooleanField(default=False)
    # Structured workflow state (booking / cancellation step, candidates, pending choice). Kept apart from
    # chat history; nullable so inserts from code that predates the column keep working.
    state = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["user", "-updated_at"]),
            models.Index(fields=["anonymous_session_key", "-updated_at"], name="research_co_anonymo_idx"),
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
    reason = models.CharField(max_length=32, blank=True, default="", db_default="")
    intent = models.CharField(max_length=64, blank=True, default="", db_default="")
    knowledge_article = models.ForeignKey(
        "CopilotKnowledgeArticle",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="feedback",
    )
    created_at = models.DateTimeField(auto_now_add=True)


class FeedbackReason(models.TextChoices):
    INCORRECT = "incorrect", _("Incorrect")
    NOT_USEFUL = "not_useful", _("Not useful")
    MISSING_INFORMATION = "missing_information", _("Missing information")
    ACTION_FAILED = "action_failed", _("Could not complete action")
    OTHER = "other", _("Other")


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
    status = models.CharField(max_length=16, blank=True, default="open", db_default="open", db_index=True)
    intent = models.CharField(max_length=64, blank=True, default="", db_default="")
    resolved_article = models.ForeignKey(
        "CopilotKnowledgeArticle",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_gaps",
    )
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="research_copilot_resolved_gaps",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class KnowledgeGapStatus(models.TextChoices):
    OPEN = "open", _("Open")
    ANSWERED = "answered", _("Answered by article")
    DISMISSED = "dismissed", _("Dismissed")


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
    # Uploaded source file (equipment manual PDFs). Object key in the private S3 bucket; never a public URL.
    source_file_key = models.CharField(max_length=512, blank=True, default="")
    original_filename = models.CharField(max_length=255, blank=True, default="")
    file_sha256 = models.CharField(max_length=64, blank=True, default="", db_index=True)
    file_size = models.PositiveBigIntegerField(default=0)
    page_count = models.PositiveIntegerField(default=0)
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


class KnowledgeArticleStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    PENDING_APPROVAL = "pending_approval", _("Pending approval")
    APPROVED = "approved", _("Approved")
    INACTIVE = "inactive", _("Inactive")


class KnowledgeArticleCategory(models.TextChoices):
    BOOKING = "booking", _("Booking")
    CANCELLATION = "cancellation", _("Cancellation & reschedule")
    WALLET = "wallet", _("Wallet & payments")
    EQUIPMENT = "equipment", _("Equipment")
    ACCOUNT = "account", _("Account & profile")
    FACULTY = "faculty", _("Faculty & supervisors")
    RESULTS = "results", _("Samples & results")
    REMOTE_ANALYSIS = "remote_analysis", _("Remote Analysis")
    MY_RESEARCH = "my_research", _("My Research")
    POLICY = "policy", _("Portal policy")
    GENERAL = "general", _("General")


class KnowledgeArticleAudience(models.TextChoices):
    ALL = "all", _("All signed-in users")
    INTERNAL = "internal", _("Internal users")
    EXTERNAL = "external", _("External users")
    STUDENT = "student", _("Students")
    FACULTY = "faculty", _("Faculty")
    STAFF = "staff", _("Staff (admin, OIC, lab)")


class KnowledgeArticleSource(models.TextChoices):
    MANUAL = "manual", _("Written by admin")
    TICKET = "ticket", _("Support ticket resolution")
    GAP = "gap", _("Unanswered question")


class CopilotKnowledgeArticle(models.Model):
    """Curated Copilot answer. Only APPROVED articles are ever shown to users."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=255)
    question = models.TextField(blank=True, default="")
    answer = models.TextField()
    category = models.CharField(
        max_length=32, choices=KnowledgeArticleCategory.choices, default=KnowledgeArticleCategory.GENERAL, db_index=True
    )
    keywords = models.JSONField(default=list, blank=True)
    audience = models.CharField(
        max_length=16, choices=KnowledgeArticleAudience.choices, default=KnowledgeArticleAudience.ALL
    )
    related_equipment = models.ManyToManyField(
        "equipment.Equipment", blank=True, related_name="copilot_knowledge_articles"
    )
    related_feature = models.CharField(max_length=64, blank=True, default="")
    source = models.CharField(
        max_length=16, choices=KnowledgeArticleSource.choices, default=KnowledgeArticleSource.MANUAL
    )
    source_ticket = models.ForeignKey(
        "support.Ticket",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="copilot_knowledge_articles",
    )
    status = models.CharField(
        max_length=24, choices=KnowledgeArticleStatus.choices, default=KnowledgeArticleStatus.DRAFT, db_index=True
    )
    version = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="copilot_articles_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="copilot_articles_updated",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="copilot_articles_approved",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    usage_count = models.PositiveIntegerField(default=0)
    helpful_count = models.PositiveIntegerField(default=0)
    not_helpful_count = models.PositiveIntegerField(default=0)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [models.Index(fields=["status", "category"])]

    def __str__(self) -> str:
        return self.title


class CopilotKnowledgeArticleVersion(models.Model):
    """Immutable snapshot written on every create / edit / approval / deactivation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    article = models.ForeignKey(CopilotKnowledgeArticle, on_delete=models.CASCADE, related_name="versions")
    version = models.PositiveIntegerField()
    title = models.CharField(max_length=255)
    question = models.TextField(blank=True, default="")
    answer = models.TextField()
    category = models.CharField(max_length=32)
    keywords = models.JSONField(default=list, blank=True)
    audience = models.CharField(max_length=16)
    status = models.CharField(max_length=24)
    change = models.CharField(max_length=32, blank=True, default="")
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="copilot_article_versions",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class EscalationReason(models.TextChoices):
    NO_VERIFIED_ANSWER = "no_verified_answer", _("No verified answer")
    ACTION_FAILED = "action_failed", _("Action could not complete")
    USER_REQUESTED = "user_requested", _("User asked for support")
    NEGATIVE_FEEDBACK = "negative_feedback", _("Negative feedback")
    OTHER = "other", _("Other")


class CopilotEscalation(models.Model):
    """Links a support Ticket (existing ticket system) to the Copilot conversation that raised it."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ticket = models.ForeignKey(
        "support.Ticket",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="copilot_escalations",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="research_copilot_escalations",
    )
    conversation = models.ForeignKey(
        Conversation, on_delete=models.SET_NULL, null=True, blank=True, related_name="escalations"
    )
    message = models.ForeignKey(Message, on_delete=models.SET_NULL, null=True, blank=True, related_name="escalations")
    question = models.TextField(blank=True, default="")
    intent = models.CharField(max_length=64, blank=True, default="")
    entities = models.JSONField(default=dict, blank=True)
    equipment_id = models.IntegerField(null=True, blank=True)
    booking_id = models.IntegerField(null=True, blank=True)
    reason = models.CharField(max_length=32, choices=EscalationReason.choices, default=EscalationReason.OTHER)
    copilot_response = models.TextField(blank=True, default="")
    knowledge_article = models.ForeignKey(
        CopilotKnowledgeArticle, on_delete=models.SET_NULL, null=True, blank=True, related_name="escalations"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]


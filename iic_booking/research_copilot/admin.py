from django.contrib import admin

from iic_booking.research_copilot.models import (
    Conversation,
    CopilotAuditEvent,
    CopilotEscalation,
    CopilotKnowledgeArticle,
    CopilotKnowledgeArticleVersion,
    EmbeddingJob,
    KnowledgeDocument,
    KnowledgeGap,
    Message,
    MessageFeedback,
    SearchQueryLog,
)


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "user", "user_role_snapshot", "updated_at")
    list_filter = ("user_role_snapshot",)
    search_fields = ("title", "user__email")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("id", "conversation", "role", "confidence", "escalate_hint", "created_at")
    list_filter = ("role", "escalate_hint")


@admin.register(CopilotAuditEvent)
class CopilotAuditEventAdmin(admin.ModelAdmin):
    list_display = ("action", "user", "conversation", "message", "created_at")
    list_filter = ("action",)


@admin.register(MessageFeedback)
class MessageFeedbackAdmin(admin.ModelAdmin):
    list_display = ("rating", "user", "conversation", "created_at")


@admin.register(KnowledgeGap)
class KnowledgeGapAdmin(admin.ModelAdmin):
    list_display = ("query_summary", "reason", "user", "created_at")


@admin.register(KnowledgeDocument)
class KnowledgeDocumentAdmin(admin.ModelAdmin):
    list_display = ("title", "category", "security_level", "index_status", "chunk_count", "updated_at")
    list_filter = ("category", "security_level", "index_status", "status")
    search_fields = ("title", "content_text", "tags")


@admin.register(EmbeddingJob)
class EmbeddingJobAdmin(admin.ModelAdmin):
    list_display = ("id", "job_type", "status", "provider", "created_at", "finished_at")
    list_filter = ("status", "job_type")


@admin.register(SearchQueryLog)
class SearchQueryLogAdmin(admin.ModelAdmin):
    list_display = ("query", "intent", "hit_count", "top_score", "latency_ms", "low_confidence", "created_at")
    list_filter = ("intent", "low_confidence")
    search_fields = ("query",)


class CopilotKnowledgeArticleVersionInline(admin.TabularInline):
    model = CopilotKnowledgeArticleVersion
    extra = 0
    can_delete = False
    fields = ("version", "status", "change", "changed_by", "created_at")
    readonly_fields = fields


@admin.register(CopilotKnowledgeArticle)
class CopilotKnowledgeArticleAdmin(admin.ModelAdmin):
    # Status is read-only so approval always goes through the versioned Copilot console.
    list_display = ("title", "category", "audience", "status", "version", "usage_count", "updated_at")
    list_filter = ("status", "category", "audience", "source")
    search_fields = ("title", "question", "answer")
    readonly_fields = ("id", "status", "version", "approved_by", "approved_at", "usage_count",
                       "helpful_count", "not_helpful_count", "last_used_at", "created_at", "updated_at")
    inlines = [CopilotKnowledgeArticleVersionInline]


@admin.register(CopilotEscalation)
class CopilotEscalationAdmin(admin.ModelAdmin):
    list_display = ("ticket", "user", "intent", "reason", "created_at")
    list_filter = ("reason",)
    search_fields = ("question", "user__email")
    readonly_fields = ("id", "created_at")

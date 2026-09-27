"""URL routes for IIC Research Copilot."""

from django.urls import path

from iic_booking.research_copilot import api_views, intelligence_views, knowledge_views, public_views

app_name = "research_copilot"

urlpatterns = [
    path("bootstrap/", api_views.bootstrap, name="bootstrap"),
    path("public/bootstrap/", public_views.public_bootstrap, name="public-bootstrap"),
    path("public/ask/", public_views.public_ask, name="public-ask"),
    path("conversations/", api_views.conversations_collection, name="conversations"),
    path("conversations/<uuid:conversation_id>/", api_views.conversation_detail, name="conversation-detail"),
    path(
        "conversations/<uuid:conversation_id>/messages/",
        api_views.conversation_messages,
        name="conversation-messages",
    ),
    path(
        "conversations/<uuid:conversation_id>/messages/stream/",
        api_views.conversation_messages_stream,
        name="conversation-messages-stream",
    ),
    path(
        "conversations/<uuid:conversation_id>/feedback/",
        api_views.conversation_feedback,
        name="conversation-feedback",
    ),
    path(
        "conversations/<uuid:conversation_id>/escalate/",
        intelligence_views.conversation_escalate,
        name="conversation-escalate",
    ),
    # Copilot answers (verified knowledge articles) + admin console
    path("answers/", intelligence_views.articles_collection, name="answers"),
    path("answers/<uuid:article_id>/", intelligence_views.article_detail, name="answer-detail"),
    path("answers/<uuid:article_id>/approve/", intelligence_views.article_approve, name="answer-approve"),
    path("answers/<uuid:article_id>/deactivate/", intelligence_views.article_deactivate, name="answer-deactivate"),
    path("answers/from-ticket/<int:ticket_id>/", intelligence_views.article_from_ticket, name="answer-from-ticket"),
    path("console/unanswered/", intelligence_views.unanswered_list, name="console-unanswered"),
    path("console/unanswered/<uuid:gap_id>/resolve/", intelligence_views.unanswered_resolve, name="console-resolve"),
    path("console/escalations/", intelligence_views.escalations_list, name="console-escalations"),
    path("console/feedback/", intelligence_views.feedback_list, name="console-feedback"),
    path("console/usage/", intelligence_views.usage_stats, name="console-usage"),
    path("tools/execute/", api_views.execute_tool, name="tools-execute"),
    path("mutations/confirm/", api_views.confirm_mutation, name="mutations-confirm"),
    path("mutations/prepare/", api_views.prepare_mutation, name="mutations-prepare"),
    path("llm/health/", api_views.llm_provider_health, name="llm-provider-health"),
    # Knowledge Engine (AI.2)
    path("knowledge/search/", knowledge_views.knowledge_search, name="knowledge-search"),
    path("knowledge/documents/", knowledge_views.knowledge_documents, name="knowledge-documents"),
    path(
        "knowledge/documents/<uuid:document_id>/",
        knowledge_views.knowledge_document_detail,
        name="knowledge-document-detail",
    ),
    path(
        "knowledge/documents/<uuid:document_id>/reindex/",
        knowledge_views.knowledge_document_reindex,
        name="knowledge-document-reindex",
    ),
    path(
        "knowledge/documents/<uuid:document_id>/file/",
        knowledge_views.knowledge_document_file,
        name="knowledge-document-file",
    ),
    path("knowledge/manuals/", knowledge_views.knowledge_manuals, name="knowledge-manuals"),
    path(
        "knowledge/manuals/<uuid:document_id>/reindex/",
        knowledge_views.knowledge_manual_reindex,
        name="knowledge-manual-reindex",
    ),
    path(
        "knowledge/manuals/<uuid:document_id>/archive/",
        knowledge_views.knowledge_manual_archive,
        name="knowledge-manual-archive",
    ),
    path("knowledge/rebuild-index/", knowledge_views.knowledge_rebuild_index, name="knowledge-rebuild"),
    path("knowledge/seed/", knowledge_views.knowledge_seed, name="knowledge-seed"),
    path("knowledge/jobs/", knowledge_views.knowledge_jobs, name="knowledge-jobs"),
    path("knowledge/analytics/", knowledge_views.knowledge_analytics, name="knowledge-analytics"),
]

"""URL routes for IIC Research Copilot."""

from django.urls import path

from iic_booking.research_copilot import api_views, knowledge_views, public_views

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
    path("tools/execute/", api_views.execute_tool, name="tools-execute"),
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
    path("knowledge/rebuild-index/", knowledge_views.knowledge_rebuild_index, name="knowledge-rebuild"),
    path("knowledge/seed/", knowledge_views.knowledge_seed, name="knowledge-seed"),
    path("knowledge/jobs/", knowledge_views.knowledge_jobs, name="knowledge-jobs"),
    path("knowledge/analytics/", knowledge_views.knowledge_analytics, name="knowledge-analytics"),
]

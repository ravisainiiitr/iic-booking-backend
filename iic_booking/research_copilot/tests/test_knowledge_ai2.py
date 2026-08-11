"""Phase AI.2 — Knowledge Engine hybrid RAG tests."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from iic_booking.research_copilot.models import (
    DocumentCategory,
    IndexStatus,
    KnowledgeDocument,
    SecurityLevel,
)
from iic_booking.research_copilot.services.ingestion import upsert_document
from iic_booking.research_copilot.services.knowledge_permissions import allowed_security_levels
from iic_booking.research_copilot.services import rag as rag_svc
from iic_booking.research_copilot.services.seed_knowledge import seed_baseline_knowledge
from iic_booking.users.models.user_type import UserType

User = get_user_model()


@pytest.fixture
def student(db):
    # Token auth requires is_active=True (project create_user defaults inactive).
    return User.objects.create_user(
        email="ai2-student@example.com",
        password="test-pass-12345",
        user_type=UserType.STUDENT,
        name="AI2 Student",
        is_active=True,
        email_verified=True,
        admin_approved=True,
    )


@pytest.fixture
def admin_user(db):
    return User.objects.create_user(
        email="ai2-admin@example.com",
        password="test-pass-12345",
        user_type=UserType.ADMIN,
        name="AI2 Admin",
        is_staff=True,
        is_active=True,
        email_verified=True,
        admin_approved=True,
    )


@pytest.fixture
def student_client(student):
    Token.objects.get_or_create(user=student)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.get(user=student).key}")
    return client


@pytest.fixture
def admin_client(admin_user):
    Token.objects.get_or_create(user=admin_user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.get(user=admin_user).key}")
    return client


@pytest.mark.django_db
def test_permission_levels_students_exclude_admin_docs():
    student_levels = allowed_security_levels("student")
    assert SecurityLevel.AUTHENTICATED in student_levels
    assert SecurityLevel.ADMIN not in student_levels
    assert SecurityLevel.OPERATOR not in student_levels
    admin_levels = allowed_security_levels("admin")
    assert SecurityLevel.ADMIN in admin_levels


@pytest.mark.django_db
@override_settings(RESEARCH_COPILOT_EMBEDDING_PROVIDER="local")
def test_seed_and_incremental_index():
    result = seed_baseline_knowledge(force=True)
    assert result["created"] >= 1
    docs = KnowledgeDocument.objects.filter(status="active")
    assert docs.exists()
    assert docs.filter(index_status=IndexStatus.INDEXED).exists()
    # Incremental: skip without force
    again = seed_baseline_knowledge(force=False)
    assert again["skipped"] >= 1


@pytest.mark.django_db
@override_settings(RESEARCH_COPILOT_EMBEDDING_PROVIDER="local")
def test_permission_filtering_in_retrieve(student, admin_user):
    upsert_document(
        title="Public Booking FAQ",
        content_text="Students book equipment from the Equipments page using available slots.",
        category=DocumentCategory.FAQ,
        security_level=SecurityLevel.AUTHENTICATED,
        tags=["booking"],
        index_now=True,
    )
    upsert_document(
        title="Admin Internal Runbook Secret",
        content_text="Internal deployment secrets and support runbook steps for admins only.",
        category=DocumentCategory.DEPLOYMENT,
        security_level=SecurityLevel.ADMIN,
        tags=["runbook", "admin"],
        index_now=True,
    )

    student_hits = rag_svc.retrieve(query="admin runbook deployment secrets", role_bucket="student", user=student)
    titles = {c.title for c in student_hits.citations}
    assert "Admin Internal Runbook Secret" not in titles

    admin_hits = rag_svc.retrieve(query="admin runbook deployment secrets", role_bucket="admin", user=admin_user)
    admin_titles = {c.title for c in admin_hits.citations}
    assert "Admin Internal Runbook Secret" in admin_titles


@pytest.mark.django_db
@override_settings(RESEARCH_COPILOT_EMBEDDING_PROVIDER="local")
def test_equipment_and_policy_search():
    upsert_document(
        title="FESEM User Guide",
        content_text="FESEM provides surface morphology and EDS elemental mapping. Typical duration one hour. Sample must be conductive.",
        category=DocumentCategory.EQUIPMENT,
        security_level=SecurityLevel.AUTHENTICATED,
        tags=["fesem", "eds"],
        index_now=True,
    )
    upsert_document(
        title="Cancellation Policy",
        content_text="Cancellation follows institute booking policy. Wallet adjustments may apply after SAMPLE_ACCEPTED.",
        category=DocumentCategory.POLICY,
        security_level=SecurityLevel.AUTHENTICATED,
        tags=["cancellation", "policy"],
        index_now=True,
    )

    eq = rag_svc.retrieve(query="FESEM elemental mapping sample", role_bucket="student")
    assert any("FESEM" in c.title for c in eq.citations)

    pol = rag_svc.retrieve(query="cancellation wallet policy", role_bucket="student")
    assert any("Cancellation" in c.title for c in pol.citations)
    assert all(c.title and c.snippet for c in pol.citations)  # citations never empty fabrications


@pytest.mark.django_db
@override_settings(RESEARCH_COPILOT_EMBEDDING_PROVIDER="local", RESEARCH_COPILOT_ENABLED=True)
def test_knowledge_admin_apis(admin_client, student_client):
    seed = admin_client.post("/api/v1/research-copilot/knowledge/seed/", {"force": True}, format="json")
    assert seed.status_code == 200

    docs = admin_client.get("/api/v1/research-copilot/knowledge/documents/")
    assert docs.status_code == 200
    assert docs.json()["count"] >= 1

    analytics = admin_client.get("/api/v1/research-copilot/knowledge/analytics/")
    assert analytics.status_code == 200
    assert "documents" in analytics.json()

    # Students cannot manage knowledge center
    denied = student_client.get("/api/v1/research-copilot/knowledge/documents/")
    assert denied.status_code == 403

    search = student_client.post(
        "/api/v1/research-copilot/knowledge/search/",
        {"query": "how do I book equipment"},
        format="json",
    )
    assert search.status_code == 200
    body = search.json()
    assert "citations" in body
    assert body["latency_ms"] >= 0


@pytest.mark.django_db
@override_settings(RESEARCH_COPILOT_ENABLED=True, OPENAI_API_KEY="", COPILOT_LLM_PROVIDER="fallback", RESEARCH_COPILOT_EMBEDDING_PROVIDER="local")
def test_conversation_includes_citations(student_client):
    seed_baseline_knowledge(force=True)
    created = student_client.post("/api/v1/research-copilot/conversations/", {}, format="json")
    assert created.status_code == 201
    conv_id = created.json()["conversation"]["id"]
    msg = student_client.post(
        f"/api/v1/research-copilot/conversations/{conv_id}/messages/",
        {"content": "How do I book equipment on the portal?"},
        format="json",
    )
    assert msg.status_code == 200
    body = msg.json()["message"]
    assert body["content"]
    # Sources footer and/or structured citations
    assert "Sources" in body["content"] or (body.get("citations") or [])

"""Phase AI.1 — Research Copilot conversation framework tests."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from iic_booking.research_copilot.models import Conversation, CopilotAuditEvent, Message
from iic_booking.research_copilot.services.context_builder import build_context
from iic_booking.users.models.user_type import UserType

User = get_user_model()


@pytest.fixture
def student(db):
    # TokenAuthenticationWithInactivity rejects inactive users (401).
    # create_user defaults is_active=False until email/admin approval in this project.
    return User.objects.create_user(
        email="copilot-student@example.com",
        password="test-pass-12345",
        user_type=UserType.STUDENT,
        name="Copilot Student",
        is_active=True,
        email_verified=True,
        admin_approved=True,
    )


@pytest.fixture
def auth_client(student):
    Token.objects.get_or_create(user=student)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.get(user=student).key}")
    return client


@pytest.mark.django_db
def test_context_builder_role_bucket(student):
    ctx = build_context(student)
    assert ctx.role_bucket in {"student", "default"}
    assert "booking_guidance" in ctx.capabilities


@pytest.mark.django_db
@override_settings(RESEARCH_COPILOT_ENABLED=False)
def test_disabled_gate(auth_client):
    resp = auth_client.post("/api/v1/research-copilot/conversations/", {}, format="json")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "research_copilot_disabled"


@pytest.mark.django_db
@override_settings(RESEARCH_COPILOT_ENABLED=True, OPENAI_API_KEY="")
def test_create_conversation_and_message(auth_client, student):
    created = auth_client.post("/api/v1/research-copilot/conversations/", {"title": ""}, format="json")
    assert created.status_code == 201
    conv_id = created.json()["conversation"]["id"]
    assert Conversation.objects.filter(id=conv_id, user=student).exists()
    assert CopilotAuditEvent.objects.filter(action="conversation_created").exists()

    msg = auth_client.post(
        f"/api/v1/research-copilot/conversations/{conv_id}/messages/",
        {"content": "How do I book FESEM?"},
        format="json",
    )
    assert msg.status_code == 200
    body = msg.json()
    assert body["message"]["role"] == "assistant"
    assert body["message"]["content"]
    assert Message.objects.filter(conversation_id=conv_id).count() == 2
    # Without an LLM key, retrieval confidence may be low and audit uses escalate_hint.
    assert CopilotAuditEvent.objects.filter(
        action__in=["message_replied", "escalate_hint"]
    ).exists()

    detail = auth_client.get(f"/api/v1/research-copilot/conversations/{conv_id}/")
    assert detail.status_code == 200
    assert len(detail.json()["messages"]) == 2


@pytest.mark.django_db
@override_settings(RESEARCH_COPILOT_ENABLED=True, OPENAI_API_KEY="")
def test_guest_denied():
    client = APIClient()
    resp = client.post("/api/v1/research-copilot/conversations/", {}, format="json")
    assert resp.status_code in {401, 403}


@pytest.mark.django_db
@override_settings(RESEARCH_COPILOT_ENABLED=True, OPENAI_API_KEY="")
def test_escalate_hint_on_human_request(auth_client):
    created = auth_client.post("/api/v1/research-copilot/conversations/", {}, format="json")
    conv_id = created.json()["conversation"]["id"]
    msg = auth_client.post(
        f"/api/v1/research-copilot/conversations/{conv_id}/messages/",
        {"content": "I need to talk to a human / create a support ticket"},
        format="json",
    )
    assert msg.status_code == 200
    assert msg.json()["message"]["escalate_hint"] is True


@pytest.mark.django_db
@override_settings(RESEARCH_COPILOT_ENABLED=True, OPENAI_API_KEY="")
def test_feedback(auth_client):
    created = auth_client.post("/api/v1/research-copilot/conversations/", {}, format="json")
    conv_id = created.json()["conversation"]["id"]
    auth_client.post(
        f"/api/v1/research-copilot/conversations/{conv_id}/messages/",
        {"content": "Wallet help"},
        format="json",
    )
    fb = auth_client.post(
        f"/api/v1/research-copilot/conversations/{conv_id}/feedback/",
        {"rating": "up"},
        format="json",
    )
    assert fb.status_code == 201

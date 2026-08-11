"""AI.17 — LLM provider selection + Ollama gateway tests."""

from __future__ import annotations

import json
import urllib.error
from io import BytesIO
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APIClient

from iic_booking.research_copilot.services.llm_gateway import (
    FallbackGateway,
    OllamaGateway,
    OpenAIGateway,
    get_gateway,
    provider_health,
)
from iic_booking.users.models.user_type import UserType

User = get_user_model()


@pytest.fixture
def admin_user(db):
    return User.objects.create_user(
        email="admin-ai17@example.com",
        password="test-pass-12345",
        name="Admin AI17",
        user_type=UserType.ADMIN,
        is_superuser=True,
        is_staff=True,
        is_active=True,
        email_verified=True,
        admin_approved=True,
    )


@pytest.fixture
def plain_user(db):
    return User.objects.create_user(
        email="user-ai17@example.com",
        password="test-pass-12345",
        name="User AI17",
        user_type=UserType.STUDENT,
        is_active=True,
        email_verified=True,
        admin_approved=True,
    )


@override_settings(
    COPILOT_PROVIDER="ollama",
    COPILOT_LLM_PROVIDER="ollama",
    OPENAI_API_KEY="",
    OLLAMA_BASE_URL="http://ollama.test:11434",
    OLLAMA_MODEL="llama3.2:3b",
)
def test_provider_selection_ollama_without_openai_key():
    gw = get_gateway()
    assert isinstance(gw, OllamaGateway)
    assert gw.model == "llama3.2:3b"


@override_settings(COPILOT_PROVIDER="openai", COPILOT_LLM_PROVIDER="openai", OPENAI_API_KEY="sk-test", RESEARCH_COPILOT_MODEL="gpt-4o-mini")
def test_provider_selection_openai():
    gw = get_gateway()
    assert isinstance(gw, OpenAIGateway)
    assert gw.model == "gpt-4o-mini"


@override_settings(COPILOT_PROVIDER="openai", COPILOT_LLM_PROVIDER="openai", OPENAI_API_KEY="")
def test_openai_provider_missing_key_uses_fallback():
    gw = get_gateway()
    assert isinstance(gw, FallbackGateway)


@override_settings(COPILOT_PROVIDER="fallback", COPILOT_LLM_PROVIDER="fallback", OPENAI_API_KEY="sk-ignored")
def test_provider_selection_fallback():
    assert isinstance(get_gateway(), FallbackGateway)


@override_settings(
    COPILOT_PROVIDER="ollama",
    COPILOT_LLM_PROVIDER="ollama",
    OPENAI_API_KEY="",
    OLLAMA_BASE_URL="http://127.0.0.1:9",
    OLLAMA_MODEL="x",
)
def test_ollama_works_config_without_openai_key():
    """Absence of OPENAI_API_KEY must not block Ollama gateway construction."""
    gw = get_gateway()
    assert isinstance(gw, OllamaGateway)


def test_ollama_complete_success():
    gw = OllamaGateway(base_url="http://ollama.test:11434", model="llama3.2:3b", timeout_seconds=5)
    payload = {
        "choices": [{"message": {"content": "Hello from Ollama"}, "finish_reason": "stop"}],
        "model": "llama3.2:3b",
        "usage": {"prompt_tokens": 10, "completion_tokens": 4},
    }

    class _Resp:
        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with patch("urllib.request.urlopen", return_value=_Resp()):
        result = gw.complete([{"role": "user", "content": "Hi"}], max_tokens=64)
    assert result is not None
    assert result.text == "Hello from Ollama"
    assert result.provider == "ollama"
    assert result.prompt_tokens == 10
    assert result.error_category == ""


def test_ollama_unavailable_network():
    gw = OllamaGateway(base_url="http://ollama.test:11434", model="llama3.2:3b", timeout_seconds=1)
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
        result = gw.complete([{"role": "user", "content": "Hi"}])
    assert result is not None
    assert result.text == ""
    assert result.error_category == "network"


def test_ollama_timeout_category():
    gw = OllamaGateway(base_url="http://ollama.test:11434", model="m", timeout_seconds=1)

    with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
        result = gw.complete([{"role": "user", "content": "Hi"}])
    assert result is not None
    assert result.error_category == "timeout"


def test_ollama_invalid_model_http():
    gw = OllamaGateway(base_url="http://ollama.test:11434", model="missing-model", timeout_seconds=5)

    def _raise(*a, **k):
        raise urllib.error.HTTPError(
            url="http://ollama.test:11434/v1/chat/completions",
            code=404,
            msg="not found",
            hdrs=None,
            fp=BytesIO(b'{"error":"model not found"}'),
        )

    with patch("urllib.request.urlopen", side_effect=_raise):
        result = gw.complete([{"role": "user", "content": "Hi"}])
    assert result is not None
    assert result.text == ""
    assert result.error_category == "invalid_model_or_path"


def test_ollama_health_available():
    gw = OllamaGateway(base_url="http://ollama.test:11434", model="llama3.2:3b")
    tags = {"models": [{"name": "llama3.2:3b"}]}

    class _Resp:
        def read(self):
            return json.dumps(tags).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with patch("urllib.request.urlopen", return_value=_Resp()):
        h = gw.health()
    assert h.status == "available"
    assert h.provider == "ollama"
    public = h.as_public_dict()
    assert "base_url" not in public


@override_settings(
    COPILOT_PROVIDER="ollama",
    COPILOT_LLM_PROVIDER="ollama",
    OLLAMA_BASE_URL="http://ollama.test:11434",
    OLLAMA_MODEL="llama3.2:3b",
)
def test_provider_health_unavailable():
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
        h = provider_health()
    assert h.provider == "ollama"
    assert h.status == "unavailable"


@override_settings(RESEARCH_COPILOT_ENABLED=True, COPILOT_PROVIDER="fallback", COPILOT_LLM_PROVIDER="fallback")
def test_llm_health_endpoint_admin_only(admin_user, plain_user):
    client = APIClient()
    client.force_authenticate(user=plain_user)
    denied = client.get("/api/v1/research-copilot/llm/health/")
    assert denied.status_code == 403

    client.force_authenticate(user=admin_user)
    ok = client.get("/api/v1/research-copilot/llm/health/")
    assert ok.status_code == 200
    body = ok.json()
    assert body["provider"] in {"fallback", "ollama", "openai", "fake"}
    assert "concurrency" in body
    assert "openai_api_key_configured" in body
    assert "sk-" not in json.dumps(body)


@override_settings(
    RESEARCH_COPILOT_ENABLED=True,
    COPILOT_PROVIDER="ollama",
    COPILOT_LLM_PROVIDER="ollama",
    OPENAI_API_KEY="",
    OLLAMA_BASE_URL="http://ollama.test:11434",
    OLLAMA_MODEL="llama3.2:3b",
)
def test_send_message_graceful_when_ollama_down(plain_user):
    from iic_booking.research_copilot.services import conversation as conv_svc

    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
        conv = conv_svc.create_conversation(user=plain_user, title="t")
        payload = conv_svc.send_message(user=plain_user, conversation=conv, content="Hello")
    message = payload["message"]
    content = message["content"].lower()
    assert "temporarily unavailable" in content or "could not generate" in content
    assert message["metadata"].get("provider") == "ollama"
    assert message["metadata"].get("llm_error_category")


@override_settings(COPILOT_PROVIDER="fake", COPILOT_LLM_PROVIDER="ollama", OPENAI_API_KEY="")
def test_copilot_provider_alias_prefers_copilot_provider():
    from iic_booking.research_copilot.services.llm_gateway import FakeInferenceProvider, configured_provider_name

    assert configured_provider_name() == "fake"
    gw = get_gateway()
    assert isinstance(gw, FakeInferenceProvider)
    result = gw.generate([{"role": "user", "content": "ping"}])
    assert result is not None
    assert result.provider == "fake"
    assert "ping" in result.text


@override_settings(COPILOT_PROVIDER="fake", RESEARCH_COPILOT_ENABLED=True, RESEARCH_COPILOT_MAX_CONCURRENT=1)
def test_concurrency_busy_does_not_raise(plain_user):
    """When saturated, Copilot returns a busy message — booking path untouched."""
    import threading
    import time

    from iic_booking.research_copilot.services import conversation as conv_svc
    from iic_booking.research_copilot.services.inference_concurrency import acquire_generation_slot
    from iic_booking.research_copilot.services.llm_gateway import FakeInferenceProvider

    held = threading.Event()
    release = threading.Event()

    def _hold():
        with acquire_generation_slot(wait=False):
            held.set()
            release.wait(timeout=5)

    t = threading.Thread(target=_hold, daemon=True)
    t.start()
    assert held.wait(timeout=2)

    with patch(
        "iic_booking.research_copilot.services.conversation.get_gateway",
        return_value=FakeInferenceProvider(reply="should-not-run"),
    ):
        conv = conv_svc.create_conversation(user=plain_user, title="busy")
        payload = conv_svc.send_message(user=plain_user, conversation=conv, content="Are you busy?")
    release.set()
    t.join(timeout=2)

    content = payload["message"]["content"]
    assert "temporarily busy" in content.lower()
    assert payload["message"]["metadata"].get("busy") is True

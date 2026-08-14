"""AI.14 — Functional Copilot tools, grounding, and pilot allowlist tests."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from iic_booking.research_copilot.services import tools as tools_svc
from iic_booking.research_copilot.services.portal_grounding import plan_tool_calls, run_portal_grounding
from iic_booking.research_copilot.services.prompt_builder import append_portal_context, build_system_prompt
from iic_booking.research_copilot.services.context_builder import build_context
from iic_booking.users.models.user_type import UserType

User = get_user_model()


def _user(email="copilot-ai14@example.com"):
    return User.objects.create_user(
        email=email,
        password="test-pass-12345",
        user_type=UserType.STUDENT,
        name="AI14 User",
        is_active=True,
        email_verified=True,
        admin_approved=True,
    )


@pytest.mark.django_db
def test_plan_tool_calls_for_bookings_and_wallet():
    plans = plan_tool_calls(text="What bookings do I have and what is my wallet balance?")
    names = {n for n, _ in plans}
    assert "search_bookings" in names or "get_next_booking" in names
    assert "get_wallet" in names


@pytest.mark.django_db
def test_plan_tool_calls_sample_status_natural_phrasing():
    plans = plan_tool_calls(text="What is the status of my sample?")
    names = {n for n, _ in plans}
    assert "get_sample_status" in names


@pytest.mark.django_db
def test_portal_grounding_injects_portal_block(django_user_model):
    user = _user()
    out = run_portal_grounding(user=user, text="What bookings do I have?")
    assert "<<<PORTAL_DATA>>>" in out["block"]
    assert "PORTAL_DATA" in out["modes"]
    assert out["tool_results"]


@pytest.mark.django_db
def test_portal_grounding_chains_pricing_after_equipment_search(monkeypatch, django_user_model):
    """AI.20: 'How much does 5 XRD samples cost?' must not skip portal pricing tools."""
    user = _user("price-ai20@example.com")
    calls: list[tuple[str, dict]] = []

    def fake_execute(*, name, arguments, user):
        calls.append((name, dict(arguments or {})))
        if name == "search_equipment":
            return {
                "ok": True,
                "data": [{"id": 42, "name": "XRD Lab"}],
                "actions": [],
            }
        if name == "estimate_booking_cost":
            return {
                "ok": True,
                "data": {
                    "equipment_id": 42,
                    "estimate": None,
                    "note": "portal calculate",
                    "source": "PORTAL_DATA",
                },
                "actions": [],
            }
        return {"ok": True, "data": {}, "actions": []}

    monkeypatch.setattr(tools_svc, "execute_tool", fake_execute)
    out = run_portal_grounding(user=user, text="How much does 5 XRD samples cost?")
    names = [n for n, _ in calls]
    assert "search_equipment" in names
    assert "estimate_booking_cost" in names
    assert any(args.get("equipment_id") == 42 for n, args in calls if n == "estimate_booking_cost")
    assert "estimate_booking_cost" in {t.get("tool") for t in out["tool_results"]}


@pytest.mark.django_db
def test_prompt_has_response_modes(django_user_model):
    user = _user("modes@example.com")
    ctx = build_context(user)
    system = build_system_prompt(ctx)
    assert "Based on your portal data" in system or "portal data" in system.lower()
    wrapped = append_portal_context(system, portal_block="<<<PORTAL_DATA>>>\n{}\n<<<END_PORTAL_DATA>>>")
    assert "<<<PORTAL_DATA>>>" in wrapped


@pytest.mark.django_db
def test_wallet_foreign_selector_still_denied():
    user = _user("wallet14@example.com")
    result = tools_svc.execute_tool(name="get_wallet", arguments={"email": "other@example.com"}, user=user)
    assert result["ok"] is False
    assert result["error"] == "forbidden"


@pytest.mark.django_db
def test_sample_status_own_scope_only():
    user = _user("sample14@example.com")
    denied = tools_svc.execute_tool(name="get_sample_status", arguments={"booking_id": 999999}, user=user)
    assert denied["ok"] is False
    assert denied["error"] in {"booking_not_found", "forbidden"}


@pytest.mark.django_db
def test_results_own_scope_only():
    user = _user("results14@example.com")
    denied = tools_svc.execute_tool(name="get_booking_results", arguments={"booking_id": 999999}, user=user)
    assert denied["ok"] is False


@pytest.mark.django_db
def test_create_booking_still_requires_confirmation():
    user = _user("book14@example.com")
    result = tools_svc.execute_tool(name="create_booking", arguments={"equipment_id": 1}, user=user)
    assert result["ok"] is True
    assert result["data"]["requires_confirmation"] is True


@pytest.mark.django_db
@override_settings(RESEARCH_COPILOT_ENABLED=True, RESEARCH_COPILOT_PILOT_EMAILS="pilot@example.com", OPENAI_API_KEY="", COPILOT_LLM_PROVIDER="fallback")
def test_pilot_allowlist_blocks_non_pilot():
    user = _user("outsider@example.com")
    Token.objects.get_or_create(user=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.get(user=user).key}")
    resp = client.get("/api/v1/research-copilot/bootstrap/")
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


@pytest.mark.django_db
@override_settings(RESEARCH_COPILOT_ENABLED=True, RESEARCH_COPILOT_PILOT_EMAILS="pilot@example.com", OPENAI_API_KEY="", COPILOT_LLM_PROVIDER="fallback")
def test_pilot_allowlist_allows_pilot():
    user = _user("pilot@example.com")
    Token.objects.get_or_create(user=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.get(user=user).key}")
    resp = client.get("/api/v1/research-copilot/bootstrap/")
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True
    assert resp.json().get("command_actions")


@pytest.mark.django_db
@override_settings(RESEARCH_COPILOT_ENABLED=True, OPENAI_API_KEY="", COPILOT_LLM_PROVIDER="fallback")
def test_message_runs_portal_grounding_metadata():
    user = _user("ground@example.com")
    Token.objects.get_or_create(user=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.get(user=user).key}")
    created = client.post("/api/v1/research-copilot/conversations/", {}, format="json")
    conv_id = created.json()["conversation"]["id"]
    msg = client.post(
        f"/api/v1/research-copilot/conversations/{conv_id}/messages/",
        {"content": "What bookings do I have?"},
        format="json",
    )
    assert msg.status_code == 200
    # Assistant reply exists; grounding should have attempted search_bookings
    body = msg.json()["message"]
    assert body["role"] == "assistant"
    assert body["content"]

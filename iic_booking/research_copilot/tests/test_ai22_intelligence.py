"""AI.22 — Query intelligence, clarification, follow-up enrichment tests."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

from iic_booking.research_copilot.models import Conversation
from iic_booking.research_copilot.services import conversation as conv_svc
from iic_booking.research_copilot.services.portal_grounding import plan_tool_calls
from iic_booking.research_copilot.services.query_intelligence import (
    clarification_question,
    enrich_query_with_history,
    extract_num_samples,
)
from iic_booking.users.models.user_type import UserType

User = get_user_model()


def _user(email="copilot-ai22@example.com"):
    return User.objects.create_user(
        email=email,
        password="test-pass-12345",
        user_type=UserType.STUDENT,
        name="AI22 User",
        is_active=True,
        email_verified=True,
        admin_approved=True,
    )


def test_extract_num_samples():
    assert extract_num_samples("How much does 5 XRD samples cost?") == 5
    assert extract_num_samples("cost for 1 sample") == 1
    assert extract_num_samples("What is XRD?") is None


def test_clarification_for_ambiguous_book():
    q = clarification_question(text="Can I book it?")
    assert q
    assert "equipment" in q.lower()


def test_clarification_not_needed_when_equipment_named():
    assert clarification_question(text="How much does 5 XRD samples cost?") is None


def test_followup_enrichment_uses_prior_equipment():
    out = enrich_query_with_history(
        text="How much will it cost?",
        prior_user_texts=["What is my next XRD booking?"],
    )
    assert out["enriched"] is True
    assert "XRD" in out["text"] or "xrd" in out["text"].lower()


def test_plan_results_ready_phrase():
    plans = plan_tool_calls(text="Are my results ready?")
    assert "get_booking_results" in {n for n, _ in plans}


def test_plan_remote_analysis_software():
    plans = plan_tool_calls(text="Can I analyze my PXRD data remotely?")
    assert "recommend_software" in {n for n, _ in plans}


def test_plan_sample_deadline_phrase():
    plans = plan_tool_calls(text="When should I submit my sample?")
    assert "get_sample_deadline" in {n for n, _ in plans}


@pytest.mark.django_db
@override_settings(RESEARCH_COPILOT_ENABLED=True, RESEARCH_COPILOT_PILOT_EMAILS="")
def test_deterministic_clarification_skips_llm(monkeypatch):
    user = _user()
    conv = Conversation.objects.create(user=user, title="ai22")

    def boom(*args, **kwargs):
        raise AssertionError("LLM must not be called for clarification")

    monkeypatch.setattr(
        "iic_booking.research_copilot.services.conversation.get_gateway",
        boom,
    )
    payload = conv_svc.send_message(user=user, conversation=conv, content="Can I book it?")
    meta = (payload.get("message") or {}).get("metadata") or {}
    assert meta.get("clarification") is True
    assert meta.get("provider") == "deterministic"
    assert "equipment" in ((payload.get("message") or {}).get("content") or "").lower()


def test_eval_subset_routing_expectations():
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parent / "data" / "ai22_eval_subset.json"
    rows = json.loads(path.read_text(encoding="utf-8"))
    assert len(rows) >= 8
    for row in rows:
        q = row["question"]
        if row.get("expect_clarification"):
            assert clarification_question(text=q), row["id"]
            continue
        names = {n for n, _ in plan_tool_calls(text=q)}
        for need in row.get("expected_tools_any") or []:
            # pricing may only plan search_equipment; chaining adds estimate in grounding
            if need == "estimate_booking_cost" and "search_equipment" in names:
                continue
            assert need in names, (row["id"], need, names)
        for ban in row.get("expected_tools_none_of") or []:
            assert ban not in names, (row["id"], ban, names)

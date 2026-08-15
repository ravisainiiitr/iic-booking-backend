"""AI.25.2 — deterministic portal replies skip Ollama for authoritative lookups."""

from __future__ import annotations

from iic_booking.research_copilot.services.portal_grounding import (
    _format_authoritative_portal_reply,
    _format_cost_only_reply,
    _format_next_booking_reply,
    _looks_explanatory,
)


def test_looks_explanatory_detects_definitions():
    assert _looks_explanatory("what is xrd?")
    assert _looks_explanatory("difference between xrd and sem?")
    assert not _looks_explanatory("how much does 5 pxrd samples cost?")
    assert not _looks_explanatory("what is my next booking?")
    assert not _looks_explanatory("what is the status of my sample?")


def test_next_booking_empty():
    text = _format_next_booking_reply(None)
    assert "no upcoming" in text.lower()
    assert "PORTAL DATA" in text


def test_cost_only_uses_inr_and_portal_engine():
    structured = {
        "equipment_name": "PXRD",
        "estimate": {"amount": 200, "currency": "INR", "charge_profile": "SAMPLE", "num_samples": 5},
        "pricing_resolution": {"billing_identity_is_pi": False, "resolved_pricing_profile": "STANDARD"},
    }
    text = _format_cost_only_reply(structured)
    assert "INR" in text
    assert "200" in text
    assert "charge engine" in text.lower()


def test_authoritative_status_path():
    reply = _format_authoritative_portal_reply(
        lower="what is my next booking?",
        tool_names={"get_next_booking"},
        structured={"next_booking": {"booking_id": 12, "equipment": "PXRD", "status": "CONFIRMED", "date": "2026-08-20"}},
        wants_cost=False,
        wants_slots=False,
        wants_prepare_docs=False,
    )
    assert reply
    assert "12" in reply
    assert "PXRD" in reply


def test_authoritative_skips_pure_definition():
    reply = _format_authoritative_portal_reply(
        lower="what is xrd?",
        tool_names=set(),
        structured={},
        wants_cost=False,
        wants_slots=False,
        wants_prepare_docs=False,
    )
    assert reply is None

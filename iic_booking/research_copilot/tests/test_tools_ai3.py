"""Unit tests for Research Copilot tool registry (AI.3)."""

from __future__ import annotations

import pytest

from iic_booking.research_copilot.services import tools as tools_svc


@pytest.mark.django_db
def test_list_tools_marks_read_only_available(django_user_model):
    rows = tools_svc.list_tools_for_role("student")
    names = {r["name"]: r for r in rows}
    assert names["search_equipment"]["available"] is True
    assert names["recommend_software"]["available"] is True
    assert names["search_documentation"]["available"] is True


@pytest.mark.django_db
def test_execute_search_equipment_empty_query(django_user_model):
    user = django_user_model.objects.create_user(email="copilot-tools@example.com", password="x")
    result = tools_svc.execute_tool(name="search_equipment", arguments={"query": "a"}, user=user)
    assert result["ok"] is True
    assert result["data"] == []


@pytest.mark.django_db
def test_execute_unknown_tool(django_user_model):
    user = django_user_model.objects.create_user(email="copilot-tools2@example.com", password="x")
    result = tools_svc.execute_tool(name="not_a_tool", arguments={}, user=user)
    assert result["ok"] is False
    assert result["error"] == "unknown_tool"


@pytest.mark.django_db
def test_enrich_actions_book_intent(django_user_model):
    user = django_user_model.objects.create_user(email="copilot-tools3@example.com", password="x")
    actions = tools_svc.enrich_actions_from_message(
        user=user,
        text="I need to book SEM tomorrow",
        base_actions=[],
    )
    ids = {a["id"] for a in actions}
    assert "book_equipment" in ids
    assert "open_equipments" in ids


@pytest.mark.django_db
def test_search_bookings_rejects_foreign_user_selector(django_user_model):
    user = django_user_model.objects.create_user(email="copilot-tools-auth@example.com", password="x")
    result = tools_svc.execute_tool(
        name="search_bookings",
        arguments={"email": "someone.else@example.com"},
        user=user,
    )
    assert result["ok"] is False
    assert result["error"] == "forbidden"


@pytest.mark.django_db
def test_cancel_booking_tool_scopes_to_caller(django_user_model):
    other = django_user_model.objects.create_user(email="other-tools@example.com", password="x")
    if hasattr(other, "user_type"):
        try:
            other.user_type = "student"
            other.save(update_fields=["user_type"])
        except Exception:
            pass
    # Foreign booking id must never resolve for another user.
    denied = tools_svc.execute_tool(
        name="cancel_booking",
        arguments={"booking_id": 999999},
        user=other,
    )
    assert denied["ok"] is False
    assert denied["error"] in {"booking_not_found", "forbidden"}


@pytest.mark.django_db
def test_tool_execution_writes_audit(django_user_model):
    from iic_booking.research_copilot.models import AuditAction, CopilotAuditEvent

    user = django_user_model.objects.create_user(email="copilot-tools-audit@example.com", password="x")
    before = CopilotAuditEvent.objects.count()
    tools_svc.execute_tool(name="search_equipment", arguments={"query": "x"}, user=user)
    after = CopilotAuditEvent.objects.filter(action=AuditAction.TOOL_EXECUTED, user=user).count()
    assert CopilotAuditEvent.objects.count() >= before + 1
    assert after >= 1

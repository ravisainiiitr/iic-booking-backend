"""AI.25: verify private handlers are never called for anonymous ACL rejects."""
from __future__ import annotations

import pytest
from django.test import override_settings

from iic_booking.research_copilot.services import tools as tools_svc
from iic_booking.research_copilot.services.access_control import AccessMode


SETTINGS = dict(
    RESEARCH_COPILOT_ENABLED=True,
    RESEARCH_COPILOT_PUBLIC_ENABLED=True,
    RESEARCH_COPILOT_PILOT_EMAILS="",
    COPILOT_PROVIDER="fake",
    COPILOT_LLM_PROVIDER="fake",
)


@pytest.mark.django_db
@override_settings(**SETTINGS)
def test_anonymous_private_tool_rejected_before_handler(monkeypatch):
    called = {"n": 0}

    def boom(**kwargs):
        called["n"] += 1
        raise AssertionError("private handler must not run for anonymous")

    # Patch handlers that must never run anonymously.
    for name in (
        "search_bookings",
        "get_next_booking",
        "get_wallet",
        "get_sample_status",
        "get_booking_results",
        "cancel_booking",
        "launch_remote_analysis",
    ):
        assert name in tools_svc._HANDLERS
        monkeypatch.setitem(tools_svc._HANDLERS, name, boom)
        result = tools_svc.execute_tool(name=name, arguments={}, user=None, access_mode=AccessMode.PUBLIC)
        assert result["ok"] is False
        assert result["error"] == "login_required"
        assert called["n"] == 0


@pytest.mark.django_db
@override_settings(**SETTINGS)
def test_anonymous_key_is_not_authorization():
    """Possession of X-Copilot-Anonymous-Key must not unlock private tools."""
    result = tools_svc.execute_tool(
        name="get_wallet",
        arguments={"anonymous_session_key": "anon_test_session_key01"},
        user=None,
        access_mode=AccessMode.PUBLIC,
    )
    assert result["ok"] is False
    assert result["error"] == "login_required"

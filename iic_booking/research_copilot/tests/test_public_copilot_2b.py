"""Tests for public Copilot ask + estimate tool wiring."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory

from iic_booking.research_copilot.services import conversation as conv_svc
from iic_booking.research_copilot.services.knowledge_permissions import allowed_security_levels
from iic_booking.research_copilot.services.portal_grounding import PUBLIC_TOOL_ALLOWLIST, plan_tool_calls
from iic_booking.research_copilot import public_views
from iic_booking.research_copilot.services import tools as tools_svc


class OllamaUrlDefaultTests(SimpleTestCase):
    def test_compose_default_uses_service_hostname(self):
        compose = Path(settings.BASE_DIR) / "docker-compose.production.yml"
        self.assertTrue(compose.exists(), compose)
        text = compose.read_text(encoding="utf-8")
        self.assertIn("${OLLAMA_BASE_URL:-http://ollama:11434}", text)
        self.assertNotIn("${OLLAMA_BASE_URL:-http://127.0.0.1:11434}", text)


class PublicPermissionTests(SimpleTestCase):
    def test_public_role_only_sees_public_docs(self):
        levels = allowed_security_levels("public")
        self.assertEqual(levels, {"public"})

    def test_plan_public_blocks_wallet(self):
        plans = plan_tool_calls(text="What is my wallet balance?", public=True)
        self.assertFalse(any(n == "get_wallet" for n, _ in plans))

    def test_plan_public_allows_docs(self):
        plans = plan_tool_calls(text="What does HOLD mean on a booking?", public=True)
        self.assertTrue(any(n == "search_documentation" for n, _ in plans))
        self.assertTrue(all(n in PUBLIC_TOOL_ALLOWLIST for n, _ in plans))

    def test_plan_auth_allows_wallet(self):
        plans = plan_tool_calls(text="What is my wallet balance?", public=False)
        self.assertTrue(any(n == "get_wallet" for n, _ in plans))


@override_settings(RESEARCH_COPILOT_ENABLED=True, RESEARCH_COPILOT_PILOT_EMAILS="")
class PublicAskApiTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    @patch("iic_booking.research_copilot.services.conversation.public_ask")
    def test_public_ask_endpoint(self, mock_ask):
        mock_ask.return_value = {
            "ok": True,
            "message": {
                "role": "assistant",
                "content": "HOLD means paused.",
                "citations": [],
                "suggested_actions": [],
            },
        }
        req = self.factory.post(
            "/api/v1/research-copilot/public/ask/",
            {"content": "What is HOLD?"},
            format="json",
        )
        req.user = SimpleNamespace(is_authenticated=False)
        resp = public_views.public_ask(req)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data.get("ok"))

    def test_public_bootstrap_shape(self):
        payload = conv_svc.public_bootstrap_payload()
        self.assertIn("suggested_prompts", payload)
        self.assertIn("auth_required_for", payload)
        self.assertIn("book", payload["auth_required_for"])
        self.assertTrue(payload.get("enabled"))

    @override_settings(RESEARCH_COPILOT_ENABLED=True, RESEARCH_COPILOT_PILOT_EMAILS="pilot@example.com")
    def test_public_enabled_during_pilot_allowlist(self):
        self.assertTrue(conv_svc.feature_enabled(user=None))
        self.assertFalse(
            conv_svc.feature_enabled(user=SimpleNamespace(email="other@example.com"))
        )


class EstimateToolTests(SimpleTestCase):
    def test_estimate_returns_numeric(self):
        eq = SimpleNamespace(id=1, name="FESEM", slot_duration_minutes=30, profile_type="SAMPLE")
        cp = SimpleNamespace(profile_type="SAMPLE")
        qs = MagicMock()
        qs.first.return_value = cp
        qs.exclude = MagicMock(return_value=qs)

        with patch("iic_booking.equipment.models.Equipment.objects.get", return_value=eq), patch(
            "iic_booking.equipment.models.ChargeProfile.objects.filter", return_value=qs
        ), patch(
            "iic_booking.equipment.models.DynamicInputField.objects.filter", return_value=[]
        ), patch(
            "iic_booking.equipment.print_3d_views.get_charge_estimate_guest_user",
            return_value=SimpleNamespace(is_authenticated=False, user_type="student"),
        ), patch(
            "iic_booking.equipment.calculators.TimeCalculationEngine.calculate_time",
            return_value=60,
        ), patch(
            "iic_booking.equipment.calculators.ChargeCalculationEngine.calculate_charge",
            return_value=(Decimal("150.00"), [{"description": "Primary", "amount": 150.0}]),
        ):
            result = tools_svc._estimate_booking_cost(
                arguments={"equipment_id": 1, "public": True},
                user=None,
            )
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result["data"]["estimate"], 150.0)
        self.assertTrue(any(a.get("id") == "sign_in_for_estimate" for a in (result.get("actions") or [])))

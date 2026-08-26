"""Copilot V2 Phase A acceptance tests — deterministic reads without LLM."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from iic_booking.research_copilot.services.v2.datetime_resolver import resolve_date_window
from iic_booking.research_copilot.services.v2.equipment_resolver import EquipmentCandidate, EquipmentResolution
from iic_booking.research_copilot.services.v2.intent_resolver import resolve_intent
from iic_booking.research_copilot.services.v2.mutations import mutations_enabled
from iic_booking.research_copilot.services.v2.mutations import booking as booking_mut
from iic_booking.users.models.user_type import UserType


class IntentResolverTests(SimpleTestCase):
    def test_fesem_slots_this_week(self):
        intent = resolve_intent("Search available slots for FESEM this week")
        self.assertEqual(intent.intent, "search_slots")
        self.assertTrue(intent.deterministic)

    def test_fesem_does_not_activate_bare_sem_token(self):
        from iic_booking.research_copilot.services.v2.equipment_resolver import _token_in_text

        self.assertTrue(_token_in_text("fesem", "search available slots for fesem this week"))
        self.assertFalse(_token_in_text("sem", "search available slots for fesem this week"))
        self.assertTrue(_token_in_text("sem", "search available slots for sem this week"))

    def test_wallet_balance(self):
        intent = resolve_intent("What is my wallet balance?")
        self.assertEqual(intent.intent, "wallet_balance")
        self.assertTrue(intent.needs_auth)

    def test_pending_actions(self):
        intent = resolve_intent("What are my pending actions?")
        self.assertEqual(intent.intent, "pending_actions")

    def test_general_falls_through(self):
        intent = resolve_intent("Tell me a joke about electrons")
        self.assertFalse(intent.deterministic)

    def test_this_week_window(self):
        window = resolve_date_window("Search available slots for FESEM this week")
        self.assertIn("week", window.label.lower())
        self.assertGreaterEqual((window.end_date - window.start_date).days, 0)


@override_settings(
    RESEARCH_COPILOT_ENABLED=True,
    COPILOT_V2_ENABLED=True,
    COPILOT_DETERMINISTIC_READS=True,
    COPILOT_AVAILABILITY=True,
    COPILOT_BOOKING_CREATE=False,
    COPILOT_WALLET_RECHARGE=False,
    COPILOT_BOOKING_E2E_TEST_MODE=False,
)
class DeterministicWithoutLlmTests(SimpleTestCase):
    @patch("iic_booking.research_copilot.services.v2.read_tools.search_available_slots")
    def test_orchestrator_skips_llm_for_slots(self, mock_slots):
        from iic_booking.research_copilot.services.v2.orchestrator import try_deterministic_turn

        mock_slots.return_value = {
            "response_kind": "LIVE_DATA",
            "content": "**FESEM — Available slots**",
            "cards": [{"type": "slots", "items": [{"date": "2026-03-26", "start": "2026-03-26T10:00:00+05:30"}]}],
            "suggested_actions": [],
            "escalate_hint": False,
            "confidence": 0.9,
            "metadata": {"equipment_id": 7, "equipment_name": "FESEM", "llm_used": False},
        }
        result = try_deterministic_turn(
            user=SimpleNamespace(is_authenticated=True, pk=1),
            text="Search available slots for FESEM this week",
            conversation=None,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["response_kind"], "LIVE_DATA")
        self.assertFalse(result["metadata"]["llm_used"])
        mock_slots.assert_called_once()

    @patch("iic_booking.research_copilot.services.conversation.get_gateway")
    @patch("iic_booking.research_copilot.services.v2.orchestrator.try_deterministic_turn")
    def test_send_message_uses_deterministic_and_skips_gateway(self, mock_det, mock_gateway):
        from iic_booking.research_copilot.models import MessageRole
        from iic_booking.research_copilot.services import conversation as conv_svc

        mock_det.return_value = {
            "response_kind": "LIVE_DATA",
            "content": "Slots found",
            "cards": [{"type": "slots", "items": []}],
            "suggested_actions": [{"id": "book", "label": "Book", "href": "/book-equipment"}],
            "escalate_hint": False,
            "confidence": 0.9,
            "metadata": {"llm_used": False, "equipment_id": 1},
        }
        user = SimpleNamespace(
            is_authenticated=True,
            pk=99,
            email="a@example.com",
            user_type=UserType.STUDENT,
        )
        with patch.object(conv_svc.Message.objects, "create") as mock_create, patch.object(
            conv_svc, "build_context", return_value=SimpleNamespace(role_bucket="student", department_id=None, capabilities=[])
        ), patch.object(conv_svc, "serialize_message", return_value={"role": "assistant", "content": "Slots found"}), patch.object(
            conv_svc.tools_svc, "enrich_actions_from_message", side_effect=lambda **kw: kw.get("base_actions") or []
        ), patch.object(conv_svc.tools_svc, "list_tools_for_role", return_value=[]), patch.object(
            conv_svc.audit_svc, "audit_message_replied"
        ), patch.object(conv_svc, "transaction") as mock_tx:
            mock_tx.atomic.return_value.__enter__ = lambda s: None
            mock_tx.atomic.return_value.__exit__ = lambda *a: None
            assistant = SimpleNamespace(
                id="aid",
                role=MessageRole.ASSISTANT,
                content="Slots found",
                confidence=0.9,
                citations=[],
                suggested_actions=[],
                escalate_hint=False,
                metadata={},
                created_at=None,
            )
            mock_create.return_value = assistant
            conv = SimpleNamespace(
                id="cid",
                title="New conversation",
                messages=SimpleNamespace(filter=MagicMock(return_value=SimpleNamespace(count=MagicMock(return_value=0)))),
                save=MagicMock(),
            )
            conv.messages.filter.return_value.count.return_value = 0
            out = conv_svc.send_message(user=user, conversation=conv, content="Search available slots for FESEM this week")
        mock_gateway.assert_not_called()
        self.assertEqual(out["response_kind"], "LIVE_DATA")
        self.assertTrue(out.get("cards"))

    def test_ambiguous_equipment_clarification(self):
        from iic_booking.research_copilot.services.v2 import read_tools

        with patch(
            "iic_booking.research_copilot.services.v2.read_tools.resolve_equipment",
            return_value=EquipmentResolution(
                confidence="AMBIGUOUS",
                candidates=[
                    EquipmentCandidate(1, "FESEM Lab A"),
                    EquipmentCandidate(2, "FESEM Lab B"),
                ],
            ),
        ):
            result = read_tools.search_available_slots(user=None, text="slots for FESEM")
        self.assertEqual(result["response_kind"], "CLARIFICATION")
        self.assertTrue(result["cards"])

    def test_mutation_flags_off(self):
        self.assertFalse(mutations_enabled())
        prep = booking_mut.prepare_booking_create(user=None, equipment_id=1)
        self.assertFalse(prep.get("ok", True) and prep.get("executable", False))
        blocked = booking_mut.execute_booking_create(
            user=SimpleNamespace(is_authenticated=True, pk=1),
            proposal_id="x",
            confirmation_token="y",
            idempotency_key="z",
        )
        self.assertFalse(blocked["ok"])
        self.assertIn("DISABLED", str(blocked.get("error") or ""))


@override_settings(
    RESEARCH_COPILOT_ENABLED=True,
    COPILOT_V2_ENABLED=True,
    COPILOT_DETERMINISTIC_READS=True,
    COPILOT_USER_CONTEXT=True,
)
class AuthzReadTests(SimpleTestCase):
    def test_wallet_requires_auth(self):
        from iic_booking.research_copilot.services.v2.orchestrator import try_deterministic_turn

        result = try_deterministic_turn(user=None, text="What is my wallet balance?", public=True)
        self.assertIsNotNone(result)
        self.assertEqual(result["response_kind"], "ACTION_REQUIRED")

    def test_bookings_tool_rejects_foreign_user_id(self):
        from iic_booking.research_copilot.services import tools as tools_svc

        user = SimpleNamespace(is_authenticated=True, pk=1)
        with patch("iic_booking.equipment.models.Booking.objects.filter") as mock_filter:
            mock_filter.return_value.select_related.return_value.prefetch_related.return_value.order_by.return_value = []
            result = tools_svc._search_bookings(arguments={"user_id": 999}, user=user)
        self.assertFalse(result.get("ok", True))
        self.assertEqual(result.get("error"), "forbidden")


@override_settings(
    RESEARCH_COPILOT_ENABLED=True,
    COPILOT_V2_ENABLED=True,
    COPILOT_DETERMINISTIC_READS=True,
    COPILOT_AVAILABILITY=True,
    OPENAI_API_KEY="",
    COPILOT_LLM_PROVIDER="fallback",
    COPILOT_PROVIDER="fallback",
)
class ApiPathWithoutLlmTests(SimpleTestCase):
    """Hard gate: FESEM slots query succeeds with LLM gateway never invoked."""

    @patch("iic_booking.research_copilot.services.conversation.get_gateway")
    @patch("iic_booking.research_copilot.services.v2.orchestrator.try_deterministic_turn")
    def test_slots_path_without_llm(self, mock_det, mock_gw):
        from iic_booking.research_copilot.models import MessageRole
        from iic_booking.research_copilot.services import conversation as conv_svc

        mock_det.return_value = {
            "response_kind": "LIVE_DATA",
            "content": "**FESEM — Available slots (this week)**\n\n- 2026-03-26  10:00–12:00",
            "cards": [{"type": "slots", "title": "FESEM — Available", "items": [{"date": "2026-03-26"}]}],
            "suggested_actions": [],
            "escalate_hint": False,
            "confidence": 0.9,
            "metadata": {"llm_used": False, "equipment_id": 1, "deterministic": True},
        }
        user = SimpleNamespace(is_authenticated=True, pk=1, email="a@example.com", user_type=UserType.STUDENT)
        with patch.object(conv_svc.Message.objects, "create") as mock_create, patch.object(
            conv_svc, "build_context", return_value=SimpleNamespace(role_bucket="student", department_id=None, capabilities=[])
        ), patch.object(
            conv_svc,
            "serialize_message",
            side_effect=lambda m: {
                "role": "assistant",
                "content": m.content,
                "metadata": getattr(m, "metadata", {}) or {},
            },
        ), patch.object(
            conv_svc.tools_svc, "enrich_actions_from_message", side_effect=lambda **kw: kw.get("base_actions") or []
        ), patch.object(conv_svc.tools_svc, "list_tools_for_role", return_value=[]), patch.object(
            conv_svc.audit_svc, "audit_message_replied"
        ), patch.object(conv_svc, "transaction") as mock_tx:
            mock_tx.atomic.return_value.__enter__ = lambda s: None
            mock_tx.atomic.return_value.__exit__ = lambda *a: None

            def _create(**kwargs):
                return SimpleNamespace(id="x", created_at=None, **kwargs)

            mock_create.side_effect = _create
            conv = SimpleNamespace(
                id="cid",
                title="New conversation",
                messages=SimpleNamespace(filter=MagicMock(return_value=SimpleNamespace(count=MagicMock(return_value=0)))),
                save=MagicMock(),
            )
            conv.messages.filter.return_value.count.return_value = 0
            out = conv_svc.send_message(
                user=user,
                conversation=conv,
                content="Search available slots for FESEM this week",
            )
        mock_gw.assert_not_called()
        self.assertIn("Available", out["message"]["content"])
        self.assertFalse(out["message"]["metadata"].get("llm_used", True))
        self.assertTrue(out.get("cards"))

    @patch("iic_booking.research_copilot.services.conversation.consume_llm_quota", create=True)
    @patch("iic_booking.research_copilot.services.conversation.get_gateway")
    @patch("iic_booking.research_copilot.services.v2.orchestrator.try_deterministic_turn", return_value=None)
    def test_llm_quota_message_when_non_deterministic(self, _mock_det, mock_gw, _quota):
        """When deterministic misses, LLM quota gate runs; gateway not reached if quota denied."""
        from iic_booking.research_copilot.services import conversation as conv_svc

        user = SimpleNamespace(is_authenticated=True, pk=1, email="a@example.com", user_type=UserType.STUDENT)
        with patch(
            "iic_booking.research_copilot.throttles.consume_llm_quota",
            return_value=(False, "Research Copilot AI replies are rate-limited right now."),
        ), patch.object(conv_svc.Message.objects, "create") as mock_create, patch.object(
            conv_svc, "build_context", return_value=SimpleNamespace(role_bucket="student", department_id=None, capabilities=[])
        ), patch.object(
            conv_svc,
            "serialize_message",
            side_effect=lambda m: {"role": "assistant", "content": m.content, "metadata": getattr(m, "metadata", {}) or {}},
        ), patch.object(
            conv_svc.tools_svc, "enrich_actions_from_message", side_effect=lambda **kw: kw.get("base_actions") or []
        ), patch.object(conv_svc.tools_svc, "list_tools_for_role", return_value=[]), patch.object(
            conv_svc, "_static_actions", return_value=[]
        ), patch.object(conv_svc, "transaction") as mock_tx:
            mock_tx.atomic.return_value.__enter__ = lambda s: None
            mock_tx.atomic.return_value.__exit__ = lambda *a: None
            mock_create.side_effect = lambda **kwargs: SimpleNamespace(id="x", created_at=None, **kwargs)
            conv = SimpleNamespace(
                id="cid",
                title="New conversation",
                messages=SimpleNamespace(
                    filter=MagicMock(return_value=SimpleNamespace(count=MagicMock(return_value=0))),
                    order_by=MagicMock(return_value=[]),
                ),
                save=MagicMock(),
            )
            conv.messages.filter.return_value.count.return_value = 0
            out = conv_svc.send_message(user=user, conversation=conv, content="Explain electron diffraction qualitatively")
        mock_gw.assert_not_called()
        self.assertIn("rate-limited", out["message"]["content"].lower())
        self.assertTrue(out["message"]["metadata"].get("llm_quota_blocked"))


# Keep pytest markers unused for local sqlite; API coverage is in ApiPathWithoutLlmTests above.
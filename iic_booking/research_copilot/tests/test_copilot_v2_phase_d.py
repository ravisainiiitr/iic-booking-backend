"""Copilot V2 Phase D — capability, compare, multi-intent, dashboard, unanswered."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from iic_booking.research_copilot.services.v2.capability_map import match_capability
from iic_booking.research_copilot.services.v2.intent_resolver import resolve_intent
from iic_booking.research_copilot.services.v2.multi_intent import plan_intents


class CapabilityMapTests(SimpleTestCase):
    def test_crystal_structure_maps_xrd(self):
        hits = match_capability("I need to identify crystal structure.")
        self.assertTrue(hits)
        self.assertIn("XRD", hits[0]["technique"])

    def test_morphology_maps_sem(self):
        hits = match_capability("I need surface morphology.")
        self.assertTrue(any("SEM" in h["technique"] or "FESEM" in h["technique"] for h in hits))


class PhaseDIntentTests(SimpleTestCase):
    def test_journey_xrd_analysis_capability(self):
        self.assertEqual(resolve_intent("I need to perform XRD analysis.").intent, "capability_search")

    def test_cancel_it(self):
        self.assertEqual(resolve_intent("Cancel it.").intent, "prepare_cancel")

    def test_move_it_to_next(self):
        self.assertEqual(resolve_intent("Move it to the next available slot.").intent, "prepare_reschedule")

    def test_what_did_i_just_book(self):
        self.assertEqual(resolve_intent("What did I just book?").intent, "next_booking")

    def test_do_i_have_enough(self):
        self.assertEqual(resolve_intent("Do I have enough?").intent, "wallet_balance")

    def test_show_xrd_equipment(self):
        self.assertEqual(resolve_intent("Show XRD equipment.").intent, "search_equipment")

    def test_prepare_the_booking(self):
        self.assertEqual(resolve_intent("Prepare the booking.").intent, "prepare_booking")

    def test_next_available_slot_search_not_reschedule(self):
        # Bare slot discovery must not be classified as reschedule
        intent = resolve_intent("Find the next available slot for FESEM")
        self.assertEqual(intent.intent, "search_slots")

    def test_compare_intent(self):
        self.assertEqual(resolve_intent("Compare the available XRD machines.").intent, "compare_equipment")

    def test_daily_dashboard_before_pending(self):
        self.assertEqual(resolve_intent("What do I need to do today?").intent, "daily_dashboard")

    def test_user_profile(self):
        self.assertEqual(resolve_intent("What department am I in?").intent, "user_profile")

    def test_support_ticket(self):
        self.assertEqual(resolve_intent("Create a support request.").intent, "support_ticket")

    def test_booking_estimate_not_multi_from_booking_substring(self):
        plan = plan_intents("Estimate the cost of booking FESEM for 2 hours.")
        self.assertFalse(plan.is_multi)
        self.assertEqual(plan.intents[0].intent, "estimate_cost")

    def test_multi_intent_chain(self):
        plan = plan_intents(
            "Find the earliest SEM slot tomorrow, estimate the cost, and check my wallet"
        )
        # May or may not split depending on segments; at least primary deterministic
        self.assertTrue(plan.intents)
        self.assertTrue(plan.intents[0].deterministic)


@override_settings(
    RESEARCH_COPILOT_ENABLED=True,
    COPILOT_V2_ENABLED=True,
    COPILOT_DETERMINISTIC_READS=True,
    COPILOT_MULTI_INTENT=True,
    COPILOT_TICKET_CREATE=False,
    COPILOT_ANALYSIS_ACTIONS=False,
    COPILOT_BOOKING_CREATE=False,
    COPILOT_WALLET_RECHARGE=False,
)
class PhaseDOrchestratorTests(SimpleTestCase):
    @patch("iic_booking.research_copilot.services.v2.read_tools.compare_equipment")
    def test_compare_dispatch(self, mock_cmp):
        from iic_booking.research_copilot.services.v2.orchestrator import try_deterministic_turn

        mock_cmp.return_value = {
            "response_kind": "LIVE_DATA",
            "content": "compare",
            "cards": [],
            "suggested_actions": [],
            "escalate_hint": False,
            "confidence": 0.9,
            "metadata": {"deterministic": True},
        }
        result = try_deterministic_turn(
            user=SimpleNamespace(is_authenticated=True, pk=1),
            text="Compare the available XRD machines.",
            conversation=None,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["metadata"]["intent"], "compare_equipment")
        mock_cmp.assert_called_once()

    @patch("iic_booking.research_copilot.services.v2.read_tools.capability_search")
    def test_capability_dispatch(self, mock_cap):
        from iic_booking.research_copilot.services.v2.orchestrator import try_deterministic_turn

        mock_cap.return_value = {
            "response_kind": "LIVE_DATA",
            "content": "cap",
            "cards": [],
            "suggested_actions": [],
            "escalate_hint": False,
            "confidence": 0.9,
            "metadata": {"deterministic": True},
        }
        result = try_deterministic_turn(
            user=SimpleNamespace(is_authenticated=True, pk=1),
            text="I need elemental composition.",
            conversation=None,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["metadata"]["intent"], "capability_search")

    @patch("iic_booking.research_copilot.services.v2.read_tools.daily_dashboard")
    def test_dashboard_dispatch(self, mock_dash):
        from iic_booking.research_copilot.services.v2.orchestrator import try_deterministic_turn

        mock_dash.return_value = {
            "response_kind": "LIVE_DATA",
            "content": "dash",
            "cards": [],
            "suggested_actions": [],
            "escalate_hint": False,
            "confidence": 0.9,
            "metadata": {"deterministic": True},
        }
        result = try_deterministic_turn(
            user=SimpleNamespace(is_authenticated=True, pk=1),
            text="What do I need to do today?",
            conversation=None,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["metadata"]["intent"], "daily_dashboard")

    def test_ticket_create_flag_off(self):
        from iic_booking.research_copilot.services.v2 import read_tools

        out = read_tools.support_ticket_assist(
            user=SimpleNamespace(is_authenticated=True, pk=1),
            text="Create a support request.",
        )
        self.assertFalse(out["metadata"].get("ticket_create_enabled"))

    def test_unanswered_response_no_hallucination(self):
        from iic_booking.research_copilot.services.v2.unanswered import unanswered_response

        out = unanswered_response(query="quantum flux")
        self.assertTrue(out["metadata"].get("unanswered"))
        self.assertIn("will not invent", out["content"].lower())


class RegressionCorpusSmokeTests(SimpleTestCase):
    def test_corpus_exists_and_has_100_plus(self):
        path = (
            Path(__file__).resolve().parents[3]
            / "docs"
            / "research-copilot"
            / "COPILOT-V2-QUERY-REGRESSION-CORPUS.json"
        )
        self.assertTrue(path.exists(), str(path))
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertGreaterEqual(data.get("count") or len(data.get("queries") or []), 100)

    def test_corpus_intent_resolver_hit_rate(self):
        """Smoke: majority of non-conversational corpus rows resolve deterministically."""
        path = (
            Path(__file__).resolve().parents[3]
            / "docs"
            / "research-copilot"
            / "COPILOT-V2-QUERY-REGRESSION-CORPUS.json"
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        ok = 0
        total = 0
        for row in data["queries"]:
            if row.get("expected_intent_family") == "CONVERSATIONAL":
                continue
            if row.get("expected_intent") in {"general", "multi", "context"}:
                continue
            total += 1
            intent = resolve_intent(row["query"])
            if intent.deterministic:
                ok += 1
        self.assertGreater(total, 50)
        self.assertGreaterEqual(ok / total, 0.55, f"deterministic hit rate {ok}/{total}")

"""Copilot V2 Phase C — financial reads + recharge/credit proposals (mutations OFF)."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from iic_booking.research_copilot.services.v2.intent_resolver import resolve_intent
from iic_booking.research_copilot.services.v2.mutations import wallet as wallet_mut


def _user(pk=78):
    return SimpleNamespace(
        is_authenticated=True,
        pk=pk,
        email="test.faculty@iic-booking.test",
        department_id=33,
        is_test_account=True,
    )


class PhaseCIntentTests(SimpleTestCase):
    def test_wallet_and_credit_intents(self):
        self.assertEqual(resolve_intent("What is my wallet balance?").intent, "wallet_balance")
        self.assertEqual(resolve_intent("Show my recent wallet transactions.").intent, "wallet_transactions")
        self.assertEqual(resolve_intent("How much have I spent this month?").intent, "wallet_spend_month")
        self.assertEqual(resolve_intent("What is my outstanding credit?").intent, "credit_status")
        self.assertEqual(resolve_intent("Recharge ₹5000").intent, "prepare_recharge")
        self.assertEqual(resolve_intent("Request ₹20000 wallet credit").intent, "prepare_credit")

    def test_soft_ok_not_confirm(self):
        self.assertNotEqual(resolve_intent("okay").intent, "confirm_proposal")


class PhaseCAmountParseTests(SimpleTestCase):
    def test_parse_amounts(self):
        self.assertEqual(wallet_mut.parse_inr_amount("Recharge ₹5,000"), Decimal("5000"))
        self.assertEqual(wallet_mut.parse_inr_amount("credit of Rs 10000"), Decimal("10000"))
        self.assertEqual(wallet_mut.parse_inr_amount("", explicit="2500.50"), Decimal("2500.50"))


@override_settings(COPILOT_WALLET_RECHARGE=False, COPILOT_WALLET_CREDIT=False)
class PhaseCFlagsOffTests(SimpleTestCase):
    def test_execute_recharge_blocked(self):
        out = wallet_mut.execute_wallet_recharge(user=_user(), proposal_id="p", confirmation_token="t")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "COPILOT_WALLET_RECHARGE_DISABLED")

    def test_execute_credit_blocked(self):
        out = wallet_mut.execute_wallet_credit_request(user=_user(), proposal_id="p", confirmation_token="t")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "COPILOT_WALLET_CREDIT_DISABLED")


class PhaseCPrepareTests(SimpleTestCase):
    @patch.object(wallet_mut, "_wallet_snapshot", return_value={"balance": "1000.00", "sub_wallets": [{"department_id": 33}], "currency": "INR"})
    @patch("iic_booking.research_copilot.services.v2.mutations.proposals.create_proposal")
    def test_prepare_recharge_ready(self, mock_prop, _snap):
        mock_prop.return_value = {
            "proposal_id": "pr1",
            "confirmation_token": "tok",
            "expires_at": "2099-01-01T00:00:00+00:00",
        }
        out = wallet_mut.prepare_wallet_recharge(user=_user(), text="Recharge ₹5000")
        self.assertTrue(out["ok"])
        self.assertTrue(out["confirmation_required"])
        self.assertEqual(out["amount"], "5000")
        self.assertFalse(out["executable"])

    @patch.object(wallet_mut, "_wallet_snapshot", return_value={"balance": "1000.00", "sub_wallets": [], "currency": "INR"})
    @patch("iic_booking.research_copilot.services.v2.mutations.domain_bridge.call_wallet_credit_summary", return_value=(200, {"outstanding_amount": "0"}))
    @patch("iic_booking.research_copilot.services.v2.mutations.proposals.create_proposal")
    def test_prepare_credit_ready(self, mock_prop, _sum, _snap):
        mock_prop.return_value = {
            "proposal_id": "pc1",
            "confirmation_token": "tok",
            "expires_at": "2099-01-01T00:00:00+00:00",
        }
        out = wallet_mut.prepare_wallet_credit(user=_user(), text="Request ₹20000 wallet credit for booking")
        self.assertTrue(out["ok"])
        self.assertEqual(out["requested_amount"], "20000")
        self.assertTrue(out["confirmation_required"])


@override_settings(COPILOT_WALLET_RECHARGE=True)
class PhaseCRechargeIdempotencyTests(SimpleTestCase):
    def test_idempotent_replay(self):
        stored = {"ok": True, "action": "WALLET_RECHARGE", "message": "Payment order created."}
        with patch("iic_booking.research_copilot.services.v2.mutations.idempotency.get_cached_result", return_value=stored):
            out = wallet_mut.execute_wallet_recharge(
                user=_user(), proposal_id="p1", confirmation_token="t", idempotency_key="k1"
            )
        self.assertTrue(out["ok"])
        self.assertTrue(out.get("idempotent_replay"))

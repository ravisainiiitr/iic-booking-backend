"""Copilot V2 Phase B — booking prepare/confirm/idempotency/security (flags default OFF)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from iic_booking.research_copilot.services.v2.intent_resolver import resolve_intent
from iic_booking.research_copilot.services.v2.mutations import booking as booking_mut
from iic_booking.research_copilot.services.v2.mutations import idempotency as idem
from iic_booking.research_copilot.services.v2.mutations import proposals as prop_store
from iic_booking.research_copilot.services.v2.mutations import mutations_enabled
from iic_booking.users.models.user_type import UserType


def _user(pk=7):
    return SimpleNamespace(is_authenticated=True, pk=pk, email="b@example.com", user_type=UserType.STUDENT)


class PhaseBIntentTests(SimpleTestCase):
    def test_book_it_intent(self):
        self.assertEqual(resolve_intent("Book it").intent, "prepare_booking")

    def test_confirm_intent(self):
        self.assertEqual(resolve_intent("Confirm").intent, "confirm_proposal")
        self.assertEqual(resolve_intent("Yes, book it").intent, "confirm_proposal")

    def test_soft_ok_not_confirm(self):
        self.assertNotEqual(resolve_intent("okay").intent, "confirm_proposal")
        self.assertNotEqual(resolve_intent("looks good").intent, "confirm_proposal")

    def test_cancel_reschedule_intents(self):
        self.assertEqual(resolve_intent("Cancel my next booking").intent, "prepare_cancel")
        self.assertEqual(resolve_intent("Reschedule my next booking").intent, "prepare_reschedule")
        self.assertEqual(
            resolve_intent("Move booking 460 to the next available slot.").intent,
            "prepare_reschedule",
        )

@override_settings(
    COPILOT_BOOKING_CREATE=False,
    COPILOT_BOOKING_CANCEL=False,
    COPILOT_BOOKING_RESCHEDULE=False,
    COPILOT_WALLET_RECHARGE=False,
    COPILOT_BOOKING_E2E_TEST_MODE=False,
)
class PhaseBFlagsOffTests(SimpleTestCase):
    def test_mutations_disabled_master(self):
        self.assertFalse(mutations_enabled())

    def test_execute_create_blocked(self):
        user = _user()
        out = booking_mut.execute_booking_create(
            user=user, proposal_id="x", confirmation_token="y", idempotency_key="k"
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "COPILOT_BOOKING_CREATE_DISABLED")

    def test_execute_cancel_blocked(self):
        out = booking_mut.execute_booking_cancel(
            user=_user(), proposal_id="x", confirmation_token="y"
        )
        self.assertEqual(out["error"], "COPILOT_BOOKING_CANCEL_DISABLED")

    def test_execute_reschedule_blocked(self):
        out = booking_mut.execute_booking_reschedule(
            user=_user(), proposal_id="x", confirmation_token="y"
        )
        self.assertEqual(out["error"], "COPILOT_BOOKING_RESCHEDULE_DISABLED")


class ProposalSecurityTests(SimpleTestCase):
    def test_wrong_user_rejected(self):
        user_b = _user(2)
        with patch("iic_booking.research_copilot.services.v2.mutations.proposals.cache.get") as mock_get:
            mock_get.return_value = {
                "proposal_id": "p1",
                "confirmation_token": "tok",
                "action": "CREATE_BOOKING",
                "user_id": 1,
                "expires_at": "2099-01-01T00:00:00+00:00",
                "payload": {},
            }
            prop, err = prop_store.validate_proposal_for_user(
                user=user_b, proposal_id="p1", confirmation_token="tok", expected_action="CREATE_BOOKING"
            )
        self.assertIsNone(prop)
        self.assertEqual(err, "PROPOSAL_FORBIDDEN")

    def test_wrong_token_rejected(self):
        user = _user(1)
        with patch("iic_booking.research_copilot.services.v2.mutations.proposals.cache.get") as mock_get:
            mock_get.return_value = {
                "proposal_id": "p1",
                "confirmation_token": "tok",
                "action": "CREATE_BOOKING",
                "user_id": 1,
                "expires_at": "2099-01-01T00:00:00+00:00",
                "payload": {},
            }
            prop, err = prop_store.validate_proposal_for_user(
                user=user, proposal_id="p1", confirmation_token="WRONG", expected_action="CREATE_BOOKING"
            )
        self.assertEqual(err, "CONFIRMATION_INVALID")


@override_settings(COPILOT_BOOKING_CREATE=True)
class PhaseBCreateWithFlagTests(SimpleTestCase):
    def test_idempotent_replay(self):
        user = _user(9)
        key = "copilot:9:CREATE_BOOKING:p1"
        stored = {"ok": True, "action": "CREATE_BOOKING", "message": "Booking confirmed. Booking ID: 1"}
        with patch.object(idem, "get_cached_result", return_value=stored):
            out = booking_mut.execute_booking_create(
                user=user, proposal_id="p1", confirmation_token="t", idempotency_key=key
            )
        self.assertTrue(out["ok"])
        self.assertTrue(out.get("idempotent_replay"))

    def test_execute_calls_domain_when_valid(self):
        user = _user(3)
        prop = {
            "proposal_id": "p2",
            "confirmation_token": "tok",
            "action": "CREATE_BOOKING",
            "user_id": 3,
            "expires_at": "2099-01-01T00:00:00+00:00",
            "payload": {
                "equipment_id": 10,
                "slot_ids": [100],
                "sample_count": 1,
                "number_of_samples": 1,
                "input_values": {"A": "1"},
            },
        }
        slot = SimpleNamespace(
            pk=100,
            status="AVAILABLE",
            booking_id=None,
            slot_master=SimpleNamespace(equipment_id=10),
        )
        with patch.object(prop_store, "validate_proposal_for_user", return_value=(prop, None)), patch.object(
            booking_mut, "_load_slot", return_value=(slot, None)
        ), patch(
            "iic_booking.research_copilot.services.v2.mutations.domain_bridge.call_book_equipment",
            return_value=(201, {"booking_id": 555, "real_booking_id": 555}),
        ) as mock_book, patch.object(prop_store, "invalidate_proposal"), patch.object(
            idem, "get_cached_result", return_value=None
        ), patch.object(idem, "store_result") as mock_store, patch.object(booking_mut, "_audit"):
            out = booking_mut.execute_booking_create(
                user=user, proposal_id="p2", confirmation_token="tok", idempotency_key="idem-1"
            )
        self.assertTrue(out["ok"])
        mock_book.assert_called_once()
        mock_store.assert_called_once()

    def test_slot_unavailable_before_execute(self):
        user = _user(3)
        prop = {
            "proposal_id": "p3",
            "confirmation_token": "tok",
            "action": "CREATE_BOOKING",
            "user_id": 3,
            "expires_at": "2099-01-01T00:00:00+00:00",
            "payload": {"equipment_id": 10, "slot_ids": [100]},
        }
        with patch.object(prop_store, "validate_proposal_for_user", return_value=(prop, None)), patch.object(
            booking_mut, "_load_slot", return_value=(None, "SLOT_UNAVAILABLE")
        ), patch.object(idem, "get_cached_result", return_value=None), patch.object(booking_mut, "_audit"):
            out = booking_mut.execute_booking_create(
                user=user, proposal_id="p3", confirmation_token="tok", idempotency_key="idem-2"
            )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "SLOT_UNAVAILABLE")


@override_settings(COPILOT_BOOKING_CANCEL=True)
class PhaseBCancelAuthzTests(SimpleTestCase):
    def test_foreign_booking_prepare_fails(self):
        user = _user(1)
        with patch.object(booking_mut, "_booking_owned", return_value=(None, "BOOKING_NOT_FOUND")):
            out = booking_mut.prepare_cancellation(user=user, booking_id=999)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "BOOKING_FORBIDDEN")


@override_settings(
    COPILOT_BOOKING_CREATE=False,
    COPILOT_BOOKING_CANCEL=False,
    COPILOT_BOOKING_RESCHEDULE=False,
    COPILOT_BOOKING_E2E_TEST_MODE=True,
    COPILOT_BOOKING_TEST_USER_IDS="42",
)
class PhaseBE2EAllowlistTests(SimpleTestCase):
    def test_allowlisted_test_user_can_execute_path(self):
        from iic_booking.research_copilot.services.v2.mutations import booking_mutation_allowed

        u = SimpleNamespace(is_authenticated=True, pk=42, is_test_account=True)
        self.assertTrue(booking_mutation_allowed(u, "COPILOT_BOOKING_CREATE"))
        self.assertTrue(booking_mutation_allowed(u, "COPILOT_BOOKING_CANCEL"))
        self.assertTrue(booking_mutation_allowed(u, "COPILOT_BOOKING_RESCHEDULE"))

    def test_real_user_blocked_even_if_id_listed(self):
        from iic_booking.research_copilot.services.v2.mutations import booking_mutation_allowed

        u = SimpleNamespace(is_authenticated=True, pk=42, is_test_account=False)
        self.assertFalse(booking_mutation_allowed(u, "COPILOT_BOOKING_CREATE"))

    def test_other_test_user_not_on_allowlist_blocked(self):
        from iic_booking.research_copilot.services.v2.mutations import booking_mutation_allowed

        u = SimpleNamespace(is_authenticated=True, pk=99, is_test_account=True)
        self.assertFalse(booking_mutation_allowed(u, "COPILOT_BOOKING_CREATE"))

    def test_e2e_mode_without_allowlist_fail_closed(self):
        from iic_booking.research_copilot.services.v2.mutations import booking_mutation_allowed

        with override_settings(COPILOT_BOOKING_TEST_USER_IDS=""):
            u = SimpleNamespace(is_authenticated=True, pk=42, is_test_account=True)
            self.assertFalse(booking_mutation_allowed(u, "COPILOT_BOOKING_CREATE"))

    def test_wallet_flags_never_via_e2e(self):
        from iic_booking.research_copilot.services.v2.mutations import booking_mutation_allowed

        u = SimpleNamespace(is_authenticated=True, pk=42, is_test_account=True)
        self.assertFalse(booking_mutation_allowed(u, "COPILOT_WALLET_RECHARGE"))


class PhaseARegressionIntentTests(SimpleTestCase):
    def test_fesem_slots_still_deterministic(self):
        i = resolve_intent("Search available slots for FESEM this week")
        self.assertEqual(i.intent, "search_slots")
        self.assertTrue(i.deterministic)

    def test_wallet_still_read(self):
        self.assertEqual(resolve_intent("What is my wallet balance?").intent, "wallet_balance")


class PhaseD2BookingIdParseTests(SimpleTestCase):
    def test_short_booking_id_from_phrase(self):
        self.assertEqual(booking_mut._extract_booking_id_from_text("Cancel booking 460."), 460)
        self.assertEqual(booking_mut._extract_booking_id_from_text("Reschedule booking ID: 12"), 12)

    def test_bare_year_not_treated_as_booking_id(self):
        # Years are 4 digits; bare numeric extraction requires 6+.
        self.assertIsNone(booking_mut._extract_booking_id_from_text("Move it on 2026-08-27"))

    def test_parse_window_from_text(self):
        start, end = booking_mut._parse_window_from_text("Reschedule booking 460 to 2026-08-27 04:30.")
        self.assertIsNotNone(start)
        self.assertIsNotNone(end)
        self.assertIn("2026-08-27", start)
        self.assertIn("04:30", start)

"""Unit tests for Equipment PI pricing resolution (no DB fixtures for wallet)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from iic_booking.equipment.models import ChargeProfilePricingProfile
from iic_booking.equipment import pi_pricing


class PiPricingResolutionTests(SimpleTestCase):
    def test_standard_when_not_pi(self):
        user = SimpleNamespace(pk=1, use_discounted_charge_profile=False)
        equipment = SimpleNamespace(pk=10)
        with patch.object(pi_pricing, "billing_identity_is_equipment_pi", return_value=False):
            with patch.object(pi_pricing, "equipment_has_pi_charge_profiles", return_value=True):
                profile = pi_pricing.resolve_pricing_profile_for_user(user, equipment)
        self.assertEqual(profile, ChargeProfilePricingProfile.STANDARD)

    def test_pi_when_billing_identity_is_pi_and_profiles_exist(self):
        user = SimpleNamespace(pk=1, use_discounted_charge_profile=False)
        equipment = SimpleNamespace(pk=10)
        with patch.object(pi_pricing, "billing_identity_is_equipment_pi", return_value=True):
            with patch.object(pi_pricing, "equipment_has_pi_charge_profiles", return_value=True):
                profile = pi_pricing.resolve_pricing_profile_for_user(user, equipment)
        self.assertEqual(profile, ChargeProfilePricingProfile.PI)

    def test_fallback_when_pi_but_no_pi_profiles(self):
        user = SimpleNamespace(pk=1, use_discounted_charge_profile=False)
        equipment = SimpleNamespace(pk=10)
        with patch.object(pi_pricing, "billing_identity_is_equipment_pi", return_value=True):
            with patch.object(pi_pricing, "equipment_has_pi_charge_profiles", return_value=False):
                profile = pi_pricing.resolve_pricing_profile_for_user(user, equipment)
        self.assertEqual(profile, ChargeProfilePricingProfile.STANDARD)

    def test_wallet_owner_pi_counts(self):
        owner = SimpleNamespace(pk=99, email="pi@example.com")
        user = SimpleNamespace(pk=1, email="student@example.com")
        equipment = SimpleNamespace(pk=10)

        def is_pi(u, _eq):
            return getattr(u, "pk", None) == 99

        with patch.object(pi_pricing, "is_equipment_pi", side_effect=is_pi):
            with patch.object(pi_pricing, "wallet_owner_user", return_value=owner):
                self.assertTrue(pi_pricing.billing_identity_is_equipment_pi(user, equipment))

    def test_frontend_cannot_spoof_via_resolver(self):
        """Resolver ignores any client-supplied is_pi flag (not even accepted as arg)."""
        user = SimpleNamespace(pk=1, use_discounted_charge_profile=False, is_pi=True)
        equipment = SimpleNamespace(pk=10)
        with patch.object(pi_pricing, "billing_identity_is_equipment_pi", return_value=False):
            with patch.object(pi_pricing, "equipment_has_pi_charge_profiles", return_value=True):
                profile = pi_pricing.resolve_pricing_profile_for_user(user, equipment)
        self.assertEqual(profile, ChargeProfilePricingProfile.STANDARD)
        self.assertNotIn("is_pi", pi_pricing.resolve_pricing_profile_for_user.__code__.co_varnames)

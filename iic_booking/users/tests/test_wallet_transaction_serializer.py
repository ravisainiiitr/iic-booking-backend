"""Tests for SubWalletTransactionSerializer equipment_name parsing."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from iic_booking.users.serializers.wallet_serializer import SubWalletTransactionSerializer


class SubWalletTransactionEquipmentNameTests(SimpleTestCase):
    def setUp(self):
        self.serializer = SubWalletTransactionSerializer()

    def _txn(self, description: str):
        return SimpleNamespace(description=description)

    def test_booking_debit_with_spaced_equipment_code(self):
        """Regression: codes like 'PXRD [A]' must not yield null equipment_name."""
        desc = (
            "Booking #PXRD [A] - Powder X-Ray Diffraction (XRD) (40 minutes) "
            "- Student: Test IITR Student | Ref: IICPXRD"
        )
        self.assertEqual(
            self.serializer.get_equipment_name(self._txn(desc)),
            "Powder X-Ray Diffraction (XRD)",
        )

    def test_booking_debit_with_simple_code(self):
        desc = "Booking #TGA - Thermogravimetric Analyzer (60 minutes)"
        self.assertEqual(
            self.serializer.get_equipment_name(self._txn(desc)),
            "Thermogravimetric Analyzer",
        )

    def test_urgent_approval_with_spaced_code(self):
        desc = (
            "Urgent approval: Booking #PXRD [A] - Powder X-Ray Diffraction "
            "(Hold converted) | Ref: IICPXRD"
        )
        self.assertEqual(
            self.serializer.get_equipment_name(self._txn(desc)),
            "Powder X-Ray Diffraction",
        )

    def test_refund_resolves_spaced_code_to_name(self):
        desc = "Refund for Booking #123 - PXRD [A] | Ref: IICPXRD"
        fake_eq = SimpleNamespace(name="Powder X-Ray Diffraction")
        with patch(
            "iic_booking.equipment.models.Equipment.objects.filter",
            return_value=MagicMock(first=MagicMock(return_value=fake_eq)),
        ) as filt:
            name = self.serializer.get_equipment_name(self._txn(desc))
            filt.assert_called_once_with(code="PXRD [A]")
        self.assertEqual(name, "Powder X-Ray Diffraction")

    def test_refund_returns_code_when_equipment_missing(self):
        desc = "Refund (Operator Unavailable) for Booking #99 - PXRD [A]"
        with patch(
            "iic_booking.equipment.models.Equipment.objects.filter",
            return_value=MagicMock(first=MagicMock(return_value=None)),
        ):
            self.assertEqual(
                self.serializer.get_equipment_name(self._txn(desc)),
                "PXRD [A]",
            )

    def test_fallback_without_minutes(self):
        desc = "Booking #PXRD [A] - Powder X-Ray Diffraction | Ref: IICPXRD"
        self.assertEqual(
            self.serializer.get_equipment_name(self._txn(desc)),
            "Powder X-Ray Diffraction",
        )

    def test_empty_description(self):
        self.assertIsNone(self.serializer.get_equipment_name(self._txn("")))
        self.assertIsNone(self.serializer.get_equipment_name(self._txn("Wallet top-up")))

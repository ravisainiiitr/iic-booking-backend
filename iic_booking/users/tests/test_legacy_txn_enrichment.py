"""Unit tests for legacy wallet transaction display enrichment."""

from django.test import SimpleTestCase

from iic_booking.users.legacy_ledger.legacy_txn_enrichment import parse_legacy_equipment_name


class ParseLegacyEquipmentNameTests(SimpleTestCase):
    def test_paid_towards_epma(self):
        self.assertEqual(
            parse_legacy_equipment_name(
                "Paid towards Electron Probe Micro-Analysis (EPMA) Booking."
            ),
            "Electron Probe Micro-Analysis (EPMA)",
        )

    def test_empty(self):
        self.assertIsNone(parse_legacy_equipment_name(""))
        self.assertIsNone(parse_legacy_equipment_name(None))

    def test_unrelated(self):
        self.assertIsNone(parse_legacy_equipment_name("Wallet top-up via NEFT"))

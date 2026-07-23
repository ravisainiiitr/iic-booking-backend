"""Unit tests for Slot Tolerance allocation helpers."""

from django.test import SimpleTestCase

from iic_booking.equipment.slot_allocation import (
    allocated_capacity_covers_analysis,
    slots_needed_for_analysis_time,
)


class SlotToleranceMathTests(SimpleTestCase):
    def test_examples_from_spec(self):
        # SlotDuration=30, Analysis=35, Tolerance=5 → 1 slot
        self.assertEqual(slots_needed_for_analysis_time(35, 30, 5), 1)
        # SlotDuration=30, Analysis=37, Tolerance=5 → 2 slots
        self.assertEqual(slots_needed_for_analysis_time(37, 30, 5), 2)

    def test_zero_tolerance_matches_legacy_ceil(self):
        self.assertEqual(slots_needed_for_analysis_time(30, 30, 0), 1)
        self.assertEqual(slots_needed_for_analysis_time(31, 30, 0), 2)
        self.assertEqual(slots_needed_for_analysis_time(60, 30, 0), 2)
        self.assertEqual(slots_needed_for_analysis_time(61, 30, 0), 3)

    def test_at_least_one_when_analysis_positive(self):
        self.assertEqual(slots_needed_for_analysis_time(5, 30, 10), 1)
        self.assertEqual(slots_needed_for_analysis_time(1, 60, 0), 1)

    def test_zero_analysis_returns_zero(self):
        self.assertEqual(slots_needed_for_analysis_time(0, 30, 5), 0)

    def test_allocated_capacity_covers(self):
        self.assertTrue(allocated_capacity_covers_analysis(30, 35, 5))
        self.assertFalse(allocated_capacity_covers_analysis(30, 37, 5))
        self.assertTrue(allocated_capacity_covers_analysis(60, 37, 5))
        # tolerance 0: strict
        self.assertFalse(allocated_capacity_covers_analysis(30, 31, 0))
        self.assertTrue(allocated_capacity_covers_analysis(30, 30, 0))

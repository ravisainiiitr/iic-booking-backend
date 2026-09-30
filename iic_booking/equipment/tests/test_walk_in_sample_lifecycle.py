"""Walk-in equipment (no sample submission lead time and no collect / discard deadline).

Users bring their samples to the slot and take them back, so no sample collection notice,
preserve-and-return question, disposal email or automatic "Booking Not Utilized" applies.
"""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import SimpleTestCase
from django.utils import timezone

from iic_booking.equipment.booking_events import (
    BOOKING_CONFIRMATION_INSTRUCTIONS,
    BOOKING_CONFIRMATION_INSTRUCTIONS_WALK_IN,
    _append_confirmation_instructions_to_context,
)
from iic_booking.equipment.sample_lifecycle_policy import (
    equipment_has_sample_collect_deadline,
    equipment_is_walk_in_sample,
)
from iic_booking.users.models.user_type import UserType


def _equipment(lead, collect):
    return SimpleNamespace(
        sample_submission_lead_hours=lead,
        sample_collect_deadline_hours=collect,
        name="XRD",
        code="XRD1",
    )


def _booking(equipment, user_type=UserType.STUDENT):
    return SimpleNamespace(
        booking_id=1,
        virtual_booking_id="IICXRD1-0001",
        equipment=equipment,
        user=SimpleNamespace(name="U", email="u@example.test", user_type=user_type),
        completed_at=timezone.now(),
    )


class WalkInPolicyTests(SimpleTestCase):
    def test_walk_in_only_when_both_are_zero_or_unset(self):
        self.assertTrue(equipment_is_walk_in_sample(_equipment(0, 0)))
        self.assertTrue(equipment_is_walk_in_sample(_equipment(None, None)))
        self.assertFalse(equipment_is_walk_in_sample(_equipment(24, 0)))
        self.assertFalse(equipment_is_walk_in_sample(_equipment(0, 72)))
        self.assertFalse(equipment_is_walk_in_sample(None))

    def test_collect_deadline(self):
        self.assertFalse(equipment_has_sample_collect_deadline(_equipment(24, 0)))
        self.assertFalse(equipment_has_sample_collect_deadline(_equipment(24, None)))
        self.assertTrue(equipment_has_sample_collect_deadline(_equipment(0, 72)))


class SampleNoticeContextTests(SimpleTestCase):
    def _external_type(self):
        return sorted(UserType.get_external_user_codes())[0]

    def test_no_collect_deadline_means_no_collection_notice(self):
        from iic_booking.equipment.api_views import (
            _append_sample_notice_html,
            _append_sample_notice_plaintext,
            _build_sample_notice_context,
        )

        ctx = _build_sample_notice_context(_booking(_equipment(24, 0)))
        self.assertEqual(ctx["sample_collection_deadline_display"], "")
        self.assertEqual(ctx["sample_collection_notice"], "")
        self.assertIsNone(ctx["sample_collection_deadline_at"])
        self.assertFalse(ctx["is_external_user"])
        self.assertEqual(_append_sample_notice_plaintext("Done.", ctx), "Done.")
        self.assertEqual(_append_sample_notice_html("<p>Done.</p>", ctx), "<p>Done.</p>")

    def test_walk_in_external_user_gets_no_preserve_links(self):
        from iic_booking.equipment.api_views import (
            _append_sample_notice_html,
            _append_sample_notice_plaintext,
            _build_sample_notice_context,
        )

        ctx = _build_sample_notice_context(_booking(_equipment(0, 0), self._external_type()))
        self.assertFalse(ctx["is_external_user"])
        self.assertEqual(_append_sample_notice_plaintext("Done.", ctx), "Done.")
        self.assertEqual(_append_sample_notice_html("<p>Done.</p>", ctx), "<p>Done.</p>")

    def test_collect_deadline_keeps_notice_and_external_links(self):
        from iic_booking.equipment.api_views import (
            _append_sample_notice_html,
            _append_sample_notice_plaintext,
            _build_sample_notice_context,
        )

        ctx = _build_sample_notice_context(_booking(_equipment(24, 72), self._external_type()))
        self.assertTrue(ctx["sample_collection_deadline_display"])
        self.assertIn("ready for collection", ctx["sample_collection_notice"])
        self.assertTrue(ctx["is_external_user"])
        plain = _append_sample_notice_plaintext("Done.", ctx)
        self.assertIn("Sample Collection Deadline", plain)
        self.assertIn("sample_preservation=YES", plain)
        html_out = _append_sample_notice_html("<p>Done.</p>", ctx)
        self.assertIn("Sample Collection Deadline", html_out)
        self.assertIn("sample_preservation=NO", html_out)

    def test_external_user_without_collect_deadline_keeps_links_only(self):
        from iic_booking.equipment.api_views import (
            _append_sample_notice_plaintext,
            _build_sample_notice_context,
        )

        ctx = _build_sample_notice_context(_booking(_equipment(24, 0), self._external_type()))
        self.assertTrue(ctx["is_external_user"])
        plain = _append_sample_notice_plaintext("Done.", ctx)
        self.assertNotIn("Sample Collection Deadline", plain)
        self.assertIn("sample_preservation=YES", plain)


class ConfirmationInstructionsTests(SimpleTestCase):
    def test_walk_in_equipment_gets_walk_in_instructions(self):
        ctx = _append_confirmation_instructions_to_context({"comment": ""}, equipment=_equipment(0, 0))
        self.assertEqual(ctx["booking_confirmation_instructions"], BOOKING_CONFIRMATION_INSTRUCTIONS_WALK_IN)
        self.assertIn("bring your sample", ctx["comment"])
        self.assertNotIn("10:00", ctx["comment"])

    def test_regular_equipment_keeps_drop_off_times(self):
        ctx = _append_confirmation_instructions_to_context({"comment": ""}, equipment=_equipment(24, 72))
        self.assertEqual(ctx["booking_confirmation_instructions"], BOOKING_CONFIRMATION_INSTRUCTIONS)
        ctx = _append_confirmation_instructions_to_context({"comment": ""})
        self.assertEqual(ctx["comment"], BOOKING_CONFIRMATION_INSTRUCTIONS)


@pytest.mark.django_db
def test_check_booking_not_utilized_skips_walk_in_equipment(egs_factory):
    from iic_booking.equipment.tasks import check_booking_not_utilized

    student = egs_factory.student()
    past = timezone.now() - timedelta(days=3)
    walk_in = egs_factory.equipment(sample_submission_lead_hours=0, sample_collect_deadline_hours=0)
    regular = egs_factory.equipment(sample_submission_lead_hours=24, sample_collect_deadline_hours=72)
    walk_in_booking = egs_factory.booking(student, walk_in, past)
    regular_booking = egs_factory.booking(student, regular, past)

    seen = []

    def _apply(booking, **kwargs):
        seen.append(booking.pk)
        return True

    with patch("iic_booking.equipment.models.Holiday.is_holiday", return_value=(False, None)), patch(
        "iic_booking.equipment.booking_not_utilized_service.apply_booking_not_utilized", side_effect=_apply
    ):
        marked = check_booking_not_utilized()

    assert regular_booking.pk in seen
    assert walk_in_booking.pk not in seen
    assert marked == len(seen)


@pytest.mark.django_db
def test_archive_expired_samples_skips_walk_in_equipment(egs_factory):
    from iic_booking.equipment.models import (
        BookingBufferConfig,
        BookingSampleTrace,
        BookingStatus,
        SampleTraceStatus,
    )
    from iic_booking.equipment.tasks import archive_expired_samples

    BookingBufferConfig.objects.all().delete()
    BookingBufferConfig.objects.create(sample_retention_days=1, auto_archive_enabled=True)

    student = egs_factory.student()
    past = timezone.now() - timedelta(days=10)
    walk_in = egs_factory.equipment(sample_submission_lead_hours=0, sample_collect_deadline_hours=0)
    regular = egs_factory.equipment(sample_submission_lead_hours=24, sample_collect_deadline_hours=72)
    bookings = {}
    for key, eq in (("walk_in", walk_in), ("regular", regular)):
        b = egs_factory.booking(student, eq, past)
        type(b).objects.filter(pk=b.pk).update(status=BookingStatus.COMPLETED)
        trace = BookingSampleTrace.objects.create(booking=b, status=SampleTraceStatus.COMPLETED)
        BookingSampleTrace.objects.filter(pk=trace.pk).update(created_at=past)
        bookings[key] = b

    with patch("iic_booking.communication.service.CommunicationService.send_email") as send_email:
        disposed = archive_expired_samples()

    assert disposed == 1
    disposed_ids = set(
        BookingSampleTrace.objects.filter(status=SampleTraceStatus.DISPOSED).values_list("booking_id", flat=True)
    )
    assert disposed_ids == {bookings["regular"].pk}
    assert send_email.call_count == 1

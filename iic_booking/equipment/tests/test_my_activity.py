"""My activity panel: the signed-in user's own next booking, schedule, results, spending and book-again list."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.files.base import ContentFile
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.models import BookingResultFile, BookingResultView, BookingSlotRange, BookingStatus
from iic_booking.equipment.tests.test_global_search import _booking, _department, _equipment
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db
URL = "/api/profiles/me/activity/"


def _get(user):
    client = APIClient()
    client.force_authenticate(user=user)
    res = client.get(URL)
    assert res.status_code == 200, res.content
    return res.json()


def _slot(booking, start):
    BookingSlotRange.objects.create(booking=booking, start_datetime=start, end_datetime=start + timedelta(hours=1))
    return booking


@pytest.fixture
def world():
    dept = _department()
    xrd = _equipment("Xenon Ray Diffractometer", dept)
    sem = _equipment("Scanning Electron Microscope", dept)
    me = UserFactory(admin_approved=True, user_type=UserType.STUDENT, name="Asha Rao")
    other = UserFactory(admin_approved=True, user_type=UserType.STUDENT, name="Other Person")
    now = timezone.now()
    soon = _slot(_booking(me, xrd, "IICXRD1"), now + timedelta(days=2))
    later = _slot(_booking(me, sem, "IICSEM2", status=BookingStatus.PENDING_PAYMENT), now + timedelta(days=5))
    _slot(_booking(me, sem, "IICSEM3", status=BookingStatus.CANCELLED), now + timedelta(days=1))
    _slot(_booking(me, xrd, "IICXRD4"), now - timedelta(days=3))  # already over
    done = _booking(me, xrd, "IICXRD5", status=BookingStatus.COMPLETED)
    BookingResultFile.objects.create(booking=done, file=ContentFile(b"x", name="r.txt"), original_name="r.txt")
    _slot(_booking(other, xrd, "IICXRD9"), now + timedelta(hours=5))
    return {"me": me, "other": other, "soon": soon, "later": later, "done": done, "xrd": xrd}


def test_only_the_users_own_upcoming_bookings_in_slot_order(world):
    body = _get(world["me"])
    assert [r["display_id"] for r in body["upcoming"]] == ["IICXRD1", "IICSEM2"]
    assert body["next"]["display_id"] == "IICXRD1"
    assert body["counts"]["upcoming"] == 2 and body["counts"]["awaiting_payment"] == 1
    assert "sample_deadline" in body["next"]


def test_new_results_until_viewed(world):
    body = _get(world["me"])
    assert body["results"]["new"] == 1
    assert body["results"]["recent"][0]["display_id"] == "IICXRD5" and body["results"]["recent"][0]["new"] is True
    BookingResultView.objects.create(booking=world["done"], user=world["me"])
    assert _get(world["me"])["results"]["new"] == 0


def test_spending_counts_charged_bookings_only(world):
    body = _get(world["me"])
    # IICXRD1, IICXRD4 (booked) and IICXRD5 (completed) at 10 each; payment pending and cancelled are not spent.
    assert body["spend"]["this_month"] == 30.0
    assert len(body["spend"]["months"]) == 6 and body["spend"]["months"][-1]["amount"] == 30.0
    assert body["hours"]["this_month"] == 3.0


def test_book_again_lists_most_booked_equipment(world):
    again = _get(world["me"])["book_again"]
    assert again[0]["id"] == world["xrd"].pk and again[0]["times"] == 3 and again[0]["available"] is True


def test_empty_for_a_new_user_and_signed_out_refused():
    body = _get(UserFactory(admin_approved=True, user_type=UserType.EXTERNAL))
    assert body["next"] is None and body["upcoming"] == [] and body["book_again"] == []
    assert body["spend"]["year"] == 0.0
    assert APIClient().get(URL).status_code in (401, 403)


def test_charges_use_the_booking_total(world):
    world["soon"].total_charge = Decimal("25.50")
    world["soon"].save(update_fields=["total_charge"])
    assert _get(world["me"])["spend"]["this_month"] == 45.5

"""The manual I-STEM FBR workflow is retired: the portal creates the I-STEM user and booking through the I-STEM API.

Users no longer submit an FBR number, Officers in Charge no longer verify one, and results are not held for it.
Stored FBR numbers and statuses on older bookings are kept (read-only).
"""

from types import SimpleNamespace

import pytest
from django.urls import Resolver404, resolve

from iic_booking.equipment.models import BookingStatus, IstemFbrStatus, initial_istem_fbr_fields_for_charge_profile
from iic_booking.equipment.results_sharing_views import _results_lock


def test_new_bookings_do_not_start_the_manual_fbr_workflow():
    assert initial_istem_fbr_fields_for_charge_profile(SimpleNamespace(require_istem_fbr=True)) == {}
    assert initial_istem_fbr_fields_for_charge_profile(None) == {}


@pytest.mark.parametrize("path", ["/api/bookings/1/istem-fbr/", "/api/bookings/1/istem-fbr/review/"])
def test_fbr_submit_and_review_endpoints_are_gone(path):
    with pytest.raises(Resolver404):
        resolve(path)


@pytest.mark.parametrize("fbr_status", [IstemFbrStatus.PENDING_FBR, IstemFbrStatus.PENDING_OIC, IstemFbrStatus.INVALID])
def test_results_are_not_held_for_an_unverified_fbr(fbr_status):
    booking = SimpleNamespace(
        status=BookingStatus.COMPLETED,
        equipment=SimpleNamespace(user_rating_enabled=True),
        rating=5,
        rating_removed=False,
        istem_fbr_status=fbr_status,
    )
    assert _results_lock(booking) == (None, None)

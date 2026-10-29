"""External booking confirmations say that I-STEM's timing is for record only and the portal's timing prevails."""

from __future__ import annotations

import pytest

from iic_booking.equipment.booking_events import ISTEM_TIMING_NOTE_PLAIN, apply_istem_timing_note_to_context
from iic_booking.users.models.user_type import UserType

from .conftest import make_user

pytestmark = pytest.mark.django_db


def test_external_users_get_the_note_once():
    user = make_user(UserType.EXTERNAL)
    context = {"comment": "Bring the sample by 10:00."}
    apply_istem_timing_note_to_context(context, user, also_append_to_comment=True)
    apply_istem_timing_note_to_context(context, user, also_append_to_comment=True)
    assert context["istem_timing_note"] == ISTEM_TIMING_NOTE_PLAIN
    assert context["comment"].count("for record purposes only") == 1
    assert context["comment"].startswith("Bring the sample by 10:00.")


def test_internal_users_get_no_note():
    user = make_user(UserType.FACULTY)
    context = {"comment": ""}
    apply_istem_timing_note_to_context(context, user, also_append_to_comment=True)
    assert context == {"comment": "", "istem_timing_note": ""}

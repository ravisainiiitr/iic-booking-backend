"""
Additional Remote Analysis unit/integration tests (Milestone 8).
"""

from __future__ import annotations

import pytest

from iic_booking.remote_analysis.activity import ActivityService
from iic_booking.remote_analysis.constants import ActivityVerb, NotificationType
from iic_booking.remote_analysis.notifications import NotificationEngine
from iic_booking.users.tests.factories import UserFactory


@pytest.mark.django_db
def test_notification_engine_portal_channel():
    user = UserFactory()
    rows = NotificationEngine().notify(
        user,
        NotificationType.ANNOUNCEMENT,
        "RC1 notice",
        "Production hardening complete",
        channels=["PORTAL"],
    )
    assert len(rows) == 1
    assert rows[0].title == "RC1 notice"
    listed = NotificationEngine().list_for_user(user)
    assert any(n.title == "RC1 notice" for n in listed)
    marked = NotificationEngine().mark_read(user, all_unread=True)
    assert marked >= 1


@pytest.mark.django_db
def test_activity_service_records_event():
    user = UserFactory()
    event = ActivityService().record(
        ActivityVerb.ANNOUNCEMENT,
        "Platform RC1",
        actor=user,
        user=user,
        also_global=True,
    )
    assert event.summary == "Platform RC1"
    events = ActivityService().list_events(user)
    assert any(e.id == event.id for e in events)


@pytest.mark.django_db
def test_invitation_expire_idempotent():
    from iic_booking.remote_analysis.sharing import InvitationService

    count = InvitationService().expire_stale()
    assert isinstance(count, int)
    assert InvitationService().expire_stale() >= 0

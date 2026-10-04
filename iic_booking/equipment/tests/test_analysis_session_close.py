"""Tests for one-shot remote analysis close after live desktop session."""

from __future__ import annotations

import pytest
from django.utils import timezone

from iic_booking.equipment.remote_analysis_integration.session_close import (
    LIVE_DESKTOP_STATUSES,
    ensure_analysis_closed_from_history,
    maybe_close_booking_analysis_after_session,
    session_reached_live_desktop,
)
from iic_booking.remote_analysis.constants import SessionStatus


class _FakeSession:
    def __init__(self, *, status, connected_at=None, launch_time=None, booking=None, pk="s1"):
        self.status = status
        self.connected_at = connected_at
        self.launch_time = launch_time
        self.booking = booking
        self.pk = pk
        self.booking_id = getattr(booking, "pk", None)


class _FakeEquipment:
    def __init__(self, *, test_only=False):
        self.visible_to_test_accounts_only = test_only


class _FakeBooking:
    def __init__(self, *, test_only=False):
        self.pk = 99
        self.analysis_closed_at = None
        self.saved = False
        self.equipment = _FakeEquipment(test_only=test_only)

    def save(self, update_fields=None):
        self.saved = True


def test_session_reached_live_desktop_via_connected_at():
    assert session_reached_live_desktop(
        _FakeSession(status=SessionStatus.PREPARING, connected_at=timezone.now())
    )


def test_session_reached_live_desktop_via_status():
    for status in LIVE_DESKTOP_STATUSES:
        assert session_reached_live_desktop(_FakeSession(status=status))


def test_preparing_without_live_markers_is_not_live(monkeypatch):
    class _EmptyQS:
        def exists(self):
            return False

    class _HistMgr:
        def filter(self, **kwargs):
            return _EmptyQS()

    class _HistModel:
        objects = _HistMgr()

    monkeypatch.setattr(
        "iic_booking.remote_analysis.session_models.SessionStateHistory",
        _HistModel,
    )
    assert not session_reached_live_desktop(_FakeSession(status=SessionStatus.PREPARING))


def test_maybe_close_skips_failed():
    booking = _FakeBooking()
    session = _FakeSession(status=SessionStatus.ACTIVE, connected_at=timezone.now(), booking=booking)
    assert maybe_close_booking_analysis_after_session(session, final_status=SessionStatus.FAILED) is False
    assert booking.analysis_closed_at is None


def test_maybe_close_sets_flag_after_live_terminate():
    booking = _FakeBooking()
    session = _FakeSession(status=SessionStatus.ACTIVE, connected_at=timezone.now(), booking=booking)
    assert maybe_close_booking_analysis_after_session(session, final_status=SessionStatus.TERMINATED) is True
    assert booking.analysis_closed_at is not None
    assert booking.saved is True


def test_maybe_close_keeps_test_only_equipment_open():
    booking = _FakeBooking(test_only=True)
    session = _FakeSession(status=SessionStatus.ACTIVE, connected_at=timezone.now(), booking=booking)
    assert maybe_close_booking_analysis_after_session(session, final_status=SessionStatus.TERMINATED) is False
    assert booking.analysis_closed_at is None
    assert booking.saved is False


def test_history_close_skips_test_only_equipment(monkeypatch):
    def _fail(*args, **kwargs):
        raise AssertionError("history must not be queried for test-only equipment")

    monkeypatch.setattr(
        "iic_booking.remote_analysis.session_models.RemoteDesktopSession.objects.filter",
        _fail,
    )
    booking = _FakeBooking(test_only=True)
    assert ensure_analysis_closed_from_history(booking) is False
    assert booking.analysis_closed_at is None

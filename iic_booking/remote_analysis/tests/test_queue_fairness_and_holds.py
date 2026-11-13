"""Queue fairness, hold windows and ready notices (RAA end-to-end review, 13 Nov 2026)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.utils import timezone

from iic_booking.equipment.models import Booking, BookingStatus, ChargeProfile, Equipment
from iic_booking.remote_analysis.constants import (
    QueueEntryStatus,
    ReservationStatus,
    SessionStatus,
    WorkstationStatus,
)
from iic_booking.remote_analysis.models import AnalysisWorkstation, InstalledSoftware
from iic_booking.remote_analysis.scheduler_models import AnalysisReservation, ReservationQueue
from iic_booking.remote_analysis.services import queue_insight
from iic_booking.remote_analysis.services.checkin import CheckinService
from iic_booking.remote_analysis.services.scheduler import SchedulerService
from iic_booking.remote_analysis.services.tokens import issue_agent_token
from iic_booking.remote_analysis.session_models import RemoteDesktopSession
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

_seq = iter(range(10_000))


def _ws(software: list[str], *, status=WorkstationStatus.AVAILABLE, fresh=True) -> AnalysisWorkstation:
    n = next(_seq)
    ws = AnalysisWorkstation.objects.create(
        agent_id=f"qf-ws-{n}",
        hostname=f"QF-PC-{n}",
        status=status,
        enabled=True,
        health_score=100,
        last_heartbeat=timezone.now() if fresh else timezone.now() - timedelta(hours=2),
        supports_rdp=True,
        last_inventory_update=timezone.now(),
        memory_gb=32,
        cpu_cores=8,
        storage_gb=500,
    )
    issue_agent_token(ws)
    for name in software:
        InstalledSoftware.objects.create(workstation=ws, software_name=name, is_present=True, allocation_enabled=True)
    return ws


def _booking(user=None) -> Booking:
    n = next(_seq)
    equipment = Equipment.objects.create(code=f"QF-EQ-{n}", name=f"QF Equipment {n}", enable_remote_analysis=True)
    profile = ChargeProfile.objects.create(
        equipment=equipment, user_type=UserType.STUDENT, primary_unit_charge=Decimal("10.00")
    )
    return Booking.objects.create(
        user=user or UserFactory(),
        equipment=equipment,
        charge_profile=profile,
        status=BookingStatus.COMPLETED,
        analysis_available=True,
        virtual_booking_id=f"QF{n:06d}",
        total_time_minutes=60,
        total_charge=Decimal("10.00"),
    )


def _reservation(*, software=(), status=ReservationStatus.QUEUED, created_ago=timedelta(0), window=timedelta(hours=72),
                 workstation=None, booking=None) -> AnalysisReservation:
    booking = booking or _booking()
    start = timezone.now() - created_ago
    return AnalysisReservation.objects.create(
        user=booking.user,
        booking=booking,
        workstation=workstation,
        status=status,
        requested_start=start,
        requested_end=start + window,
        priority=100,
        requested_capabilities={"required_software_names": list(software)} if software else {},
    )


def _enqueue(reservation, *, ago: timedelta) -> ReservationQueue:
    entry = ReservationQueue.objects.create(reservation=reservation, status=QueueEntryStatus.WAITING, priority=100)
    ReservationQueue.objects.filter(pk=entry.pk).update(enqueued_at=timezone.now() - ago)
    entry.refresh_from_db()
    return entry


def _open_session(reservation, ws, *, ends_in: timedelta) -> RemoteDesktopSession:
    return RemoteDesktopSession.objects.create(
        reservation=reservation,
        booking=reservation.booking,
        user=reservation.user,
        workstation=ws,
        status=SessionStatus.ACTIVE,
        expires_at=timezone.now() + ends_in,
    )


# --- queue expiry ---------------------------------------------------------------------------------


def test_request_waiting_longer_than_30_minutes_keeps_its_place():
    reservation = _reservation(created_ago=timedelta(hours=2))
    _enqueue(reservation, ago=timedelta(hours=2))
    SchedulerService().expire_stale()
    reservation.refresh_from_db()
    assert reservation.status == ReservationStatus.QUEUED
    assert reservation.queue_entry.status == QueueEntryStatus.WAITING


def test_request_expires_when_its_access_window_ends_and_the_user_is_told():
    reservation = _reservation(created_ago=timedelta(hours=73), window=timedelta(hours=72))
    _enqueue(reservation, ago=timedelta(hours=73))
    with mock.patch("iic_booking.remote_analysis.services.ready_notice.notify_queue_expired") as notice:
        SchedulerService().expire_stale()
    reservation.refresh_from_db()
    assert reservation.status == ReservationStatus.EXPIRED
    notice.assert_called_once()


def test_hold_for_a_request_that_waited_starts_at_allocation_not_at_request_time():
    ws = _ws(["Origin"])
    reservation = _reservation(software=["Origin"], created_ago=timedelta(hours=1))
    _enqueue(reservation, ago=timedelta(hours=1))
    SchedulerService().process_queue()
    reservation.refresh_from_db()
    assert reservation.status == ReservationStatus.AWAITING_CHECKIN
    assert reservation.workstation_id == ws.id
    assert reservation.reserved_start >= timezone.now() - timedelta(minutes=1)


def test_checked_in_hold_with_a_desktop_being_prepared_is_not_a_missed_start():
    ws = _ws(["Origin"], status=WorkstationStatus.BUSY)
    reservation = _reservation(status=ReservationStatus.RESERVED, created_ago=timedelta(hours=1), workstation=ws)
    AnalysisReservation.objects.filter(pk=reservation.pk).update(reserved_start=timezone.now() - timedelta(hours=1))
    session = _open_session(reservation, ws, ends_in=timedelta(minutes=30))
    RemoteDesktopSession.objects.filter(pk=session.pk).update(status=SessionStatus.PREPARING)
    SchedulerService().expire_stale()
    reservation.refresh_from_db()
    assert reservation.status == ReservationStatus.RESERVED


# --- check-in holds -------------------------------------------------------------------------------


def test_releasing_a_held_pc_leaves_the_queue_instead_of_rejoining_it():
    ws = _ws(["Origin"], status=WorkstationStatus.RESERVED)
    reservation = _reservation(status=ReservationStatus.RESERVED, workstation=ws)
    _enqueue(reservation, ago=timedelta(minutes=5))
    CheckinService().open_checkin_window(reservation)
    with mock.patch("iic_booking.remote_analysis.services.ready_notice.released") as released:
        result = CheckinService().release_checkin(reservation, reason="Released by user")
    reservation.refresh_from_db()
    ws.refresh_from_db()
    assert result["action"] == "released"
    assert reservation.status == ReservationStatus.CANCELLED
    assert reservation.queue_entry.status == QueueEntryStatus.CANCELLED
    assert ws.status == WorkstationStatus.AVAILABLE
    released.assert_called_once()


def test_missed_check_ins_stop_after_the_limit_under_the_default_policy():
    ws = _ws(["Origin"], status=WorkstationStatus.RESERVED)
    reservation = _reservation(status=ReservationStatus.RESERVED, workstation=ws)
    _enqueue(reservation, ago=timedelta(minutes=5))
    AnalysisReservation.objects.filter(pk=reservation.pk).update(missed_checkin_count=2)
    reservation.refresh_from_db()
    CheckinService().open_checkin_window(reservation)
    AnalysisReservation.objects.filter(pk=reservation.pk).update(checkin_expires_at=timezone.now() - timedelta(seconds=1))
    CheckinService().expire_due()
    reservation.refresh_from_db()
    assert reservation.status == ReservationStatus.CANCELLED
    assert reservation.queue_entry.status == QueueEntryStatus.CANCELLED


def test_opening_a_hold_sends_the_ready_notice_to_bell_and_whatsapp():
    ws = _ws(["Origin"], status=WorkstationStatus.RESERVED)
    reservation = _reservation(status=ReservationStatus.RESERVED, workstation=ws)
    with mock.patch("iic_booking.communication.in_app.notify_in_app") as bell, mock.patch(
        "iic_booking.whatsapp.hooks.remote_analysis_ready"
    ) as wa:
        CheckinService().open_checkin_window(reservation)
    bell.assert_called_once()
    kwargs = bell.call_args.kwargs
    assert kwargs["event"] == "remote_analysis_ready"
    assert kwargs["link"] == f"/analysis-workspace/{reservation.booking.pk}"
    assert "Join session" in kwargs["message"]
    wa.assert_called_once()
    assert wa.call_args.args[1] == 10


def test_whatsapp_ready_hook_sends_the_template_values(monkeypatch):
    from iic_booking.whatsapp import conf, hooks

    monkeypatch.setattr(conf, "enabled", lambda: True)
    reservation = _reservation(status=ReservationStatus.AWAITING_CHECKIN)
    reservation.checkin_notified_at = timezone.now()
    with mock.patch("iic_booking.whatsapp.service.notify") as notify:
        hooks.remote_analysis_ready(reservation, 10)
    args, kwargs = notify.call_args
    assert args[1] == "remote_analysis_ready"
    assert args[2][1:] == [reservation.booking.virtual_booking_id, reservation.booking.equipment.name, "10"]
    assert kwargs["event_key"].startswith(f"ra:{reservation.pk}:ready:")


def test_booking_completion_no_longer_holds_a_pc_by_itself():
    from iic_booking.equipment.remote_analysis_integration.eligibility import EligibilityResult
    from iic_booking.equipment.remote_analysis_integration.service import BookingRemoteAnalysisService

    _ws([])
    booking = _booking()
    svc = BookingRemoteAnalysisService()
    with mock.patch.object(svc.eligibility, "evaluate", return_value=EligibilityResult(True, "ok")):
        result = svc.on_booking_completed(booking)
    assert result["eligible"] is True
    assert not AnalysisReservation.objects.filter(booking=booking).exists()


# --- queue insight ----------------------------------------------------------------------------------


def test_position_counts_only_requests_competing_for_the_same_pcs():
    _ws(["Origin"], status=WorkstationStatus.BUSY)
    _ws(["MATLAB"], status=WorkstationStatus.BUSY)
    other_software = _reservation(software=["MATLAB"])
    _enqueue(other_software, ago=timedelta(minutes=20))
    same_software = _reservation(software=["Origin"])
    _enqueue(same_software, ago=timedelta(minutes=10))
    mine = _reservation(software=["Origin"])
    _enqueue(mine, ago=timedelta(minutes=1))

    info = queue_insight.insight(mine, avg_session_minutes=30)
    assert info["position"] == 2
    assert info["people_ahead"] == 1
    assert info["queue_size"] == 2
    assert info["reason_code"] == "ahead_in_queue"


def test_wait_estimate_uses_when_the_compatible_pc_frees_up():
    ws = _ws(["Origin"], status=WorkstationStatus.BUSY)
    running = _reservation(software=["Origin"], status=ReservationStatus.ACTIVE, workstation=ws)
    _open_session(running, ws, ends_in=timedelta(minutes=12))
    mine = _reservation(software=["Origin"])
    _enqueue(mine, ago=timedelta(minutes=1))

    info = queue_insight.insight(mine, avg_session_minutes=30)
    assert info["position"] == 1
    assert info["reason_code"] == "pcs_busy"
    assert info["estimated_wait_minutes"] == 15  # 12 min rounded up to the next 5


def test_offline_pc_gives_an_honest_reason_and_no_fake_estimate():
    _ws(["Origin"], fresh=False)
    mine = _reservation(software=["Origin"])
    _enqueue(mine, ago=timedelta(minutes=1))
    info = queue_insight.insight(mine, avg_session_minutes=30)
    assert info["reason_code"] == "pcs_offline"
    assert info["estimated_wait_minutes"] is None
    assert "not connected" in info["reason"]


def test_no_pc_with_the_software():
    _ws(["MATLAB"])
    mine = _reservation(software=["Origin"])
    _enqueue(mine, ago=timedelta(minutes=1))
    assert queue_insight.insight(mine, avg_session_minutes=30)["reason_code"] == "no_compatible_pc"


def test_estimate_start_hands_each_person_ahead_the_next_free_pc():
    now = timezone.now()
    avg = timedelta(minutes=30)
    free = [now + timedelta(minutes=10), now + timedelta(minutes=20)]
    assert queue_insight.estimate_start(free, 0, avg) == now + timedelta(minutes=10)
    assert queue_insight.estimate_start(free, 1, avg) == now + timedelta(minutes=20)
    assert queue_insight.estimate_start(free, 2, avg) == now + timedelta(minutes=40)
    assert queue_insight.estimate_start([], 0, avg) is None


def test_only_a_compatible_waiting_request_blocks_extension():
    ws = _ws(["Origin"], status=WorkstationStatus.BUSY)
    _ws(["MATLAB"], status=WorkstationStatus.BUSY)
    running = _reservation(software=["Origin"], status=ReservationStatus.ACTIVE, workstation=ws)
    waiting_other = _reservation(software=["MATLAB"])
    _enqueue(waiting_other, ago=timedelta(minutes=3))
    assert queue_insight.someone_waiting_for(ws.id, exclude_reservation_id=running.id) is False

    waiting_same = _reservation(software=["Origin"])
    _enqueue(waiting_same, ago=timedelta(minutes=1))
    assert queue_insight.someone_waiting_for(ws.id, exclude_reservation_id=running.id) is True


def test_launch_uses_the_reservation_holding_the_open_desktop_not_a_newer_queued_one():
    from iic_booking.equipment.remote_analysis_integration.eligibility import EligibilityResult
    from iic_booking.equipment.remote_analysis_integration.service import BookingRemoteAnalysisService

    ws = _ws(["Origin"], status=WorkstationStatus.BUSY)
    booking = _booking()
    live = _reservation(software=["Origin"], status=ReservationStatus.READY, workstation=ws, booking=booking)
    _open_session(live, ws, ends_in=timedelta(minutes=20))
    newer = _reservation(software=["Origin"], status=ReservationStatus.QUEUED, booking=booking)
    booking.analysis_reservation = newer
    booking.save(update_fields=["analysis_reservation"])

    svc = BookingRemoteAnalysisService()
    with mock.patch.object(svc.eligibility, "evaluate", return_value=EligibilityResult(True, "ok")):
        chosen = svc.ensure_reservation(booking)
    booking.refresh_from_db()
    assert chosen.pk == live.pk
    assert booking.analysis_reservation_id == live.pk

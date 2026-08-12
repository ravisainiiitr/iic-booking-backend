"""R11 allocation + inventory safety: multi-PC, stale inventory, delta sync."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from iic_booking.remote_analysis.constants import WorkstationStatus
from iic_booking.remote_analysis.models import AnalysisWorkstation, InstalledSoftware
from iic_booking.remote_analysis.services.allocation import AllocationService
from iic_booking.remote_analysis.services.availability import AvailabilityEngine
from iic_booking.remote_analysis.services.inventory import InventoryService


def _eligible_ws(*, agent_id: str, hostname: str, status=WorkstationStatus.AVAILABLE):
    from iic_booking.remote_analysis.services.tokens import issue_agent_token

    ws = AnalysisWorkstation.objects.create(
        agent_id=agent_id,
        hostname=hostname,
        display_name=hostname,
        status=status,
        enabled=True,
        health_score=95,
        last_heartbeat=timezone.now(),
        last_inventory_update=timezone.now(),
        supports_rdp=True,
        memory_gb=32,
        cpu_cores=8,
        storage_gb=500,
    )
    issue_agent_token(ws)
    return ws


@pytest.mark.django_db
def test_r11_busy_pc_selects_next_compatible_pc():
    busy = _eligible_ws(agent_id="r11-busy", hostname="RAA-1", status=WorkstationStatus.BUSY)
    free = _eligible_ws(agent_id="r11-free", hostname="RAA-2", status=WorkstationStatus.AVAILABLE)
    for ws in (busy, free):
        InstalledSoftware.objects.create(
            workstation=ws,
            software_name="OriginPro",
            version="2024",
            is_present=True,
            allocation_enabled=True,
        )

    start = timezone.now()
    end = start + timedelta(hours=1)
    ranked = AllocationService().rank_candidates(
        start,
        end,
        required_software_names=["OriginPro"],
        requested_capabilities={"required_software_names": ["OriginPro"]},
        include_unavailable=True,
    )
    available = [c for c in ranked if c.available]
    assert available, (
        "expected at least one available candidate; "
        + "; ".join(f"{c.workstation.hostname}:{c.reasons}" for c in ranked)
    )
    assert available[0].workstation.id == free.id
    busy_scores = [c for c in ranked if c.workstation.id == busy.id]
    assert busy_scores
    assert busy_scores[0].available is False

    best = AllocationService().rank_candidates(
        start,
        end,
        required_software_names=["OriginPro"],
        requested_capabilities={"required_software_names": ["OriginPro"]},
    )
    assert best and best[0].workstation.id == free.id


@pytest.mark.django_db
def test_allocate_skips_busy_pc_to_next_with_same_software(ra_user):
    """Scheduler must pick the next free RAA with the mapped software — not queue early."""
    from iic_booking.remote_analysis.constants import ReservationStatus
    from iic_booking.remote_analysis.services.reservation import ReservationService
    from iic_booking.remote_analysis.services.scheduler import SchedulerService

    busy = _eligible_ws(agent_id="r11-alloc-busy", hostname="RAA-BUSY", status=WorkstationStatus.BUSY)
    free = _eligible_ws(agent_id="r11-alloc-free", hostname="RAA-FREE", status=WorkstationStatus.AVAILABLE)
    other = _eligible_ws(agent_id="r11-alloc-other", hostname="RAA-OTHER", status=WorkstationStatus.AVAILABLE)
    for ws in (busy, free):
        InstalledSoftware.objects.create(
            workstation=ws,
            software_name="Altium Designer 26",
            version="26",
            is_present=True,
            allocation_enabled=True,
        )
    # Wrong software on an otherwise free PC — must not be chosen.
    InstalledSoftware.objects.create(
        workstation=other,
        software_name="HighScore Plus",
        version="1",
        is_present=True,
        allocation_enabled=True,
    )

    start = timezone.now()
    end = start + timedelta(hours=1)
    reservation = ReservationService().create_reservation(
        user=ra_user,
        requested_start=start,
        requested_end=end,
        created_by=ra_user,
        auto_allocate=False,
        requested_capabilities={"required_software_names": ["Altium Designer 26"]},
    )
    out = SchedulerService().allocate(reservation, actor=ra_user)
    out.refresh_from_db()
    assert out.status == ReservationStatus.AWAITING_CHECKIN
    assert out.workstation_id == free.id
    free.refresh_from_db()
    assert free.status in {WorkstationStatus.BUSY, WorkstationStatus.RESERVED}


@pytest.mark.django_db
def test_allocate_queues_only_when_all_matching_pcs_busy(ra_user):
    """When every RAA with the mapped software is occupied, queue — do not steal wrong software."""
    from iic_booking.remote_analysis.constants import ReservationStatus, QueueEntryStatus
    from iic_booking.remote_analysis.scheduler_models import ReservationQueue
    from iic_booking.remote_analysis.services.reservation import ReservationService
    from iic_booking.remote_analysis.services.scheduler import SchedulerService

    a = _eligible_ws(agent_id="r11-all-busy-a", hostname="RAA-A", status=WorkstationStatus.BUSY)
    b = _eligible_ws(agent_id="r11-all-busy-b", hostname="RAA-B", status=WorkstationStatus.RESERVED)
    free_wrong = _eligible_ws(
        agent_id="r11-all-busy-wrong", hostname="RAA-WRONG", status=WorkstationStatus.AVAILABLE
    )
    for ws in (a, b):
        InstalledSoftware.objects.create(
            workstation=ws,
            software_name="Altium Designer 26",
            version="26",
            is_present=True,
            allocation_enabled=True,
        )
    InstalledSoftware.objects.create(
        workstation=free_wrong,
        software_name="OriginPro",
        version="2024",
        is_present=True,
        allocation_enabled=True,
    )

    start = timezone.now()
    end = start + timedelta(hours=1)
    reservation = ReservationService().create_reservation(
        user=ra_user,
        requested_start=start,
        requested_end=end,
        created_by=ra_user,
        auto_allocate=False,
        requested_capabilities={"required_software_names": ["Altium Designer 26"]},
    )
    out = SchedulerService().allocate(reservation, actor=ra_user)
    out.refresh_from_db()
    assert out.status == ReservationStatus.QUEUED
    assert out.workstation_id is None
    assert ReservationQueue.objects.filter(
        reservation=out, status=QueueEntryStatus.WAITING
    ).exists()


@pytest.mark.django_db
def test_process_queue_allocates_when_matching_pc_becomes_free(ra_user):
    """Queued request is allocated once a matching RAA returns to AVAILABLE."""
    from iic_booking.remote_analysis.constants import ReservationStatus, QueueEntryStatus
    from iic_booking.remote_analysis.scheduler_models import ReservationQueue
    from iic_booking.remote_analysis.services.reservation import ReservationService
    from iic_booking.remote_analysis.services.scheduler import SchedulerService

    busy = _eligible_ws(agent_id="r11-q-busy", hostname="RAA-Q1", status=WorkstationStatus.BUSY)
    later = _eligible_ws(agent_id="r11-q-later", hostname="RAA-Q2", status=WorkstationStatus.BUSY)
    for ws in (busy, later):
        InstalledSoftware.objects.create(
            workstation=ws,
            software_name="Altium Designer 26",
            version="26",
            is_present=True,
            allocation_enabled=True,
        )

    start = timezone.now()
    end = start + timedelta(hours=1)
    reservation = ReservationService().create_reservation(
        user=ra_user,
        requested_start=start,
        requested_end=end,
        created_by=ra_user,
        auto_allocate=False,
        requested_capabilities={"required_software_names": ["Altium Designer 26"]},
    )
    out = SchedulerService().allocate(reservation, actor=ra_user)
    out.refresh_from_db()
    assert out.status == ReservationStatus.QUEUED

    later.status = WorkstationStatus.AVAILABLE
    later.save(update_fields=["status", "updated_at"])
    ReservationQueue.objects.filter(reservation=out).update(status=QueueEntryStatus.WAITING)

    stats = SchedulerService().process_queue(limit=5)
    out.refresh_from_db()
    assert stats["allocated"] >= 1
    assert out.status == ReservationStatus.AWAITING_CHECKIN
    assert out.workstation_id == later.id


@pytest.mark.django_db
def test_r11_stale_inventory_excluded():
    from iic_booking.remote_analysis.services.tokens import issue_agent_token

    ws = AnalysisWorkstation.objects.create(
        agent_id="r11-stale",
        hostname="STALE-PC",
        display_name="STALE-PC",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
        health_score=95,
        last_heartbeat=timezone.now(),
        last_inventory_update=timezone.now() - timedelta(hours=3),
        supports_rdp=True,
        memory_gb=32,
        cpu_cores=8,
        storage_gb=500,
    )
    issue_agent_token(ws)
    InstalledSoftware.objects.create(
        workstation=ws,
        software_name="Notepad",
        version="1",
        is_present=True,
        allocation_enabled=True,
    )
    start = timezone.now()
    end = start + timedelta(hours=1)
    result = AvailabilityEngine().evaluate(
        ws,
        start,
        end,
        requested_capabilities={"required_software_names": ["Notepad"]},
    )
    assert result.available is False
    assert any("stale" in r.lower() for r in result.reasons)


@pytest.mark.django_db
def test_r11_delta_does_not_wipe_unrelated_software():
    ws = AnalysisWorkstation.objects.create(
        agent_id="r11-delta",
        hostname="DELTA-PC",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
        last_heartbeat=timezone.now(),
    )
    InventoryService().synchronize(
        ws,
        {
            "syncMode": "full",
            "software": [
                {"displayName": "OriginPro", "version": "2024", "publisher": "OriginLab"},
                {"displayName": "Notepad", "version": "10", "publisher": "Microsoft"},
            ],
        },
    )
    InventoryService().synchronize(
        ws,
        {
            "syncMode": "delta",
            "added": [{"displayName": "HighScore", "version": "1", "publisher": "PANalytical"}],
            "updated": [],
            "removed": [],
            "software": [],
        },
    )
    names = set(
        InstalledSoftware.objects.filter(workstation=ws, is_present=True).values_list(
            "software_name", flat=True
        )
    )
    assert names == {"OriginPro", "Notepad", "HighScore"}


@pytest.mark.django_db
def test_r11_delta_removed_marks_absent():
    ws = AnalysisWorkstation.objects.create(
        agent_id="r11-delta-rm",
        hostname="DELTA-RM",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
    )
    InventoryService().synchronize(
        ws,
        {
            "syncMode": "full",
            "software": [
                {"displayName": "Notepad", "version": "10", "publisher": "Microsoft"},
                {"displayName": "ImageJ", "version": "1.54", "publisher": "NIH"},
            ],
        },
    )
    InventoryService().synchronize(
        ws,
        {
            "syncMode": "delta",
            "removed": [{"displayName": "Notepad", "version": "10", "publisher": "Microsoft"}],
        },
    )
    assert not InstalledSoftware.objects.filter(
        workstation=ws, software_name="Notepad", is_present=True
    ).exists()
    assert InstalledSoftware.objects.filter(
        workstation=ws, software_name="ImageJ", is_present=True
    ).exists()

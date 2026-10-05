"""Per-department Remote Analysis switch inside BookingAnalysisEligibilityService (one gate for every entry point)."""

import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from iic_booking.department_modules.constants import ModuleKey
from iic_booking.equipment.remote_analysis_integration.eligibility import BookingAnalysisEligibilityService

from .conftest import DAY, make_booking, make_equipment, switch

pytestmark = pytest.mark.django_db
RA = ModuleKey.REMOTE_ANALYSIS


def evaluate(booking):
    return BookingAnalysisEligibilityService().evaluate(booking)


@pytest.fixture
def ra(world):
    world.ra_equipment = make_equipment(world.dept, enable_remote_analysis=True)
    world.other_ra_equipment = make_equipment(world.other_dept, enable_remote_analysis=True)
    return world


def test_department_off_blocks_new_bookings_and_lets_earlier_ones_finish(ra):
    earlier = make_booking(ra.student, ra.ra_equipment, created_ago=DAY)
    assert evaluate(earlier).eligible

    switch(ra.admin, ra.dept, RA, enabled=False)
    later = make_booking(ra.student, ra.ra_equipment)
    result = evaluate(later)
    assert result.eligible is False and result.checks["department_enabled"] is False
    assert "switched off" in result.reason
    assert evaluate(earlier).eligible
    assert evaluate(make_booking(ra.student, ra.other_ra_equipment)).eligible  # other departments unaffected

    from iic_booking.equipment.remote_analysis_integration.service import BookingRemoteAnalysisService

    with pytest.raises(ValueError):
        BookingRemoteAnalysisService().ensure_reservation(later)

    switch(ra.admin, ra.dept, RA, enabled=True)
    assert evaluate(later).eligible


def test_active_reservation_keeps_working_after_switch_off(ra):
    from iic_booking.remote_analysis.constants import ReservationStatus
    from iic_booking.remote_analysis.scheduler_models import AnalysisReservation

    switch(ra.admin, ra.dept, RA, enabled=False)
    booking = make_booking(ra.student, ra.ra_equipment)
    assert not evaluate(booking).eligible
    now = timezone.now()
    AnalysisReservation.objects.create(
        booking=booking, user=ra.student, requested_start=now, requested_end=now + timedelta(hours=2),
        status=ReservationStatus.ACTIVE,
    )
    assert evaluate(booking).eligible


def test_test_users_only(ra):
    earlier = make_booking(ra.student, ra.ra_equipment, created_ago=DAY)
    switch(ra.admin, ra.dept, RA, test_users_only=True)
    real = make_booking(ra.student, ra.ra_equipment)
    test = make_booking(ra.test_student, ra.ra_equipment)
    result = evaluate(real)
    assert result.eligible is False and "test accounts" in result.reason
    assert evaluate(test).eligible
    assert evaluate(earlier).eligible


def workstation(department, name):
    from iic_booking.remote_analysis.constants import WorkstationStatus
    from iic_booking.remote_analysis.models import AnalysisWorkstation
    from iic_booking.remote_analysis.services.tokens import issue_agent_token

    now = timezone.now()
    ws = AnalysisWorkstation.objects.create(
        agent_id=f"dm-{name.lower()}-{uuid.uuid4().hex[:6]}",
        hostname=name,
        display_name=name,
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
        health_score=95,
        last_heartbeat=now,
        last_inventory_update=now,
        supports_rdp=True,
        memory_gb=32,
        cpu_cores=8,
        storage_gb=500,
        department=department,
    )
    issue_agent_token(ws)
    return ws


def reserve(user, actor, *, hours_from_now=0, auto_allocate=True):
    from iic_booking.remote_analysis.services.reservation import ReservationService

    start = timezone.now() + timedelta(hours=hours_from_now)
    return ReservationService().create_reservation(
        user=user,
        requested_start=start,
        requested_end=start + timedelta(hours=1),
        department=getattr(user, "department", None),
        created_by=actor,
        auto_allocate=auto_allocate,
    )


def test_bookingless_reservation_is_checked_against_the_workstation_department(ra):
    from iic_booking.remote_analysis.constants import ReservationStatus, WorkstationStatus
    from iic_booking.remote_analysis.models import AnalysisWorkstation
    from iic_booking.remote_analysis.services.scheduler import SchedulerService

    from .conftest import make_user

    chem_ws = workstation(ra.dept, "CHEM-RAA")
    workstation(ra.other_dept, "PHYS-RAA")
    physics_student = make_user(department=ra.other_dept)
    queued_earlier = reserve(physics_student, ra.admin, hours_from_now=6, auto_allocate=False)

    switch(ra.admin, ra.other_dept, RA, enabled=False)

    # Neither the user's nor the reservation's department decides: only the Chemistry workstation may be used.
    for i, user in enumerate((ra.student, physics_student)):
        reservation = reserve(user, ra.admin, hours_from_now=2 * i)
        assert reservation.workstation_id == chem_ws.id, reservation.allocation_notes
        AnalysisWorkstation.objects.filter(pk=chem_ws.pk).update(status=WorkstationStatus.AVAILABLE)

    switch(ra.admin, ra.dept, RA, enabled=False)
    blocked = reserve(ra.student, ra.admin, hours_from_now=10)
    assert blocked.workstation_id is None and blocked.status == ReservationStatus.QUEUED

    # A reservation created before the switch-off is still allocated (existing work finishes).
    allocated = SchedulerService().allocate(queued_earlier, actor=ra.admin)
    assert allocated.workstation_id is not None


def test_bookingless_reservation_test_users_only(ra):
    chem_ws = workstation(ra.dept, "CHEM-RAA")
    switch(ra.admin, ra.dept, RA, test_users_only=True)

    assert reserve(ra.student, ra.admin).workstation_id is None
    assert reserve(ra.test_student, ra.admin, hours_from_now=3).workstation_id == chem_ws.id


def test_manager_reservation_api_checks_the_workstation_department(ra):
    from .conftest import client_for, make_user

    workstation(ra.dept, "CHEM-RAA")
    workstation(ra.other_dept, "PHYS-RAA")
    switch(ra.admin, ra.dept, RA, enabled=False)
    now = timezone.now()
    body = {
        "requested_start": now.isoformat(),
        "requested_end": (now + timedelta(hours=1)).isoformat(),
        "auto_allocate": False,
    }
    api = client_for(ra.admin)

    # The user's own department (Chemistry) is off, but the Physics workstation can take the reservation.
    resp = api.post(
        "/api/v1/analysis/reservations/", {**body, "user_id": ra.student.id, "department_id": ra.dept.id}, format="json"
    )
    assert resp.status_code == 201, resp.data

    switch(ra.admin, ra.other_dept, RA, enabled=False)
    physics_student = make_user(department=ra.other_dept)
    for user in (ra.student, physics_student):
        resp = api.post("/api/v1/analysis/reservations/", {**body, "user_id": user.id}, format="json")
        assert resp.status_code == 403 and resp.data["code"] == "department_module_disabled"

    switch(ra.admin, ra.other_dept, RA, enabled=True, test_users_only=True)
    resp = api.post("/api/v1/analysis/reservations/", {**body, "user_id": ra.student.id}, format="json")
    assert resp.status_code == 403
    resp = api.post("/api/v1/analysis/reservations/", {**body, "user_id": ra.test_student.id}, format="json")
    assert resp.status_code == 201, resp.data

    # Booking-backed reservations still follow the booked equipment's department.
    later = make_booking(ra.student, ra.ra_equipment)
    resp = api.post("/api/v1/analysis/reservations/", {"booking_id": later.booking_id, "auto_allocate": False}, format="json")
    assert resp.status_code == 403 and resp.data["code"] == "department_module_disabled"


def test_equipment_flag_still_applies(ra):
    """The department switch only narrows: equipment without Remote Analysis stays ineligible when the department is on."""
    plain = make_equipment(ra.dept)
    switch(ra.admin, ra.dept, RA, enabled=True)
    result = evaluate(make_booking(ra.student, plain))
    assert result.eligible is False and result.checks["equipment_enabled"] is False

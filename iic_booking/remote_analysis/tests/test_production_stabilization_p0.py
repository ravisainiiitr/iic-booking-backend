"""Security/stabilization tests for S1, S2, S3, R1, R3."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.models import Booking, BookingStatus, ChargeProfile, Equipment
from iic_booking.remote_analysis.catalog_models import AnalysisSoftwareCatalog
from iic_booking.remote_analysis.constants import (
    ReservationStatus,
    WorkflowJobStatus,
    WorkspaceStatus,
    WorkstationStatus,
)
from iic_booking.remote_analysis.services.workflow_engine import WorkflowEngine, WorkflowEngineError
from iic_booking.remote_analysis.services.workspace_metadata import AnalysisWorkspaceMetadata
from iic_booking.remote_analysis.workflow_models import (
    AnalysisJob,
    AnalysisWorkflow,
    AnalysisWorkflowStep,
    AnalysisWorkflowVersion,
    EquipmentAnalysisWorkflow,
)
from iic_booking.users.models.user_type import UserType


def _user(email_prefix: str, user_type: str = "student"):
    User = get_user_model()
    return User.objects.create_user(
        email=f"{email_prefix}-{uuid.uuid4().hex[:6]}@example.com",
        password="pass12345",
        user_type=user_type,
    )


def _equipment():
    return Equipment.objects.create(
        name="PXRD",
        code=f"PX{uuid.uuid4().hex[:4].upper()}",
        enable_remote_analysis=True,
    )


def _booking(user, equipment):
    profile = ChargeProfile.objects.create(
        equipment=equipment,
        user_type=UserType.STUDENT,
        primary_unit_charge=Decimal("10.00"),
    )
    return Booking.objects.create(
        user=user,
        equipment=equipment,
        charge_profile=profile,
        status=BookingStatus.COMPLETED,
        analysis_available=True,
        total_time_minutes=60,
        total_charge=Decimal("10.00"),
    )


def _published_workflow(equipment, *, mapped: bool = True):
    catalog = AnalysisSoftwareCatalog.objects.create(
        name="OriginPro",
        slug=f"origin-{uuid.uuid4().hex[:8]}",
        is_active=True,
    )
    wf = AnalysisWorkflow.objects.create(
        name=f"WF {uuid.uuid4().hex[:6]}",
        slug=f"wf-{uuid.uuid4().hex[:8]}",
        is_active=True,
    )
    version = AnalysisWorkflowVersion.objects.create(
        workflow=wf,
        version_number=1,
        label="v1",
        is_published=True,
        published_at=timezone.now(),
    )
    AnalysisWorkflowStep.objects.create(
        version=version,
        step_number=1,
        software=catalog,
        mandatory=True,
        expected_output_folder="Step01",
    )
    if mapped:
        EquipmentAnalysisWorkflow.objects.create(equipment=equipment, workflow=wf, is_default=True)
    return wf, version


@pytest.mark.django_db
def test_s3_unmapped_workflow_rejected():
    eq = _equipment()
    wf, _ = _published_workflow(eq, mapped=False)
    engine = WorkflowEngine()
    with pytest.raises(WorkflowEngineError) as exc:
        engine.resolve_workflow(eq, workflow_id=str(wf.id), require_equipment_mapping=True)
    assert exc.value.code == "workflow_not_mapped"


@pytest.mark.django_db
def test_s3_mapped_workflow_ok():
    eq = _equipment()
    wf, version = _published_workflow(eq, mapped=True)
    resolved, ver, mapping = WorkflowEngine().resolve_workflow(
        eq, workflow_id=str(wf.id), require_equipment_mapping=True
    )
    assert resolved.id == wf.id
    assert ver.id == version.id
    assert mapping is not None


@pytest.mark.django_db
def test_s1_reservation_serialization_hides_hostname():
    from iic_booking.equipment.remote_analysis_integration.service import BookingRemoteAnalysisService
    from iic_booking.remote_analysis.models import AnalysisWorkstation
    from iic_booking.remote_analysis.scheduler_models import AnalysisReservation

    user = _user("owner")
    eq = _equipment()
    booking = _booking(user, eq)
    ws = AnalysisWorkstation.objects.create(
        agent_id=f"ag-{uuid.uuid4().hex[:8]}",
        hostname="SECRET-PC-01",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
        health_score=90,
        last_heartbeat=timezone.now(),
    )
    reservation = AnalysisReservation.objects.create(
        booking=booking,
        user=user,
        status=ReservationStatus.RESERVED,
        requested_start=timezone.now(),
        requested_end=timezone.now() + timedelta(hours=2),
        workstation=ws,
    )
    svc = BookingRemoteAnalysisService()
    public = svc._serialize_reservation(reservation, expose_infrastructure=False)
    assert "workstation" not in public
    assert "workstation_id" not in public
    assert public["allocated"] is True
    admin = svc._serialize_reservation(reservation, expose_infrastructure=True)
    assert admin["workstation"] == "SECRET-PC-01"


@pytest.mark.django_db
def test_s2_files_forbidden_for_other_student():
    owner = _user("owner")
    other = _user("other")
    eq = _equipment()
    booking = _booking(owner, eq)
    client = APIClient()
    client.force_authenticate(user=other)
    res = client.get(f"/api/v1/bookings/{booking.booking_id}/analysis/files/")
    assert res.status_code == 403
    assert res.data.get("code") == "files_forbidden"


@pytest.mark.django_db
def test_s2_files_allowed_for_owner():
    owner = _user("owner")
    eq = _equipment()
    booking = _booking(owner, eq)
    client = APIClient()
    client.force_authenticate(user=owner)
    res = client.get(f"/api/v1/bookings/{booking.booking_id}/analysis/files/")
    assert res.status_code == 200


@pytest.mark.django_db
def test_s2_faculty_same_dept_cannot_list_files():
    from iic_booking.users.models import Department

    dept = Department.objects.create(name=f"D-{uuid.uuid4().hex[:6]}", code=f"D{uuid.uuid4().hex[:3].upper()}")
    owner = _user("owner")
    owner.department = dept
    owner.save(update_fields=["department"])
    faculty = _user("fac", user_type="faculty")
    faculty.department = dept
    faculty.save(update_fields=["department"])
    eq = _equipment()
    booking = _booking(owner, eq)
    client = APIClient()
    client.force_authenticate(user=faculty)
    res = client.get(f"/api/v1/bookings/{booking.booking_id}/analysis/files/")
    assert res.status_code == 403


@pytest.mark.django_db
def test_r3_metadata_write_failure_does_not_raise(tmp_path):
    user = _user("owner")
    eq = _equipment()
    booking = _booking(user, eq)
    wf, version = _published_workflow(eq, mapped=True)
    from iic_booking.remote_analysis.scheduler_models import AnalysisReservation
    from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace

    reservation = AnalysisReservation.objects.create(
        booking=booking,
        user=user,
        status=ReservationStatus.RESERVED,
        requested_start=timezone.now(),
        requested_end=timezone.now() + timedelta(hours=1),
    )
    workspace = AnalysisWorkspace.objects.create(
        reservation=reservation,
        booking=booking,
        user=user,
        status=WorkspaceStatus.READY,
        storage_key=f"ws-{uuid.uuid4().hex[:12]}",
        quota_gb=10,
    )
    job = AnalysisJob.objects.create(
        booking=booking,
        workflow_version=version,
        workspace=workspace,
        reservation=reservation,
        owner=user,
        status=WorkflowJobStatus.ACTIVE,
    )
    meta = AnalysisWorkspaceMetadata()
    with patch.object(meta.storage, "ensure_folders", side_effect=OSError("disk full")):
        result = meta.write(job)
    assert result is None
    job.refresh_from_db()
    assert job.status == WorkflowJobStatus.ACTIVE
    assert "metadata_stale:" in (job.status_detail or "")


@pytest.mark.django_db
def test_r1_mandatory_names_from_version():
    eq = _equipment()
    wf, version = _published_workflow(eq, mapped=True)
    catalog2 = AnalysisSoftwareCatalog.objects.create(
        name="HighScore",
        slug=f"hs-{uuid.uuid4().hex[:8]}",
        is_active=True,
    )
    AnalysisWorkflowStep.objects.create(
        version=version,
        step_number=2,
        software=catalog2,
        mandatory=True,
        expected_output_folder="Step02",
    )
    names = WorkflowEngine().mandatory_software_names_for_version(version)
    assert "OriginPro" in names
    assert "HighScore" in names

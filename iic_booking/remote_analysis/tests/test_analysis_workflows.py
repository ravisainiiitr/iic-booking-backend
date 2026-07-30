"""Tests for Analysis Workflows / Analysis Jobs (pytest)."""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from iic_booking.equipment.models import Equipment
from iic_booking.remote_analysis.catalog_models import AnalysisSoftwareCatalog, EquipmentAnalysisSoftware
from iic_booking.remote_analysis.constants import WorkflowJobStatus
from iic_booking.remote_analysis.services.workflow_engine import WorkflowEngine
from iic_booking.remote_analysis.workflow_models import (
    AnalysisCapability,
    AnalysisWorkflow,
    AnalysisWorkflowStep,
    AnalysisWorkflowVersion,
    EquipmentAnalysisWorkflow,
)


@pytest.fixture
def workflow_bundle(db):
    catalog = AnalysisSoftwareCatalog.objects.create(
        name="HighScore Plus",
        slug=f"highscore-{uuid.uuid4().hex[:8]}",
        is_active=True,
    )
    catalog2 = AnalysisSoftwareCatalog.objects.create(
        name="OriginPro",
        slug=f"origin-{uuid.uuid4().hex[:8]}",
        is_active=True,
    )
    wf = AnalysisWorkflow.objects.create(
        name="PXRD Standard",
        slug=f"pxrd-{uuid.uuid4().hex[:8]}",
        description="Standard PXRD pipeline",
        estimated_duration_minutes=90,
        require_raw_data=True,
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
        title="HighScore",
        software=catalog,
        mandatory=True,
        estimated_duration_minutes=40,
        expected_output_folder="Step01",
        expected_outputs=["*.xy"],
        environment_label="HighScore Environment",
    )
    AnalysisWorkflowStep.objects.create(
        version=version,
        step_number=2,
        title="Origin",
        software=catalog2,
        mandatory=True,
        estimated_duration_minutes=50,
        expected_output_folder="Step02",
        expected_outputs=[],
        environment_label="OriginPro Environment",
    )
    eq = Equipment.objects.create(
        name="PXRD",
        code=f"PXRD{uuid.uuid4().hex[:4].upper()}",
        enable_remote_analysis=True,
    )
    EquipmentAnalysisWorkflow.objects.create(equipment=eq, workflow=wf, is_default=True)
    User = get_user_model()
    user = User.objects.create_user(
        email=f"wf-{uuid.uuid4().hex[:6]}@example.com",
        password="pass12345",
    )
    return {
        "wf": wf,
        "version": version,
        "catalog": catalog,
        "eq": eq,
        "user": user,
        "engine": WorkflowEngine(),
    }


@pytest.mark.django_db
def test_resolve_and_list_workflows(workflow_bundle):
    engine = workflow_bundle["engine"]
    eq = workflow_bundle["eq"]
    wf = workflow_bundle["wf"]
    resolved, version, mapping = engine.resolve_workflow(eq)
    assert resolved.id == wf.id
    assert version.is_published
    listed = engine.list_workflows_for_equipment(eq)
    assert len(listed) == 1
    assert listed[0]["name"] == "PXRD Standard"
    assert len(listed[0]["steps"]) == 2


@pytest.mark.django_db
def test_job_checkpoint_resume_and_review(workflow_bundle):
    from decimal import Decimal

    from iic_booking.equipment.models import Booking, BookingStatus, ChargeProfile
    from iic_booking.users.models.user_type import UserType

    engine = workflow_bundle["engine"]
    user = workflow_bundle["user"]
    eq = workflow_bundle["eq"]
    wf = workflow_bundle["wf"]
    profile = ChargeProfile.objects.create(
        equipment=eq,
        user_type=UserType.STUDENT,
        primary_unit_charge=Decimal("10.00"),
    )
    booking = Booking.objects.create(
        user=user,
        equipment=eq,
        charge_profile=profile,
        status=BookingStatus.COMPLETED,
        analysis_available=True,
        total_time_minutes=60,
        total_charge=Decimal("10.00"),
    )
    job = engine.start_job(booking, user=user, workflow_id=str(wf.id))
    assert job.steps.count() == 2
    engine.activate_step(job, 1)
    # Without a workspace, output verification is skipped — force path still advances.
    result = engine.complete_step(job, 1, force=True)
    assert result.get("advanced") is True
    job.refresh_from_db()
    assert job.current_step_number == 2
    engine.pause(job)
    job.refresh_from_db()
    assert job.status == WorkflowJobStatus.PAUSED
    engine.resume(job)
    job.refresh_from_db()
    assert job.current_step_number == 2
    assert job.status == WorkflowJobStatus.ACTIVE


@pytest.mark.django_db
def test_clone_and_capability_resolution(workflow_bundle):
    engine = workflow_bundle["engine"]
    wf = workflow_bundle["wf"]
    catalog = workflow_bundle["catalog"]
    user = workflow_bundle["user"]
    version = workflow_bundle["version"]
    clone = engine.clone_workflow(wf, name="PXRD Advanced", actor=user)
    assert clone.cloned_from_id == wf.id
    assert clone.published_version().steps.count() == 2
    cap = AnalysisCapability.objects.create(name="Peak Fitting", slug=f"peak-{uuid.uuid4().hex[:6]}")
    catalog.capabilities.add(cap)
    step = version.steps.get(step_number=1)
    step.software = None
    step.capability = cap
    step.save()
    resolved_catalog, _req, name = engine.resolve_step_software(step)
    assert resolved_catalog.id == catalog.id
    assert name == "HighScore Plus"


@pytest.mark.django_db
def test_legacy_single_step_shape():
    catalog = AnalysisSoftwareCatalog.objects.create(name="AZtec", slug=f"aztec-{uuid.uuid4().hex[:8]}")
    eq = Equipment.objects.create(name="SEM", code=f"SEM{uuid.uuid4().hex[:4].upper()}")
    EquipmentAnalysisSoftware.objects.create(equipment=eq, catalog=catalog, is_default=True)
    wf = AnalysisWorkflow.objects.create(name="SEM / AZtec", slug=f"sem-{uuid.uuid4().hex[:8]}")
    ver = AnalysisWorkflowVersion.objects.create(
        workflow=wf, version_number=1, label="v1", is_published=True, published_at=timezone.now()
    )
    AnalysisWorkflowStep.objects.create(
        version=ver, step_number=1, software=catalog, mandatory=True, expected_output_folder="Step01"
    )
    EquipmentAnalysisWorkflow.objects.create(equipment=eq, workflow=wf, is_default=True)
    listed = WorkflowEngine().list_workflows_for_equipment(eq)
    assert len(listed) == 1
    assert len(listed[0]["steps"]) == 1

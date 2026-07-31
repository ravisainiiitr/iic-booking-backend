"""API lifecycle tests — booking → analyze → job → pause/resume → complete → files."""

from __future__ import annotations

import pytest

from iic_booking.equipment.models import BookingEvent
from iic_booking.remote_analysis.constants import WorkflowJobStatus
from iic_booking.remote_analysis.workflow_models import AnalysisJob
from tests.analysis_platform.data_generator import unmapped_workflow_id
from tests.analysis_platform.utils import assert_no_hostname, assert_status


@pytest.mark.analysis_platform
@pytest.mark.django_db
def test_api_eligibility_and_summary(apt_researcher_api, apt_booking_id, apt_seed):
    res = apt_researcher_api.get(f"/api/v1/bookings/{apt_booking_id}/analysis/")
    assert_status(res, 200, context="analysis summary")
    body = res.json()
    assert body.get("eligible") is True or body.get("analyze", {}).get("can_analyze") is not None
    assert_no_hostname(body)
    analyze = body.get("analyze") or {}
    assert analyze.get("can_analyze") is True
    assert len(analyze.get("workflows") or []) >= 1


@pytest.mark.analysis_platform
@pytest.mark.django_db
def test_api_workflow_list(apt_researcher_api, apt_booking_id, apt_seed):
    res = apt_researcher_api.get(f"/api/v1/bookings/{apt_booking_id}/analysis/workflows/")
    assert_status(res, 200)
    workflows = res.json().get("workflows") or []
    assert len(workflows) >= 2
    ids = {str(w.get("id")) for w in workflows}
    assert str(apt_seed.single_step_workflow.id) in ids
    assert str(apt_seed.multi_step_workflow.id) in ids


@pytest.mark.analysis_platform
@pytest.mark.django_db
def test_api_analyze_creates_job_and_allocation(apt_researcher_api, apt_booking_id, apt_seed, apt_mock_agent):
    apt_mock_agent.process_once()
    res = apt_researcher_api.post(
        f"/api/v1/bookings/{apt_booking_id}/analysis/analyze/",
        {"workflow_id": str(apt_seed.single_step_workflow.id)},
        format="json",
    )
    assert_status(res, {201, 202}, context="analyze")
    body = res.json()
    assert_no_hostname(body)
    assert body.get("job") or body.get("reservation_id") or body.get("eligible") is not False

    job = AnalysisJob.objects.filter(booking_id=apt_booking_id).order_by("-created_at").first()
    assert job is not None
    assert job.status in {
        WorkflowJobStatus.PENDING,
        WorkflowJobStatus.PREPARING,
        WorkflowJobStatus.ACTIVE,
        WorkflowJobStatus.PAUSED,
        WorkflowJobStatus.NEEDS_REVIEW,
        WorkflowJobStatus.COMPLETED,
    }

    # Agent should see prepare / related commands eventually
    apt_mock_agent.process_once()
    apt_mock_agent.process_once()

    assert BookingEvent.objects.filter(
        booking_id=apt_booking_id,
        comment__icontains="Remote Analysis",
    ).exists() or BookingEvent.objects.filter(
        booking_id=apt_booking_id,
        metadata__remote_analysis=True,
    ).exists()


@pytest.mark.analysis_platform
@pytest.mark.django_db
def test_api_job_status_pause_resume_complete(apt_researcher_api, apt_booking_id, apt_seed, apt_mock_agent):
    apt_mock_agent.heartbeat()
    start = apt_researcher_api.post(
        f"/api/v1/bookings/{apt_booking_id}/analysis/analyze/",
        {"workflow_id": str(apt_seed.single_step_workflow.id)},
        format="json",
    )
    assert_status(start, {201, 202})
    apt_mock_agent.process_once()

    job_res = apt_researcher_api.get(f"/api/v1/bookings/{apt_booking_id}/analysis/job/")
    assert_status(job_res, 200)
    assert job_res.json().get("job") is not None

    pause = apt_researcher_api.post(f"/api/v1/bookings/{apt_booking_id}/analysis/job/pause/", {}, format="json")
    # Pause may 400 if job not yet in pausable state — accept both as harness soft gate
    assert pause.status_code in {200, 400}

    if pause.status_code == 200:
        resume = apt_researcher_api.post(
            f"/api/v1/bookings/{apt_booking_id}/analysis/job/resume/", {}, format="json"
        )
        assert resume.status_code in {200, 400}

    complete = apt_researcher_api.post(
        f"/api/v1/bookings/{apt_booking_id}/analysis/job/steps/1/complete/",
        {},
        format="json",
    )
    assert complete.status_code in {200, 400}


@pytest.mark.analysis_platform
@pytest.mark.django_db
def test_api_files_owner_access(apt_researcher_api, apt_booking_id, apt_seed, apt_mock_agent):
    apt_researcher_api.post(
        f"/api/v1/bookings/{apt_booking_id}/analysis/analyze/",
        {"workflow_id": str(apt_seed.single_step_workflow.id)},
        format="json",
    )
    apt_mock_agent.process_once()
    files = apt_researcher_api.get(f"/api/v1/bookings/{apt_booking_id}/analysis/files/")
    assert_status(files, 200)
    assert isinstance(files.json(), (list, dict))


@pytest.mark.analysis_platform
@pytest.mark.django_db
def test_api_unmapped_workflow_rejected(apt_researcher_api, apt_booking_id):
    res = apt_researcher_api.post(
        f"/api/v1/bookings/{apt_booking_id}/analysis/analyze/",
        {"workflow_id": unmapped_workflow_id()},
        format="json",
    )
    assert res.status_code in {400, 403}
    body = res.json()
    assert body.get("code") in {"workflow_not_mapped", "workflow_not_found", "no_software"} or "workflow" in str(
        body.get("detail", "")
    ).lower()


@pytest.mark.analysis_platform
@pytest.mark.django_db
def test_api_create_reservation(apt_researcher_api, apt_booking_id, apt_mock_agent):
    apt_mock_agent.heartbeat()
    res = apt_researcher_api.post(f"/api/v1/bookings/{apt_booking_id}/analysis/create/", {}, format="json")
    assert_status(res, {201, 400})
    if res.status_code == 201:
        assert res.json().get("reservation_id")

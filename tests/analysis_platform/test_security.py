"""Security tests — authorization boundaries for Analysis Platform."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from tests.analysis_platform.data_generator import unmapped_workflow_id
from tests.analysis_platform.utils import assert_status


@pytest.mark.analysis_platform
@pytest.mark.django_db
def test_sec_other_user_cannot_open_booking(apt_other_api, apt_booking_id):
    res = apt_other_api.get(f"/api/v1/bookings/{apt_booking_id}/analysis/")
    assert_status(res, 403, context="other user summary")


@pytest.mark.analysis_platform
@pytest.mark.django_db
def test_sec_other_user_cannot_analyze(apt_other_api, apt_booking_id, apt_seed):
    res = apt_other_api.post(
        f"/api/v1/bookings/{apt_booking_id}/analysis/analyze/",
        {"workflow_id": str(apt_seed.single_step_workflow.id)},
        format="json",
    )
    assert_status(res, 403)
    assert res.json().get("code") == "forbidden"


@pytest.mark.analysis_platform
@pytest.mark.django_db
def test_sec_other_user_cannot_list_files(apt_other_api, apt_booking_id, apt_researcher_api, apt_seed, apt_mock_agent):
    apt_researcher_api.post(
        f"/api/v1/bookings/{apt_booking_id}/analysis/analyze/",
        {"workflow_id": str(apt_seed.single_step_workflow.id)},
        format="json",
    )
    apt_mock_agent.process_once()
    res = apt_other_api.get(f"/api/v1/bookings/{apt_booking_id}/analysis/files/")
    assert_status(res, 403)
    assert res.json().get("code") == "files_forbidden"


@pytest.mark.analysis_platform
@pytest.mark.django_db
def test_sec_other_user_cannot_archive(apt_other_api, apt_booking_id):
    res = apt_other_api.post(f"/api/v1/bookings/{apt_booking_id}/analysis/archive/", {}, format="json")
    assert_status(res, 403)
    assert res.json().get("code") == "archive_forbidden"


@pytest.mark.analysis_platform
@pytest.mark.django_db
def test_sec_cannot_inject_unmapped_workflow(apt_researcher_api, apt_booking_id):
    res = apt_researcher_api.post(
        f"/api/v1/bookings/{apt_booking_id}/analysis/analyze/",
        {"workflow_id": unmapped_workflow_id()},
        format="json",
    )
    assert res.status_code in {400, 403}


@pytest.mark.analysis_platform
@pytest.mark.django_db
def test_sec_anonymous_denied(apt_anon_api, apt_booking_id):
    for path in (
        f"/api/v1/bookings/{apt_booking_id}/analysis/",
        f"/api/v1/bookings/{apt_booking_id}/analysis/files/",
        f"/api/v1/bookings/{apt_booking_id}/analysis/job/",
        "/api/v1/analysis/workstations/",
        "/api/v1/analysis/operations/commissioning/",
    ):
        res = apt_anon_api.get(path)
        assert res.status_code in {401, 403}, path


@pytest.mark.analysis_platform
@pytest.mark.django_db
def test_sec_researcher_cannot_enumerate_workstations(apt_researcher_api):
    res = apt_researcher_api.get("/api/v1/analysis/workstations/")
    assert res.status_code in {401, 403}


@pytest.mark.analysis_platform
@pytest.mark.django_db
def test_sec_researcher_cannot_access_ops_console(apt_researcher_api):
    res = apt_researcher_api.get("/api/v1/analysis/operations/commissioning/")
    assert_status(res, 403)


@pytest.mark.analysis_platform
@pytest.mark.django_db
def test_sec_invalid_agent_token_rejected():
    api = APIClient()
    res = api.post(
        "/api/v1/analysis/heartbeat/",
        {"CPU": 1, "Memory": 1, "Disk": 1},
        format="json",
        HTTP_AUTHORIZATION="Bearer invalid-apt-token",
        HTTP_X_AGENT_ID="apt-fake",
    )
    assert res.status_code in {401, 403}


@pytest.mark.analysis_platform
@pytest.mark.django_db
def test_sec_cannot_complete_other_users_job_step(apt_other_api, apt_booking_id):
    res = apt_other_api.post(
        f"/api/v1/bookings/{apt_booking_id}/analysis/job/steps/1/complete/",
        {},
        format="json",
    )
    assert_status(res, 403)

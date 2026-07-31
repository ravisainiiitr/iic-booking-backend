"""Fixtures for the Analysis Platform Test Harness."""

from __future__ import annotations

import os

import pytest
from rest_framework.test import APIClient

from tests.analysis_platform.mock_agent import MockAnalysisAgent
from tests.analysis_platform.seeder import AnalysisPlatformSeeder, SeedResult


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


@pytest.fixture
def apt_seed(db) -> SeedResult:
    return AnalysisPlatformSeeder().run()


@pytest.fixture
def apt_researcher_api(apt_seed) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=apt_seed.researcher)
    return client


@pytest.fixture
def apt_other_api(apt_seed) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=apt_seed.other_researcher)
    return client


@pytest.fixture
def apt_admin_api(apt_seed) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=apt_seed.administrator)
    return client


@pytest.fixture
def apt_lab_api(apt_seed) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=apt_seed.lab_incharge)
    return client


@pytest.fixture
def apt_anon_api() -> APIClient:
    return APIClient()


@pytest.fixture
def apt_mock_agent(apt_seed) -> MockAnalysisAgent:
    agent = MockAnalysisAgent.from_seed(apt_seed)
    agent.bootstrap(re_register=False)
    return agent


@pytest.fixture
def apt_booking_id(apt_seed) -> int:
    return int(apt_seed.booking.booking_id)


@pytest.fixture
def analysis_perf_enabled():
    if not _flag("ANALYSIS_PERF"):
        pytest.skip("Performance harness requires ANALYSIS_PERF=1.")
    return True


@pytest.fixture
def analysis_lab_enabled():
    if not _flag("ANALYSIS_LAB"):
        pytest.skip("Real-agent smoke requires ANALYSIS_LAB=1.")
    return True


@pytest.fixture
def analysis_e2e_enabled():
    if not _flag("ANALYSIS_E2E"):
        pytest.skip("Playwright e2e requires ANALYSIS_E2E=1 (run via scripts/run-analysis-tests).")
    return True

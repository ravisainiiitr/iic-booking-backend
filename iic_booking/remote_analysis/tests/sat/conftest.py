"""SAT fixtures and lab/perf gates."""

from __future__ import annotations

import os

import pytest
from rest_framework.test import APIClient

from iic_booking.users.tests.factories import UserFactory


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


@pytest.fixture
def sat_lab_enabled():
    if not _flag("SAT_LAB"):
        pytest.skip("Lab SAT requires SAT_LAB=1 (live agent / staging).")
    return True


@pytest.fixture
def sat_perf_enabled():
    if not _flag("SAT_PERF"):
        pytest.skip("Performance SAT requires SAT_PERF=1.")
    return True


@pytest.fixture
def sat_admin(db):
    return UserFactory(
        user_type="admin",
        is_staff=True,
        is_superuser=True,
        admin_approved=True,
        email_verified=True,
    )


@pytest.fixture
def sat_api(sat_admin):
    client = APIClient()
    client.force_authenticate(user=sat_admin)
    return client


@pytest.fixture
def sat_anon():
    return APIClient()

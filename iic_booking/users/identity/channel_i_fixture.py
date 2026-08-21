"""Staging-only Channel-I userinfo fixtures.

HARD SAFETY:
  - Must never activate when DEPLOYMENT_ENVIRONMENT is production.
  - Requires CHANNEL_I_STAGING_FIXTURE_MODE=true AND staging settings.
  - Does not invent OAuth secrets; only supplies userinfo payloads
    that flow through the real extract/sync/identity architecture.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


FIXTURE_CASES = (
    "STAFF",
    "UG_WITH_END_DATE",
    "UG_WITHOUT_END_DATE",
    "UNKNOWN_DEGREE",
    "MAPPED_DEPARTMENT",
    "UNMAPPED_DEPARTMENT",
    "NO_DATES",
)


def channel_i_fixture_mode_enabled() -> bool:
    if not bool(getattr(settings, "CHANNEL_I_STAGING_FIXTURE_MODE", False)):
        return False
    if bool(getattr(settings, "REAL_INTEGRATION_ENABLED", False)):
        raise ImproperlyConfigured(
            "CHANNEL_I_STAGING_FIXTURE_MODE cannot be used when REAL_INTEGRATION_ENABLED=true. "
            "Disable fixture mode for REAL Channel-I integration."
        )
    env = (getattr(settings, "DEPLOYMENT_ENVIRONMENT", "") or "").upper()
    if env != "STAGING":
        raise ImproperlyConfigured(
            "CHANNEL_I_STAGING_FIXTURE_MODE is only allowed when "
            "DEPLOYMENT_ENVIRONMENT=STAGING. Refusing activation."
        )
    settings_module = (getattr(settings, "SETTINGS_MODULE", "") or "").lower()
    if "production" in settings_module:
        raise ImproperlyConfigured(
            "CHANNEL_I_STAGING_FIXTURE_MODE refused under production settings."
        )
    return True


def require_real_channel_i_credentials_when_fixture_off() -> None:
    """Fail clearly when real Channel-I is expected but secrets are missing."""
    if channel_i_fixture_mode_enabled():
        return
    env = (getattr(settings, "DEPLOYMENT_ENVIRONMENT", "") or "").upper()
    if env != "STAGING":
        return
    if bool(getattr(settings, "REAL_INTEGRATION_ENABLED", False)):
        client_id = (getattr(settings, "OMNIPORT_CLIENT_ID", "") or "").strip()
        client_secret = (getattr(settings, "OMNIPORT_CLIENT_SECRET", "") or "").strip()
        if not client_id or not client_secret:
            raise ImproperlyConfigured(
                "REAL_INTEGRATION_ENABLED=true but OMNIPORT_CLIENT_ID/SECRET missing. "
                "Staging will not invent or reuse production credentials."
            )
        return
    client_id = (getattr(settings, "OMNIPORT_CLIENT_ID", "") or "").strip()
    client_secret = (getattr(settings, "OMNIPORT_CLIENT_SECRET", "") or "").strip()
    if not client_id or not client_secret:
        raise ImproperlyConfigured(
            "STAGING Channel-I credentials missing. Set OMNIPORT_CLIENT_ID and "
            "OMNIPORT_CLIENT_SECRET for real OAuth, or enable "
            "CHANNEL_I_STAGING_FIXTURE_MODE=true for fixture-only qualification. "
            "Staging will not silently use production credentials."
        )


def _base(*, user_id: str, username: str, sex: str, email: str, phone: str) -> dict[str, Any]:
    return {
        "userId": user_id,
        "username": username,
        "person": {"fullName": f"Fixture {username}"},
        "student": {},
        "facultyMember": {},
        "biologicalInformation": {"sex": sex, "gender": sex},
        "contactInformation": {
            "instituteWebmailAddress": email,
            "emailAddress": email,
            "primaryPhoneNumber": phone,
        },
    }


def fixture_payload(case: str) -> dict[str, Any]:
    case = (case or "").strip().upper()
    if case not in FIXTURE_CASES:
        raise KeyError(f"Unknown Channel-I fixture case {case!r}. Allowed: {FIXTURE_CASES}")

    if case == "STAFF":
        p = _base(
            user_id="96001",
            username="100673",
            sex="male",
            email="staff.fixture@iitr.ac.in",
            phone="9000000001",
        )
        # Staff: empty student; username is identity username, NOT auto Employee ID
        p["facultyMember"] = {}
        return p

    if case == "UG_WITH_END_DATE":
        p = _base(
            user_id="96002",
            username="24117001",
            sex="female",
            email="ug.end@iitr.ac.in",
            phone="9000000002",
        )
        p["student"] = {
            "enrolmentNumber": "24117001",
            "start_date": "2021-08-01",
            "end_date": "2025-07-31",
            "branch": {
                "degree": {"name": "B.Tech"},
                "department": {"name": "Department of Mechanical Engineering"},
            },
        }
        return p

    if case == "UG_WITHOUT_END_DATE":
        p = _base(
            user_id="96003",
            username="24117002",
            sex="male",
            email="ug.open@iitr.ac.in",
            phone="9000000003",
        )
        p["student"] = {
            "enrolmentNumber": "24117002",
            "start_date": "2024-07-18",
            "end_date": None,
            "branch": {
                "degree": {"name": "B.Tech"},
                "department": {"name": "Department of Mechanical Engineering"},
            },
        }
        return p

    if case == "UNKNOWN_DEGREE":
        p = _base(
            user_id="96004",
            username="24999001",
            sex="other",
            email="unk.deg@iitr.ac.in",
            phone="9000000004",
        )
        p["student"] = {
            "enrolmentNumber": "24999001",
            "start_date": "2023-01-01",
            "end_date": None,
            "branch": {
                "degree": {"name": "Exotic Certificate Programme"},
                "department": {"name": "Department of Mechanical Engineering"},
            },
        }
        return p

    if case == "MAPPED_DEPARTMENT":
        p = _base(
            user_id="96005",
            username="24117005",
            sex="female",
            email="mapped.dept@iitr.ac.in",
            phone="9000000005",
        )
        p["student"] = {
            "enrolmentNumber": "24117005",
            "startDate": "2022-08-01",
            "endDate": None,
            "branch": {
                "degree": {"name": "B.Tech"},
                "department": {"name": "Department of Mechanical Engineering"},
            },
        }
        return p

    if case == "UNMAPPED_DEPARTMENT":
        p = _base(
            user_id="96006",
            username="24117006",
            sex="male",
            email="unmap.dept@iitr.ac.in",
            phone="9000000006",
        )
        p["student"] = {
            "enrolmentNumber": "24117006",
            "start_date": "2022-08-01",
            "end_date": None,
            "branch": {
                "degree": {"name": "B.Tech"},
                "department": {"name": "Department of Unmapped Sciences"},
            },
        }
        return p

    # NO_DATES
    p = _base(
        user_id="96007",
        username="24900000",
        sex="male",
        email="nodates@iitr.ac.in",
        phone="9000000007",
    )
    p["student"] = {
        "enrolmentNumber": "24900000",
        "start_date": None,
        "end_date": None,
        "branch": {
            "degree": {"name": "B.Tech"},
            "department": {"name": "Department of Mechanical Engineering"},
        },
    }
    return p


def all_fixture_payloads() -> dict[str, dict[str, Any]]:
    return {name: deepcopy(fixture_payload(name)) for name in FIXTURE_CASES}

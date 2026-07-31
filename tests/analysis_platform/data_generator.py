"""Synthetic data helpers for load / negative scenarios."""

from __future__ import annotations

import uuid
from decimal import Decimal

from django.utils import timezone

from iic_booking.equipment.models import Booking, BookingStatus, ChargeProfile, Equipment
from iic_booking.remote_analysis.catalog_models import AnalysisSoftwareCatalog, EquipmentAnalysisPool
from iic_booking.remote_analysis.constants import WorkstationStatus
from iic_booking.remote_analysis.models import AnalysisWorkstation, InstalledSoftware
from iic_booking.remote_analysis.services.tokens import issue_agent_token
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


def make_researcher(*, suffix: str | None = None):
    tag = suffix or uuid.uuid4().hex[:6]
    return UserFactory(
        email=f"apt-gen-{tag}@example.com",
        user_type="student",
        admin_approved=True,
        email_verified=True,
    )


def make_bookings_for_equipment(equipment: Equipment, *, count: int, user=None) -> list[Booking]:
    user = user or make_researcher()
    profile, _ = ChargeProfile.objects.get_or_create(
        equipment=equipment,
        user_type=UserType.STUDENT,
        defaults={"primary_unit_charge": Decimal("10.00")},
    )
    bookings = []
    for i in range(count):
        bookings.append(
            Booking.objects.create(
                user=user,
                equipment=equipment,
                charge_profile=profile,
                status=BookingStatus.COMPLETED,
                analysis_available=True,
                total_time_minutes=60,
                total_charge=Decimal("10.00"),
                virtual_booking_id=f"APTGEN{uuid.uuid4().hex[:10].upper()}{i:03d}"[:32],
            )
        )
    return bookings


def make_mock_workstations(*, count: int, software_names: list[str] | None = None) -> list[tuple]:
    """Return list of (workstation, token)."""
    names = software_names or ["Notepad", "Origin Test", "MATLAB Test"]
    out = []
    for i in range(count):
        tag = uuid.uuid4().hex[:6]
        ws = AnalysisWorkstation.objects.create(
            agent_id=f"apt-load-{tag}",
            hostname=f"APT-LOAD-{tag.upper()}",
            display_name=f"Load PC {i}",
            status=WorkstationStatus.AVAILABLE,
            enabled=True,
            health_score=90,
            last_heartbeat=timezone.now(),
            supports_rdp=True,
            memory_gb=16,
            cpu_cores=4,
            storage_gb=256,
            agent_version="mock-load",
        )
        _, token = issue_agent_token(ws)
        for name in names:
            InstalledSoftware.objects.create(
                workstation=ws, software_name=name, version="1.0", is_present=True
            )
        out.append((ws, token))
    return out


def attach_workstations_to_pool(equipment: Equipment, workstations: list) -> None:
    for ws in workstations:
        EquipmentAnalysisPool.objects.get_or_create(
            equipment=equipment, workstation=ws, defaults={"priority_boost": 10}
        )


def fake_raw_blob(*, size_mb: int = 1) -> bytes:
    """Generate an in-memory RAW-like payload (not written to disk unless caller does)."""
    chunk = b"APT-RAW-" + uuid.uuid4().bytes
    target = max(1, size_mb) * 1024 * 1024
    return (chunk * (target // len(chunk) + 1))[:target]


def unmapped_workflow_id() -> str:
    """UUID that will not match seeded equipment mappings."""
    return str(uuid.uuid4())

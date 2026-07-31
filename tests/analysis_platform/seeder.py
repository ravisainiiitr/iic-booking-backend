"""Seed a complete Analysis Platform lab for automated testing.

Creates software catalog, workflows, equipment, pool, booking, and personas.
Idempotent by fixed codes/slugs where practical.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from django.utils.text import slugify

from iic_booking.equipment.models import Booking, BookingStatus, ChargeProfile, Equipment
from iic_booking.remote_analysis.catalog_models import (
    AnalysisSoftwareCatalog,
    EquipmentAnalysisPool,
    EquipmentAnalysisSoftware,
)
from iic_booking.remote_analysis.constants import WorkstationStatus
from iic_booking.remote_analysis.models import AnalysisWorkstation, InstalledSoftware
from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings
from iic_booking.remote_analysis.services.tokens import issue_agent_token
from iic_booking.remote_analysis.workflow_models import (
    AnalysisWorkflow,
    AnalysisWorkflowStep,
    AnalysisWorkflowVersion,
    EquipmentAnalysisWorkflow,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


SEED_PREFIX = "APT"  # Analysis Platform Test


@dataclass
class SeedResult:
    researcher: object
    lab_incharge: object
    administrator: object
    other_researcher: object
    equipment: Equipment
    workstation: AnalysisWorkstation
    agent_token: str
    software: list = field(default_factory=list)
    single_step_workflow: AnalysisWorkflow | None = None
    multi_step_workflow: AnalysisWorkflow | None = None
    booking: Booking | None = None
    charge_profile: ChargeProfile | None = None


class AnalysisPlatformSeeder:
    """Create a self-contained test lab environment."""

    def __init__(self, *, unique_suffix: str | None = None):
        self.suffix = unique_suffix or uuid.uuid4().hex[:6].upper()

    def run(self) -> SeedResult:
        self._ensure_settings()
        admin = self._user(
            email=f"apt-admin-{self.suffix.lower()}@example.com",
            user_type="admin",
            is_staff=True,
            is_superuser=True,
        )
        lab = self._user(
            email=f"apt-lab-{self.suffix.lower()}@example.com",
            user_type="officer_in_charge",
            is_staff=True,
        )
        researcher = self._user(
            email=f"apt-researcher-{self.suffix.lower()}@example.com",
            user_type="student",
        )
        other = self._user(
            email=f"apt-other-{self.suffix.lower()}@example.com",
            user_type="student",
        )

        softwares = [
            self._software("Notepad", f"notepad-{self.suffix.lower()}"),
            self._software("Origin Test", f"origin-test-{self.suffix.lower()}"),
            self._software("MATLAB Test", f"matlab-test-{self.suffix.lower()}"),
        ]
        for s in softwares:
            s.ensure_software_requirement()

        equipment = Equipment.objects.create(
            name=f"PXRD Test Equipment {self.suffix}",
            code=f"{SEED_PREFIX}{self.suffix}"[:32],
            enable_remote_analysis=True,
            analysis_access_duration=72,
            slot_duration_minutes=60,
            user_rating_enabled=False,
        )

        for i, soft in enumerate(softwares):
            EquipmentAnalysisSoftware.objects.get_or_create(
                equipment=equipment,
                catalog=soft,
                defaults={"is_default": i == 0, "sort_order": i},
            )

        single = self._workflow(
            name=f"PXRD Single-Step {self.suffix}",
            slug=f"pxrd-single-{self.suffix.lower()}",
            steps=[(softwares[0], "Notepad Environment")],
        )
        multi = self._workflow(
            name=f"PXRD Multi-Step {self.suffix}",
            slug=f"pxrd-multi-{self.suffix.lower()}",
            steps=[
                (softwares[1], "Origin Test Environment"),
                (softwares[2], "MATLAB Test Environment"),
            ],
        )
        EquipmentAnalysisWorkflow.objects.create(
            equipment=equipment, workflow=single, is_default=True, sort_order=0
        )
        EquipmentAnalysisWorkflow.objects.create(
            equipment=equipment, workflow=multi, is_default=False, sort_order=1
        )

        workstation = AnalysisWorkstation.objects.create(
            agent_id=f"apt-mock-{self.suffix.lower()}",
            hostname=f"APT-MOCK-{self.suffix}",
            display_name=f"APT Mock Analysis PC {self.suffix}",
            status=WorkstationStatus.AVAILABLE,
            enabled=True,
            health_score=95,
            last_heartbeat=timezone.now(),
            supports_rdp=True,
            memory_gb=32,
            cpu_cores=8,
            storage_gb=500,
            agent_version="mock-1.0.0",
        )
        _, token = issue_agent_token(workstation)
        for soft in softwares:
            InstalledSoftware.objects.create(
                workstation=workstation,
                software_name=soft.name,
                version="1.0",
                is_present=True,
            )
        EquipmentAnalysisPool.objects.create(
            equipment=equipment, workstation=workstation, priority_boost=20
        )

        profile = ChargeProfile.objects.create(
            equipment=equipment,
            user_type=UserType.STUDENT,
            primary_unit_charge=Decimal("10.00"),
        )
        booking = Booking.objects.create(
            user=researcher,
            equipment=equipment,
            charge_profile=profile,
            status=BookingStatus.COMPLETED,
            analysis_available=True,
            total_time_minutes=60,
            total_charge=Decimal("10.00"),
            virtual_booking_id=f"{SEED_PREFIX}{self.suffix}202600001",
        )

        return SeedResult(
            researcher=researcher,
            lab_incharge=lab,
            administrator=admin,
            other_researcher=other,
            equipment=equipment,
            workstation=workstation,
            agent_token=token,
            software=softwares,
            single_step_workflow=single,
            multi_step_workflow=multi,
            booking=booking,
            charge_profile=profile,
        )

    def cleanup(self, seed: SeedResult) -> None:
        """Best-effort teardown of seeded rows (tests usually use transaction rollback)."""
        from iic_booking.remote_analysis.workflow_models import AnalysisJob

        if seed.booking is not None:
            AnalysisJob.objects.filter(booking=seed.booking).delete()
            seed.booking.delete()
        if seed.equipment is not None:
            EquipmentAnalysisWorkflow.objects.filter(equipment=seed.equipment).delete()
            EquipmentAnalysisSoftware.objects.filter(equipment=seed.equipment).delete()
            EquipmentAnalysisPool.objects.filter(equipment=seed.equipment).delete()
            if seed.charge_profile is not None:
                seed.charge_profile.delete()
            seed.equipment.delete()
        if seed.workstation is not None:
            seed.workstation.delete()
        for wf in (seed.single_step_workflow, seed.multi_step_workflow):
            if wf is not None:
                wf.delete()
        for soft in seed.software or []:
            soft.delete()

    def _ensure_settings(self) -> None:
        settings_obj = RemoteAnalysisSettings.get_solo()
        settings_obj.mock_guacamole = True
        settings_obj.guacamole_api_url = settings_obj.guacamole_api_url or ""
        settings_obj.analyze_data_prefer_workflow = True
        settings_obj.analyze_data_require_s3_files = False
        settings_obj.analyze_data_stage_raw_on_launch = False
        settings_obj.save()

    def _user(self, *, email: str, user_type: str, is_staff: bool = False, is_superuser: bool = False):
        User = get_user_model()
        existing = User.objects.filter(email=email).first()
        if existing:
            return existing
        return UserFactory(
            email=email,
            user_type=user_type,
            is_staff=is_staff,
            is_superuser=is_superuser,
            admin_approved=True,
            email_verified=True,
            password="apt-test-password",
        )

    def _software(self, name: str, slug: str) -> AnalysisSoftwareCatalog:
        obj, _ = AnalysisSoftwareCatalog.objects.get_or_create(
            slug=slugify(slug)[:250],
            defaults={"name": name, "is_active": True, "max_concurrent": 0},
        )
        return obj

    def _workflow(self, *, name: str, slug: str, steps: list) -> AnalysisWorkflow:
        wf, _ = AnalysisWorkflow.objects.get_or_create(
            slug=slugify(slug)[:250],
            defaults={
                "name": name,
                "description": f"Harness-seeded workflow {name}",
                "is_active": True,
                "estimated_duration_minutes": 30 * len(steps),
                "require_raw_data": False,
            },
        )
        version = wf.versions.filter(is_published=True).first()
        if version is None:
            version = AnalysisWorkflowVersion.objects.create(
                workflow=wf,
                version_number=1,
                label="v1",
                is_published=True,
                published_at=timezone.now(),
                changelog="Harness seed",
            )
            for n, (catalog, env_label) in enumerate(steps, start=1):
                AnalysisWorkflowStep.objects.create(
                    version=version,
                    step_number=n,
                    title=catalog.name,
                    software=catalog,
                    mandatory=True,
                    estimated_duration_minutes=30,
                    expected_output_folder=f"Step{n:02d}",
                    expected_outputs=[],
                    environment_label=env_label,
                    operator_instructions=f"Harness step {n}: use {catalog.name}",
                )
        return wf

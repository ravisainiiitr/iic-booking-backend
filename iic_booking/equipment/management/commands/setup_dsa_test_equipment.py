"""
Create or refresh the Department Sync Agent test equipment (visible to test accounts only).

    python manage.py setup_dsa_test_equipment --status
    python manage.py setup_dsa_test_equipment --confirm SETUP_DSA_TEST [--dsa-machine-name RAVI]

Idempotent. Never creates user accounts: the seeded test student, test faculty, test Officer In-Charge
(test.manager) and test Lab Operator (test.operator) must already exist. Charges are zero, the results
deadline automation is off, and only flagged test accounts (plus the Main Administrator) can see or book
the equipment. Output is limited to non-sensitive facts (ids, codes, counts, installer SHA-256).
"""

from __future__ import annotations

import json
from datetime import time as dt_time
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

CONFIRM_TOKEN = "SETUP_DSA_TEST"
DEFAULT_CODE = "DSATEST"
DEFAULT_NAME = "DSA Test Equipment (TEST)"
SLOT_HOURS = [(9, 10), (10, 11), (11, 12), (12, 13), (14, 15), (15, 16), (16, 17), (17, 18)]


class Command(BaseCommand):
    help = "Create/refresh the test-accounts-only equipment used for Department Sync Agent end-to-end tests."

    def add_arguments(self, parser):
        parser.add_argument("--code", default=DEFAULT_CODE)
        parser.add_argument("--name", default=DEFAULT_NAME)
        parser.add_argument("--department-code", default="IIC", help="Internal department code (default IIC).")
        parser.add_argument(
            "--dsa-machine-name",
            default="",
            help="Assign the equipment to the active Department Sync Agent with this machine name (e.g. RAVI).",
        )
        parser.add_argument("--slot-days", type=int, default=21, help="Generate daily slots for this many days.")
        parser.add_argument("--status", action="store_true", help="Read-only report; changes nothing.")
        parser.add_argument("--json", action="store_true", help="Print the report as JSON.")
        parser.add_argument("--confirm", default="", help=f"Type {CONFIRM_TOKEN} to create or update.")

    def handle(self, *args, **opts):
        code = (opts["code"] or DEFAULT_CODE).strip()
        machine = (opts["dsa_machine_name"] or "").strip()
        if opts["status"]:
            self._emit(self._report(code, machine), opts["json"])
            return
        if (opts["confirm"] or "").strip() != CONFIRM_TOKEN:
            raise CommandError(f"Refusing to change data: pass --confirm {CONFIRM_TOKEN} (or use --status).")

        users = self._test_users()
        department = self._department(opts["department_code"])
        with transaction.atomic():
            equipment, created = self._equipment(code, opts["name"], department)
            self._inputs_and_charges(equipment)
            self._slot_masters(equipment)
            self._staff(equipment, users)
            if machine:
                self._assign_agent(equipment, machine)
        slots = self._generate_slots(equipment, max(0, int(opts["slot_days"] or 0)))
        self.stdout.write(
            f"{'Created' if created else 'Updated'} equipment {equipment.code} (id {equipment.pk}); "
            f"new daily slots: {slots}"
        )
        self._emit(self._report(code, machine), opts["json"])

    def _test_users(self):
        from iic_booking.users.models import User
        from iic_booking.users.models.user_type import UserType
        from iic_booking.users.test_accounts import user_email_for_type

        wanted = {
            "student": UserType.STUDENT,
            "faculty": UserType.FACULTY,
            "manager": UserType.MANAGER,
            "operator": UserType.OPERATOR,
        }
        found = {}
        missing = []
        for role, user_type in wanted.items():
            user = User.objects.filter(
                email__iexact=user_email_for_type(user_type), is_test_account=True, user_type=user_type
            ).first()
            if user is None:
                missing.append(role)
            else:
                found[role] = user
        if missing:
            raise CommandError(
                "Seeded test account(s) missing: " + ", ".join(missing) + ". Run Seed Test Users first; "
                "this command never creates accounts."
            )
        return found

    def _department(self, dept_code: str):
        from iic_booking.users.models import Department
        from iic_booking.users.models.department import DepartmentType

        dept = Department.objects.filter(code__iexact=(dept_code or "").strip(), department_type=DepartmentType.INTERNAL).first()
        if dept is None:
            raise CommandError(f"Internal department with code {dept_code!r} not found.")
        return dept

    def _equipment(self, code: str, name: str, department):
        from iic_booking.equipment.models import Equipment, EquipmentProfileType, EquipmentStatus

        values = {
            "name": name,
            "description": (
                "Testing equipment for the Department Sync Agent / Equipment PC end-to-end tests. "
                "Not a real instrument; visible to test accounts only."
            ),
            "location": "IIC test bench (not a real instrument)",
            "status": EquipmentStatus.ACTIVE,
            "profile_type": EquipmentProfileType.SAMPLE,
            "internal_department": department,
            "visible_to_test_accounts_only": True,
            "slot_duration_minutes": 60,
            "slots_per_day": len(SLOT_HOURS),
            "skip_quota_check": True,
            "reschedule_hours_threshold": 1,
            "results_base_location": r"D:\IICData",
            "user_rating_enabled": False,
            "dsa_enabled": True,
            "dsa_watch_folder_enabled": True,
            "results_deadline_value": 0,
        }
        field_names = {f.name for f in Equipment._meta.get_fields()}
        values = {k: v for k, v in values.items() if k in field_names}
        equipment = Equipment.objects.filter(code=code).first()
        if equipment is not None and not equipment.visible_to_test_accounts_only and equipment.bookings.filter(
            user__is_test_account=False
        ).exists():
            raise CommandError(f"Equipment {code} already exists with real bookings; refusing to repurpose it.")
        created = equipment is None
        if created:
            equipment = Equipment(code=code)
        for key, value in values.items():
            setattr(equipment, key, value)
        equipment.save()
        return equipment, created

    def _inputs_and_charges(self, equipment):
        from iic_booking.equipment.models import (
            ChargeProfile,
            ChargeProfilePricingProfile,
            DynamicInputField,
            EquipmentProfileType,
        )
        from iic_booking.users.models.user_type import UserType

        for user_type in (UserType.STUDENT, UserType.FACULTY):
            DynamicInputField.objects.update_or_create(
                equipment=equipment,
                user_type=user_type,
                field_key="A",
                defaults={
                    "field_label": "Number of samples",
                    "field_type": "NUMERIC",
                    "is_required": True,
                    "default_value": "1",
                },
            )
            for pricing in (ChargeProfilePricingProfile.STANDARD, ChargeProfilePricingProfile.DISCOUNTED):
                ChargeProfile.objects.update_or_create(
                    equipment=equipment,
                    user_type=user_type,
                    pricing_profile=pricing,
                    defaults={
                        "profile_type": EquipmentProfileType.SAMPLE,
                        "is_active": True,
                        "primary_unit_charge": Decimal("0.00"),
                        "secondary_unit_charge": Decimal("0.00"),
                        "breakpoint": None,
                        "time_formula": "A * 30",
                    },
                )
        ChargeProfile.objects.filter(equipment=equipment).exclude(
            primary_unit_charge=Decimal("0.00"), secondary_unit_charge=Decimal("0.00")
        ).update(is_active=False)

    def _slot_masters(self, equipment):
        from iic_booking.equipment.models import SlotMaster

        for number, (start, end) in enumerate(SLOT_HOURS, start=1):
            SlotMaster.objects.update_or_create(
                equipment=equipment,
                slot_number=number,
                defaults={
                    "slot_name": f"{start:02d}:00-{end:02d}:00",
                    "open_time": dt_time(start, 0),
                    "close_time": dt_time(end, 0),
                    "is_active": True,
                },
            )

    def _staff(self, equipment, users):
        from iic_booking.equipment.models import EquipmentManager, EquipmentOperator

        EquipmentManager.objects.get_or_create(equipment=equipment, manager=users["manager"])
        EquipmentOperator.objects.get_or_create(
            equipment=equipment,
            operator=users["operator"],
            defaults={"role": EquipmentOperator.Role.PRIMARY},
        )
        real_oics = EquipmentManager.objects.filter(equipment=equipment).exclude(manager__is_test_account=True)
        real_ops = EquipmentOperator.objects.filter(equipment=equipment).exclude(operator__is_test_account=True)
        if real_oics.exists() or real_ops.exists():
            real_oics.delete()
            real_ops.delete()

    def _assign_agent(self, equipment, machine_name: str):
        from iic_booking.sync.models import (
            AgentAssignment,
            AgentLifecycleStatus,
            DepartmentSyncAgent,
            EquipmentSyncProfile,
        )

        agent = (
            DepartmentSyncAgent.objects.filter(machine_name__iexact=machine_name, is_active=True)
            .exclude(status__in=[AgentLifecycleStatus.DISABLED, AgentLifecycleStatus.REVOKED])
            .order_by("-last_seen_at", "-last_heartbeat_at")
            .first()
        )
        if agent is None:
            raise CommandError(f"No active Department Sync Agent with machine name {machine_name!r}.")
        if agent.department_id and agent.department_id != equipment.internal_department_id:
            raise CommandError("That agent belongs to a different department than the test equipment.")
        profile, _ = EquipmentSyncProfile.objects.get_or_create(
            equipment=equipment,
            defaults={"primary_agent": agent, "sync_enabled": True, "watch_enabled": True},
        )
        if profile.primary_agent_id != agent.id:
            profile.primary_agent = agent
            profile.configuration_version = (profile.configuration_version or 0) + 1
            profile.save(update_fields=["primary_agent", "configuration_version", "updated_at"])
        AgentAssignment.objects.filter(sync_profile=profile, is_active=True).exclude(sync_agent=agent).update(
            is_active=False, unassigned_at=timezone.now()
        )
        AgentAssignment.objects.update_or_create(
            sync_agent=agent,
            sync_profile=profile,
            defaults={"is_active": True, "unassigned_at": None, "notes": "DSA end-to-end test equipment"},
        )
        # A new profile starts at configuration_version 1, below the agent's max, so heartbeat alone
        # would never tell the DSA to re-bootstrap and pick up the test equipment.
        if not agent.bootstrap_required:
            agent.bootstrap_required = True
            agent.save(update_fields=["bootstrap_required", "updated_at"])

    def _generate_slots(self, equipment, days: int) -> int:
        from iic_booking.equipment.slot_utils import SlotGenerator

        today = timezone.localdate()
        created = 0
        for offset in range(days):
            created += len(
                SlotGenerator.generate_daily_slots(equipment, today + timedelta(days=offset), allow_holiday=True)
                or []
            )
        return created

    def _agent_scope(self, machine_name: str) -> dict:
        from iic_booking.sync.models import AgentAssignment, DepartmentSyncAgent
        from iic_booking.sync.services.tokens import agent_expected_versions

        agent = (
            DepartmentSyncAgent.objects.filter(machine_name__iexact=machine_name, is_active=True)
            .order_by("-last_seen_at", "-last_heartbeat_at")
            .first()
        )
        if agent is None:
            return {"machine_name": machine_name, "found": False}
        codes = sorted(
            AgentAssignment.objects.filter(sync_agent=agent, is_active=True).values_list(
                "sync_profile__equipment__code", flat=True
            )
        )
        expected_config, expected_schema = agent_expected_versions(agent)
        return {
            "machine_name": agent.machine_name,
            "found": True,
            "status": agent.status,
            "version": agent.version,
            "last_seen_at": agent.last_seen_at.isoformat() if agent.last_seen_at else None,
            "bootstrap_required": agent.bootstrap_required,
            "expected_configuration_version": expected_config,
            "reported_configuration_version": agent.last_reported_configuration_version,
            "expected_schema_version": expected_schema,
            "reported_schema_version": agent.last_reported_schema_version,
            "assigned_count": len(codes),
            "assigned_codes": codes,
        }

    def _report(self, code: str, machine_name: str = "") -> dict:
        from iic_booking.equipment.models import (
            Booking,
            ChargeProfile,
            DailySlot,
            Equipment,
            EquipmentManager,
            EquipmentOperator,
        )

        report: dict = {"code": code, "found": False, "installers": self._installers()}
        if machine_name:
            report["dsa_scope"] = self._agent_scope(machine_name)
        equipment = Equipment.objects.select_related("internal_department").filter(code=code).first()
        if equipment is None:
            return report
        today = timezone.localdate()
        assignments = []
        try:
            from iic_booking.sync.models import AgentAssignment

            for a in AgentAssignment.objects.filter(sync_profile__equipment=equipment, is_active=True).select_related(
                "sync_agent"
            ):
                assignments.append(
                    {
                        "agent_name": a.sync_agent.agent_name,
                        "machine_name": a.sync_agent.machine_name,
                        "agent_version": a.sync_agent.version,
                        "last_seen_at": a.sync_agent.last_seen_at.isoformat() if a.sync_agent.last_seen_at else None,
                    }
                )
        except Exception as exc:  # noqa: BLE001
            assignments.append({"error": type(exc).__name__})
        bookings = Booking.objects.filter(equipment=equipment)
        report.update(
            {
                "found": True,
                "equipment_id": equipment.pk,
                "name": equipment.name,
                "department": getattr(equipment.internal_department, "code", None),
                "status": equipment.status,
                "visible_to_test_accounts_only": equipment.visible_to_test_accounts_only,
                "dsa_enabled": getattr(equipment, "dsa_enabled", None),
                "oic_all_test_accounts": not EquipmentManager.objects.filter(equipment=equipment)
                .exclude(manager__is_test_account=True)
                .exists(),
                "operators_all_test_accounts": not EquipmentOperator.objects.filter(equipment=equipment)
                .exclude(operator__is_test_account=True)
                .exists(),
                "oic_count": EquipmentManager.objects.filter(equipment=equipment).count(),
                "operator_count": EquipmentOperator.objects.filter(equipment=equipment).count(),
                "nonzero_active_charge_profiles": ChargeProfile.objects.filter(equipment=equipment, is_active=True)
                .exclude(primary_unit_charge=0, secondary_unit_charge=0)
                .count(),
                "upcoming_daily_slots": DailySlot.objects.filter(
                    slot_master__equipment=equipment, date__gte=today
                ).count(),
                "bookings_total": bookings.count(),
                "bookings_by_non_test_users": bookings.filter(user__is_test_account=False).count(),
                "dsa_assignments": assignments,
            }
        )
        return report

    def _installers(self) -> dict:
        out: dict = {}
        try:
            from iic_booking.sync.installer.models import DsaInstallerRelease

            rel = (
                DsaInstallerRelease.objects.filter(is_active=True, is_latest=True).first()
                or DsaInstallerRelease.objects.filter(is_active=True).order_by("-release_date").first()
            )
            if rel is not None:
                out["dsa"] = {"version": rel.version, "sha256": rel.sha256 or "", "file": rel.original_name or ""}
        except Exception as exc:  # noqa: BLE001
            out["dsa"] = {"error": type(exc).__name__}
        try:
            from iic_booking.deployment.models import EquipmentPcWizardRelease

            rel = (
                EquipmentPcWizardRelease.objects.filter(is_active=True, is_latest=True).first()
                or EquipmentPcWizardRelease.objects.filter(is_active=True).order_by("-release_date").first()
            )
            if rel is not None:
                out["eq_wizard"] = {
                    "version": rel.version,
                    "build_number": getattr(rel, "build_number", None),
                    "sha256": rel.sha256 or "",
                    "file": rel.original_name or "",
                }
        except Exception as exc:  # noqa: BLE001
            out["eq_wizard"] = {"error": type(exc).__name__}
        return out

    def _emit(self, report: dict, as_json: bool):
        if as_json:
            self.stdout.write(json.dumps(report, indent=2, default=str))
            return
        for key, value in report.items():
            self.stdout.write(f"{key}: {json.dumps(value, default=str)}")

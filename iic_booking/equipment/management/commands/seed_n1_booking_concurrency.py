"""
Seed Phase N.1 booking-concurrency SAT fixtures (idempotent).

Creates departments, users, wallets, grants, and dedicated N1-* equipments.

  python manage.py seed_n1_booking_concurrency
  python manage.py seed_n1_booking_concurrency --password '...' --wipe-bookings
"""

from __future__ import annotations

import json
from datetime import date, datetime, time as dt_time, timedelta
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from iic_booking.equipment.models import (
    ChargeProfile,
    ChargeProfilePricingProfile,
    DailySlot,
    Equipment,
    EquipmentCategory,
    EquipmentGroup,
    EquipmentGroupQuota,
    EquipmentProfileType,
    EquipmentStatus,
    QuotaType,
    SlotMaster,
    SlotStatus,
)
from iic_booking.equipment.slot_utils import SlotGenerator
from iic_booking.users.models import User
from iic_booking.users.models.department import Department, DepartmentType
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import (
    SubWallet,
    Wallet,
    WalletJoinRequest,
    WalletJoinRequestStatus,
)

N1_PASSWORD_DEFAULT = "N1Sat@IIC2026!"
N1_PREFIX = "n1.sat"
WALLET_INR = Decimal("100000.00")
LOW_WALLET_INR = Decimal("50.00")  # for overdraft tests


class Command(BaseCommand):
    help = "Seed Phase N.1 concurrency SAT users, wallets, and equipment."

    def add_arguments(self, parser):
        parser.add_argument("--password", default=N1_PASSWORD_DEFAULT)
        parser.add_argument(
            "--manifest",
            default="/tmp/n1_sat_manifest.json",
            help="Write credentials + equipment ids JSON here (host path via docker -v or /tmp).",
        )
        parser.add_argument(
            "--wipe-bookings",
            action="store_true",
            help="Cancel/clear N1 equipment bookings and reset slots to AVAILABLE.",
        )

    def handle(self, *args, **options):
        password = options["password"]
        manifest_path = Path(options["manifest"])
        wipe = options["wipe_bookings"]

        with transaction.atomic():
            depts = self._depts()
            faculty_by_dept = self._users(depts, password)
            equipments = self._equipments(depts)
            if wipe:
                self._wipe_n1_bookings(equipments)
            self._ensure_slots(equipments)

        manifest = {
            "password": password,
            "generated_at": timezone.now().isoformat(),
            "departments": [{"id": d.id, "code": d.code, "name": d.name} for d in depts],
            "users": self._user_manifest(),
            "equipments": [
                {
                    "id": e.pk,
                    "code": e.code,
                    "name": e.name,
                    "slot_duration_minutes": e.slot_duration_minutes,
                    "waitlist_queue_depth": e.waitlist_queue_depth,
                }
                for e in Equipment.objects.filter(code__startswith="N1-").order_by("code")
            ],
        }
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"Wrote manifest {manifest_path}"))
        self.stdout.write(
            self.style.SUCCESS(
                f"Users={User.objects.filter(email__startswith=N1_PREFIX).count()} "
                f"Equipments={Equipment.objects.filter(code__startswith='N1-').count()}"
            )
        )

    def _depts(self):
        specs = [
            ("N1-CHEM", "N1 Chemistry", DepartmentType.INTERNAL),
            ("N1-PHYS", "N1 Physics", DepartmentType.INTERNAL),
            ("N1-MECH", "N1 Mechanical", DepartmentType.INTERNAL),
            ("N1-EXT", "N1 External Org", DepartmentType.EXTERNAL),
        ]
        out = []
        for code, name, dtype in specs:
            d, _ = Department.objects.get_or_create(
                code=code,
                defaults={"name": name, "department_type": dtype},
            )
            if d.department_type != dtype:
                d.department_type = dtype
                d.save(update_fields=["department_type"])
            out.append(d)
        return out

    def _users(self, depts, password):
        internal = [d for d in depts if d.department_type == DepartmentType.INTERNAL]
        external = [d for d in depts if d.department_type == DepartmentType.EXTERNAL][0]
        faculty_by_dept: dict[int, list[User]] = {d.id: [] for d in internal}

        plans = [
            ("student", 50, UserType.STUDENT),
            ("faculty", 20, UserType.FACULTY),
            ("external", 10, UserType.EXTERNAL),
            ("project", 10, UserType.OTHER),
            ("deptadmin", 5, UserType.DEPT_ADMIN),
            ("lab", 5, UserType.OPERATOR),
            # Extra bookers for large concurrency waves (still is_test_account)
            ("booker", 950, UserType.FACULTY),
        ]

        for role, count, utype in plans:
            for i in range(1, count + 1):
                email = f"{N1_PREFIX}.{role}{i:03d}@iic-booking.test"
                dept = external if utype == UserType.EXTERNAL else internal[(i - 1) % len(internal)]
                defaults = {
                    "name": f"N1 {role.title()} {i:03d}",
                    "user_type": utype,
                    "department": dept,
                    "is_test_account": True,
                    "email_verified": True,
                    "admin_approved": True,
                    "supervisor_approved": True,
                    "is_active": True,
                    "force_inactive": False,
                    "access_on_hold": False,
                    "is_staff": utype in UserType.get_admin_panel_codes(),
                }
                user, created = User.objects.get_or_create(email=email, defaults=defaults)
                for k, v in defaults.items():
                    setattr(user, k, v)
                user.set_password(password)
                user.save()
                if utype == UserType.FACULTY:
                    faculty_by_dept[dept.id].append(user)
                    self._ensure_wallet(user, dept, WALLET_INR)
                elif utype == UserType.EXTERNAL:
                    self._ensure_wallet(user, dept, WALLET_INR)
                elif utype == UserType.STUDENT:
                    # shared faculty wallet
                    facs = faculty_by_dept[dept.id]
                    fac = facs[(i - 1) % max(1, len(facs))] if facs else None
                    if fac is None:
                        # create synthetic faculty owner if needed
                        fac_email = f"{N1_PREFIX}.faculty-owner-{dept.code.lower()}@iic-booking.test"
                        fac, _ = User.objects.get_or_create(
                            email=fac_email,
                            defaults={
                                "name": f"N1 Faculty Owner {dept.code}",
                                "user_type": UserType.FACULTY,
                                "department": dept,
                                "is_test_account": True,
                                "email_verified": True,
                                "admin_approved": True,
                                "supervisor_approved": True,
                                "is_active": True,
                            },
                        )
                        fac.set_password(password)
                        fac.save()
                        faculty_by_dept[dept.id].append(fac)
                        self._ensure_wallet(fac, dept, WALLET_INR)
                    self._join_wallet(user, fac)
                elif utype == UserType.OTHER:
                    facs = faculty_by_dept[dept.id]
                    fac = facs[0] if facs else None
                    if fac:
                        self._join_wallet(user, fac)
                # dept_admin / lab: no booking wallet required

        # Dedicated low-balance faculty for wallet race tests
        low, _ = User.objects.get_or_create(
            email=f"{N1_PREFIX}.wallet-low@iic-booking.test",
            defaults={
                "name": "N1 Wallet Low",
                "user_type": UserType.FACULTY,
                "department": internal[0],
                "is_test_account": True,
                "email_verified": True,
                "admin_approved": True,
                "supervisor_approved": True,
                "is_active": True,
            },
        )
        low.set_password(password)
        low.save()
        self._ensure_wallet(low, internal[0], LOW_WALLET_INR, force_balance=True)
        return faculty_by_dept

    def _ensure_wallet(self, user, dept, amount, force_balance=False):
        wallet, _ = Wallet.objects.get_or_create(user=user)
        sw, created = SubWallet.objects.get_or_create(
            wallet=wallet,
            department=dept,
            defaults={"balance": amount},
        )
        if force_balance or created:
            SubWallet.objects.filter(pk=sw.pk).update(balance=amount)
        elif sw.balance < amount:
            SubWallet.objects.filter(pk=sw.pk).update(balance=amount)

    def _join_wallet(self, student, faculty):
        wallet, _ = Wallet.objects.get_or_create(user=faculty)
        WalletJoinRequest.objects.update_or_create(
            student=student,
            faculty=faculty,
            defaults={
                "wallet": wallet,
                "status": WalletJoinRequestStatus.APPROVED,
                "responded_at": timezone.now(),
            },
        )

    def _equipments(self, depts):
        internal = [d for d in depts if d.department_type == DepartmentType.INTERNAL]
        cat, _ = EquipmentCategory.objects.get_or_create(
            code="N1-CAT",
            defaults={"name": "N1 Concurrency Category", "description": "Phase N.1 SAT"},
        )
        group, _ = EquipmentGroup.objects.get_or_create(
            code="N1-GRP",
            defaults={"name": "N1 Quota Group", "description": "Phase N.1 quotas"},
        )
        EquipmentGroupQuota.objects.update_or_create(
            equipment_group=group,
            quota_type=QuotaType.WEEKLY,
            defaults={
                "is_enforced": True,
                "internal_individual_quota_minutes": 60,
                "internal_faculty_quota_minutes": 60,
                "external_individual_quota_minutes": 60,
                "external_faculty_quota_minutes": 60,
            },
        )

        specs = [
            # code, name, duration, slots_per_day, waitlist, group, open hours
            ("N1-SINGLE", "N1 Single-User Instrument", 60, 4, 0, None, [(9, 0, 10, 0), (10, 0, 11, 0), (11, 0, 12, 0), (14, 0, 15, 0)]),
            ("N1-MULTI", "N1 Multi-Slot Instrument", 30, 8, 0, None, [(9, 0, 9, 30), (9, 30, 10, 0), (10, 0, 10, 30), (10, 30, 11, 0), (11, 0, 11, 30), (11, 30, 12, 0), (14, 0, 14, 30), (14, 30, 15, 0)]),
            ("N1-QUOTA", "N1 Quota-Controlled Instrument", 30, 6, 0, group, [(9, 0, 9, 30), (9, 30, 10, 0), (10, 0, 10, 30), (10, 30, 11, 0), (11, 0, 11, 30), (11, 30, 12, 0)]),
            ("N1-WAIT", "N1 Waitlist Instrument", 30, 4, 100, None, [(9, 0, 9, 30), (9, 30, 10, 0), (10, 0, 10, 30), (10, 30, 11, 0)]),
            ("N1-OVERLAP", "N1 Overlap Probe Instrument", 60, 3, 0, None, [(9, 0, 10, 0), (10, 0, 11, 0), (11, 0, 12, 0)]),
        ]
        out = []
        for code, name, dur, spd, wl, grp, slots in specs:
            eq, created = Equipment.objects.get_or_create(
                code=code,
                defaults={
                    "name": name,
                    "description": "Phase N.1 concurrency SAT — safe to book/cancel",
                    "status": EquipmentStatus.ACTIVE,
                    "profile_type": EquipmentProfileType.SAMPLE,
                    "category": cat,
                    "internal_department": internal[0],
                    "slot_duration_minutes": dur,
                    "slots_per_day": spd,
                    "location": "N1 SAT Lab",
                    "waitlist_queue_depth": wl,
                    "equipment_group": grp,
                },
            )
            eq.status = EquipmentStatus.ACTIVE
            eq.waitlist_queue_depth = wl
            eq.equipment_group = grp
            eq.slot_duration_minutes = dur
            eq.slots_per_day = spd
            eq.internal_department = internal[0]
            eq.save()
            # Charge profiles for bookable types
            for ut in (UserType.STUDENT, UserType.FACULTY, UserType.EXTERNAL, UserType.OTHER):
                ChargeProfile.objects.get_or_create(
                    equipment=eq,
                    user_type=ut,
                    pricing_profile=ChargeProfilePricingProfile.STANDARD,
                    defaults={
                        "is_active": True,
                        "primary_unit_charge": Decimal("10.00"),
                        "secondary_unit_charge": Decimal("0.00"),
                        "time_formula": "A*30",
                    },
                )
                ChargeProfile.objects.filter(
                    equipment=eq, user_type=ut, pricing_profile=ChargeProfilePricingProfile.STANDARD
                ).update(
                    is_active=True,
                    primary_unit_charge=Decimal("10.00"),
                    time_formula="A*30",
                )
            # Slot masters
            for idx, (oh, om, ch, cm) in enumerate(slots, start=1):
                SlotMaster.objects.update_or_create(
                    equipment=eq,
                    slot_number=idx,
                    defaults={
                        "slot_name": f"N1-S{idx}",
                        "open_time": dt_time(oh, om),
                        "close_time": dt_time(ch, cm),
                        "is_active": True,
                    },
                )
            out.append(eq)
        return out

    def _ensure_slots(self, equipments):
        today = timezone.localdate()
        for eq in equipments:
            for offset in range(0, 14):
                d = today + timedelta(days=offset)
                SlotGenerator.generate_daily_slots(eq, d, allow_holiday=True)
                # Force AVAILABLE for SAT window (ignore holiday NOT_AVAILABLE)
                DailySlot.objects.filter(
                    slot_master__equipment=eq,
                    date=d,
                    booking__isnull=True,
                ).exclude(status=SlotStatus.BOOKED).update(status=SlotStatus.AVAILABLE)

    def _wipe_n1_bookings(self, equipments):
        from iic_booking.equipment.models import Booking, BookingStatus

        eqs = list(equipments)
        qs = Booking.objects.filter(equipment__in=eqs).exclude(
            status__in=[BookingStatus.CANCELLED, BookingStatus.COMPLETED]
        )
        n = qs.count()
        for b in qs.iterator():
            b.status = BookingStatus.CANCELLED
            b.save(update_fields=["status"])
        DailySlot.objects.filter(slot_master__equipment__in=eqs).update(
            booking=None, status=SlotStatus.AVAILABLE
        )
        self.stdout.write(self.style.WARNING(f"Wiped/cancelled {n} active N1 bookings; slots reset."))

    def _user_manifest(self):
        rows = []
        for u in User.objects.filter(email__startswith=N1_PREFIX).order_by("email"):
            rows.append(
                {
                    "id": u.id,
                    "email": u.email,
                    "user_type": u.user_type,
                    "department_id": u.department_id,
                }
            )
        return rows

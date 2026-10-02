import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.models import (
    DailySlot,
    Equipment,
    EquipmentManager,
    EquipmentOperator,
    EquipmentTemporaryOIC,
    SlotMaster,
    SlotStatus,
)
from iic_booking.training.models import BadgeDefinition, CertificationLevel, TrainingPolicy
from iic_booking.users.models.department import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import SubWallet, Wallet, WalletJoinRequest, WalletJoinRequestStatus
from iic_booking.users.tests.factories import UserFactory

API = "/api/v1/training"


@pytest.fixture(autouse=True)
def training_settings(settings):
    settings.TRAINING_MODULE_ENABLED = True
    settings.TRAINING_PILOT_EQUIPMENT_CODES = ""
    settings.TRAINING_PILOT_OIC_EMAILS = ""
    return settings


@pytest.fixture(autouse=True)
def seed_levels(db):
    trained, _ = CertificationLevel.objects.get_or_create(
        code="TRAINED", defaults={"name": "Trained", "rank": 10, "default_validity_months": 24, "is_active": True}
    )
    CertificationLevel.objects.get_or_create(code="CERT_L1", defaults={"name": "Certified L1", "rank": 20, "is_active": False})
    BadgeDefinition.objects.get_or_create(code="trained", defaults={"name": "Trained", "level": trained})
    if not TrainingPolicy.objects.filter(scope="GLOBAL").exists():
        TrainingPolicy.objects.create(scope="GLOBAL", version=1, is_active=True)
    return trained


def client_for(user=None):
    c = APIClient()
    if user is not None:
        c.force_authenticate(user=user)
    return c


def make_user(**kwargs):
    kwargs.setdefault("email_verified", True)
    kwargs.setdefault("admin_approved", True)
    kwargs.setdefault("email", f"u{uuid.uuid4().hex[:10]}@iitr.ac.in")
    return UserFactory(**kwargs)


def make_department(department_type="internal", name=None):
    tag = uuid.uuid4().hex[:6].upper()
    return Department.objects.create(
        name=name or f"TR-Dept-{tag}",
        code=f"TR{tag[:4]}",
        department_type=department_type,
        equipment_booking_enabled=True,
        equipment_visibility_enabled=True,
    )


def make_equipment(department=None, **kwargs):
    defaults = {
        "name": f"TR EQ {uuid.uuid4().hex[:4]}",
        "code": f"TR{uuid.uuid4().hex[:5].upper()}",
        "slot_duration_minutes": 60,
        "user_rating_enabled": False,
        "status": "ACTIVE",
        "internal_department": department,
    }
    defaults.update(kwargs)
    return Equipment.objects.create(**defaults)


def future_day(days=10):
    return timezone.localdate() + timedelta(days=days)


def make_slots(equipment, day=None, hours=range(9, 17), status=SlotStatus.AVAILABLE):
    day = day or future_day()
    tz = timezone.get_current_timezone()
    slots = []
    for h in hours:
        master, _ = SlotMaster.objects.get_or_create(
            equipment=equipment,
            slot_number=h,
            defaults={"slot_name": f"S{h}", "open_time": time(h, 0), "close_time": time(h + 1, 0), "is_active": True},
        )
        start = timezone.make_aware(datetime.combine(day, time(h, 0)), tz)
        slots.append(
            DailySlot.objects.create(
                slot_master=master, date=day, start_datetime=start, end_datetime=start + timedelta(hours=1), status=status
            )
        )
    return slots


def at(day, hour, minute=0):
    return timezone.make_aware(datetime.combine(day, time(hour, minute)), timezone.get_current_timezone())


class World:
    pass


@pytest.fixture
def world(db):
    w = World()
    w.dept = make_department(name="Chemistry")
    w.other_dept = make_department(name="Physics")
    w.equipment = make_equipment(w.dept, name="FE-SEM", code=f"FESEM{uuid.uuid4().hex[:3].upper()}")
    w.other_equipment = make_equipment(w.other_dept, name="XRD")
    w.admin = make_user(user_type=UserType.ADMIN, name="Main Admin")
    w.oic = make_user(user_type=UserType.MANAGER, name="OIC One")
    w.other_oic = make_user(user_type=UserType.MANAGER, name="OIC Two")
    w.temp_oic = make_user(user_type=UserType.MANAGER, name="Temp OIC")
    w.operator = make_user(user_type=UserType.OPERATOR, name="Lab Operator")
    w.faculty = make_user(user_type=UserType.FACULTY, department=w.dept, name="Prof. Asha Rao")
    w.faculty2 = make_user(user_type=UserType.FACULTY, department=w.other_dept, name="Prof. B Iyer")
    w.student = make_user(user_type=UserType.STUDENT, department=w.dept, supervisor=w.faculty, name="Student One")
    w.student2 = make_user(user_type=UserType.STUDENT, department=w.other_dept, supervisor=w.faculty2, name="Student Two")
    w.outsider = make_user(user_type=UserType.STUDENT, department=w.dept, name="Not In Group")
    EquipmentManager.objects.create(equipment=w.equipment, manager=w.oic)
    EquipmentManager.objects.create(equipment=w.other_equipment, manager=w.other_oic)
    EquipmentTemporaryOIC.objects.create(
        equipment=w.equipment, primary_oic=w.oic, temporary_oic=w.temp_oic, resume_at=timezone.now() + timedelta(days=5)
    )
    EquipmentOperator.objects.create(equipment=w.equipment, operator=w.operator, role=EquipmentOperator.Role.PRIMARY)
    return w


def fund_faculty(faculty, department, amount=Decimal("5000.00")):
    wallet, _ = Wallet.objects.get_or_create(user=faculty)
    sub, _ = SubWallet.objects.get_or_create(wallet=wallet, department=department, defaults={"balance": amount})
    if sub.balance != amount:
        SubWallet.objects.filter(pk=sub.pk).update(balance=amount)
        sub.refresh_from_db()
    return sub


def join_wallet(student, faculty):
    wallet, _ = Wallet.objects.get_or_create(user=faculty)
    return WalletJoinRequest.objects.create(student=student, faculty=faculty, wallet=wallet, status=WalletJoinRequestStatus.APPROVED)

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.models import Equipment, EquipmentManager, EquipmentOperator, EquipmentTemporaryOIC
from iic_booking.procurement_management import config_service
from iic_booking.procurement_management import constants as c
from iic_booking.procurement_management.models import ProcurementManagementConfiguration
from iic_booking.users.models.department import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

API = "/api/v1/procurement"


def client_for(user=None):
    cl = APIClient()
    if user is not None:
        cl.force_authenticate(user=user)
    return cl


def make_user(**kwargs):
    kwargs.setdefault("email_verified", True)
    kwargs.setdefault("admin_approved", True)
    kwargs.setdefault("email", f"pm{uuid.uuid4().hex[:10]}@iitr.ac.in")
    return UserFactory(**kwargs)


def make_department(name=None):
    tag = uuid.uuid4().hex[:6].upper()
    return Department.objects.create(name=name or f"PM-Dept-{tag}", code=f"PM{tag[:4]}", department_type="internal")


def make_equipment(department, **kwargs):
    defaults = {
        "name": f"PM EQ {uuid.uuid4().hex[:4]}",
        "code": f"PM{uuid.uuid4().hex[:6].upper()}",
        "slot_duration_minutes": 60,
        "user_rating_enabled": False,
        "status": "ACTIVE",
        "internal_department": department,
    }
    defaults.update(kwargs)
    return Equipment.objects.create(**defaults)


def enable(department, admin, **overrides):
    """Enable the module for general use (pilot mode off); pilot behaviour has its own tests."""
    return config_service.update_config(admin, department, {"module_enabled": True, "pilot_mode": False, **overrides})


class World:
    pass


@pytest.fixture
def world(db):
    w = World()
    w.dept = make_department("Chemistry PM")
    w.other_dept = make_department("Physics PM")
    w.equipment = make_equipment(w.dept, name="FE-SEM")
    w.equipment2 = make_equipment(w.dept, name="XRD")
    w.other_equipment = make_equipment(w.other_dept, name="TEM")
    w.admin = make_user(user_type=UserType.ADMIN, name="Main Admin")
    w.oic = make_user(user_type=UserType.MANAGER, name="OIC One", department=w.dept)
    w.oic2 = make_user(user_type=UserType.MANAGER, name="OIC Two", department=w.dept)
    w.other_oic = make_user(user_type=UserType.MANAGER, name="Other OIC", department=w.other_dept)
    w.operator = make_user(user_type=UserType.OPERATOR, name="Lab Operator", department=w.dept)
    w.operator2 = make_user(user_type=UserType.OPERATOR, name="Lab Operator Two", department=w.dept)
    w.other_operator = make_user(user_type=UserType.OPERATOR, name="Other Operator", department=w.other_dept)
    w.stores = make_user(user_type=UserType.OPERATOR, name="OC Stores", department=w.dept)
    w.office = make_user(user_type=UserType.FINANCE, name="Office Clerk", department=w.dept)
    w.hod = make_user(user_type=UserType.HOD, name="Prof. HOD", department=w.dept)
    w.auditor = make_user(user_type=UserType.FINANCE, name="Auditor", department=w.dept)
    w.outsider = make_user(user_type=UserType.STUDENT, name="Student", department=w.dept)
    EquipmentManager.objects.create(equipment=w.equipment, manager=w.oic)
    EquipmentManager.objects.create(equipment=w.equipment2, manager=w.oic2)
    EquipmentManager.objects.create(equipment=w.other_equipment, manager=w.other_oic)
    EquipmentOperator.objects.create(equipment=w.equipment, operator=w.operator, role=EquipmentOperator.Role.PRIMARY)
    EquipmentOperator.objects.create(equipment=w.equipment2, operator=w.operator2, role=EquipmentOperator.Role.PRIMARY)
    EquipmentOperator.objects.create(
        equipment=w.other_equipment, operator=w.other_operator, role=EquipmentOperator.Role.PRIMARY
    )
    w.dept.head = w.hod
    w.dept.save(update_fields=["head"])
    w.cfg = enable(w.dept, w.admin)
    enable(w.other_dept, w.admin)
    config_service.assign_role(w.admin, w.dept, w.stores, c.ModuleRole.OC_STORES)
    config_service.assign_role(w.admin, w.dept, w.office, c.ModuleRole.OFFICE)
    config_service.assign_role(w.admin, w.dept, w.auditor, c.ModuleRole.AUDITOR)
    return w


@pytest.fixture
def temp_oic(world):
    user = make_user(user_type=UserType.MANAGER, name="Temp OIC")
    EquipmentTemporaryOIC.objects.create(
        equipment=world.equipment, primary_oic=world.oic, temporary_oic=user, resume_at=timezone.now() + timedelta(days=3)
    )
    return user


def config_of(dept) -> ProcurementManagementConfiguration:
    return ProcurementManagementConfiguration.objects.get(department=dept)


def category(dept, code):
    from iic_booking.procurement_management.models import ItemCategory

    return ItemCategory.objects.get(department=dept, code=code)


def request_type(dept, code):
    from iic_booking.procurement_management.models import RequestTypeConfig

    return RequestTypeConfig.objects.get(department=dept, code=code)


def line(price="1000.00", qty="1", gst="0", **extra):
    return {"description": extra.pop("description", "Item"), "quantity": qty, "estimated_unit_price": price, "gst_rate": gst, **extra}


def new_request(user, *, equipment=None, rt="CONSUMABLE", cat=None, lines=None, submit=False, expect=201, **extra):
    from iic_booking.procurement_management.models import PurchaseRequest

    body = {
        "title": extra.pop("title", "Request"),
        "justification": extra.pop("justification", "Needed for experiments"),
        "request_type": rt,
        "lines": lines if lines is not None else [line()],
        "submit": submit,
        **extra,
    }
    if equipment is not None:
        body["equipment_id"] = equipment.pk
    if cat is not None:
        body["category_id"] = cat.pk
    res = client_for(user).post(f"{API}/requests/", body, format="json")
    assert res.status_code == expect, res.json()
    if expect != 201:
        return res
    return PurchaseRequest.objects.get(pk=res.json()["id"])


def act(user, r, action, expect=200, **body):
    res = client_for(user).post(f"{API}/requests/{r.pk}/{action}/", body, format="json")
    assert res.status_code == expect, res.json()
    r.refresh_from_db()
    return res


def pdf_upload(name="signed.pdf", content=b"%PDF-1.4\n%test\n"):
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile(name, content, content_type="application/pdf")


D = Decimal

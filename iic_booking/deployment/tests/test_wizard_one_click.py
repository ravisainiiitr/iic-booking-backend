"""One-click Equipment PC Wizard download: per-equipment file name + trailer."""

from __future__ import annotations

import datetime
import json
import struct

import pytest
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from rest_framework.test import APIClient

from iic_booking.deployment.models import EquipmentPcWizardRelease
from iic_booking.equipment.models import Equipment
from iic_booking.users.models import Department
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.user_type import UserType


def _parse(blob: bytes) -> dict:
    assert blob.endswith(b"IICEPCW1")
    (length,) = struct.unpack("<I", blob[-12:-8])
    return json.loads(blob[-12 - length : -12])


@pytest.fixture
def main_admin(db):
    user = get_user_model().objects.create_user(
        email="wiz-oc-admin@example.com", password="test-pass-12345", user_type=UserType.ADMIN, name="Main Admin",
    )
    user.admin_approved = True
    user.save()
    return user


@pytest.fixture
def equipment(db):
    dept = Department.objects.create(name="IIC", code="IICWIZ", department_type=DepartmentType.INTERNAL)
    return Equipment.objects.create(name="DSA Test Equipment", code="DSATEST", internal_department=dept)


@pytest.fixture
def wizard_release(db, settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    settings.STORAGES = {**settings.STORAGES, "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"}}
    rel = EquipmentPcWizardRelease.objects.create(
        version="1.0.7", release_date=datetime.date(2026, 11, 13), is_latest=True, is_active=True,
    )
    rel.file.save("EquipmentWizard.exe", ContentFile(b"MZ-wizard"), save=True)
    return rel


@pytest.mark.django_db
def test_per_equipment_wizard_download(main_admin, equipment, wizard_release):
    client = APIClient()
    client.force_authenticate(main_admin)
    ticket = client.post(
        "/api/v1/deployment/wizard/releases/latest/one-click-ticket/",
        {"equipment_id": equipment.pk, "data_folder": r"D:\DSATEST"},
        format="json",
    )
    assert ticket.status_code == 200, ticket.content
    assert ticket.json()["filename"] == "EquipmentWizard-DSATEST.exe"

    path = "/" + ticket.json()["url"].split("://", 1)[-1].split("/", 1)[-1]
    resp = APIClient().get(path)
    assert resp.status_code == 200
    blob = b"".join(resp.streaming_content)
    assert blob.startswith(b"MZ-wizard")
    assert _parse(blob) == {"v": 1, "equipment_code": "DSATEST", "data_folder": r"D:\DSATEST"}
    assert "EquipmentWizard-DSATEST.exe" in resp["Content-Disposition"]


@pytest.mark.django_db
def test_rejects_unc_data_folder(main_admin, equipment, wizard_release):
    client = APIClient()
    client.force_authenticate(main_admin)
    resp = client.post(
        "/api/v1/deployment/wizard/releases/latest/one-click-ticket/",
        {"equipment_id": equipment.pk, "data_folder": "\\server\\share"},
        format="json",
    )
    assert resp.status_code == 400

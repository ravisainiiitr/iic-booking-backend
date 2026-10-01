"""OIC-formatted important instruction: HTML is sanitized, and per-user-type text overrides the default."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from iic_booking.equipment.models import EquipmentManager
from iic_booking.equipment.rich_text import rich_text_to_plain, sanitize_rich_text
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


def _client(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def test_sanitizer_keeps_formatting_and_drops_scripts():
    dirty = (
        '<p style="color: rgb(185, 28, 28); font-family: Georgia; position: fixed">'
        '<b onclick="steal()">Dry</b> <font size="5" face="Arial" onmouseover="x()">samples</font></p>'
        '<script>alert(1)</script><img src=x onerror=alert(1)><a href="javascript:x">link</a>'
    )
    clean = sanitize_rich_text(dirty)
    assert clean == (
        '<p style="color: rgb(185, 28, 28); font-family: Georgia"><b>Dry</b> '
        '<font size="5" face="Arial">samples</font></p>link'
    )
    assert rich_text_to_plain(clean) == "Dry samples\nlink"
    assert sanitize_rich_text("  a < b\r\nc  ") == "a < b\nc"


@pytest.mark.django_db
def test_per_user_type_instruction_saved_and_resolved(egs_factory):
    equipment = egs_factory.equipment()
    oic = UserFactory(user_type=UserType.MANAGER, admin_approved=True)
    EquipmentManager.objects.create(equipment=equipment, manager=oic)
    url = f"/api/oic/equipment-settings/{equipment.pk}/"

    listing = _client(oic).get("/api/oic/equipment-settings/").data
    assert {"value": UserType.STUDENT, "label": "IITR Student"} in listing["instruction_user_types"]
    assert all(o["value"] not in (UserType.MANAGER, UserType.ADMIN) for o in listing["instruction_user_types"])

    res = _client(oic).patch(
        url,
        {
            "important_instruction": "<p><b>Default</b> note</p>",
            "important_instruction_by_user_type": {
                UserType.STUDENT: '<p style="color: #b91c1c">Students: bring ID</p><script>x()</script>',
                UserType.FACULTY: "   ",
            },
        },
        format="json",
    )
    assert res.status_code == 200, res.data
    settings = res.data["equipment"]["settings"]
    assert settings["important_instruction"] == "<p><b>Default</b> note</p>"
    assert settings["important_instruction_by_user_type"] == {
        UserType.STUDENT: '<p style="color: #b91c1c">Students: bring ID</p>'
    }

    student = egs_factory.student()
    faculty = UserFactory(user_type=UserType.FACULTY, admin_approved=True)
    detail = f"/api/equipments/{equipment.pk}/"
    assert _client(student).get(detail).data["important_instruction"] == (
        '<p style="color: #b91c1c">Students: bring ID</p>'
    )
    assert _client(faculty).get(detail).data["important_instruction"] == "<p><b>Default</b> note</p>"
    assert _client(oic).get(detail, {"all_input_fields": "1"}).data["important_instruction"] == (
        "<p><b>Default</b> note</p>"
    )

    bad = _client(oic).patch(url, {"important_instruction_by_user_type": {UserType.ADMIN: "x"}}, format="json")
    assert bad.status_code == 400
    assert set(bad.data["errors"]) == {"important_instruction_by_user_type"}

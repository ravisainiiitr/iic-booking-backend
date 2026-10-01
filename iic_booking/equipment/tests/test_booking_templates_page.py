"""Booking templates dashboard page: all-equipment list, readable summaries and bookable-equipment checks."""

from __future__ import annotations

import pytest

from iic_booking.equipment.models import BookingInputTemplate, DynamicInputField

URL = "/api/booking-templates/"


def _field(eq, key, label, field_type="TEXT", user_type=""):
    return DynamicInputField.objects.create(
        equipment=eq, user_type=user_type, field_key=key, field_label=label, field_type=field_type
    )


@pytest.mark.django_db
def test_list_covers_every_equipment_with_department_and_summary(egs_factory):
    eq = egs_factory.equipment(name="TGA Analyzer")
    other = egs_factory.equipment(name="XRD")
    _field(eq, "A", "Number of samples", "NUMERIC")
    _field(eq, "B", "Gas", "RADIO")
    _field(eq, "C", "Elements", "PERIODIC_TABLE")
    _field(eq, "D", "Sample table", "TABLE")
    _field(eq, "B", "Gas (student)", "RADIO", user_type="student")
    user = egs_factory.student()
    BookingInputTemplate.objects.create(
        user=user,
        equipment=eq,
        name="Routine",
        input_values={
            "A": "3",
            "B": "N2",
            "C": 2,
            "C_elements": "Fe,Cu",
            "D": [["1", "PVC"], ["2", ""], ["", ""]],
            "comments": "fragile",
            "_sample_sets": [{"A": "1"}, {"A": "2"}],
        },
    )
    BookingInputTemplate.objects.create(user=user, equipment=other, name="Quick scan")

    resp = egs_factory.client_for(user).get(URL)
    assert resp.status_code == 200
    by_name = {t["name"]: t for t in resp.data["templates"]}
    assert set(by_name) == {"Routine", "Quick scan"}
    routine = by_name["Routine"]
    assert routine["equipment_name"] == "TGA Analyzer"
    assert routine["department_id"] == egs_factory.department.pk
    assert routine["department_name"] == egs_factory.department.name
    assert routine["department_code"] == egs_factory.department.code
    assert routine["equipment_status"] == "ACTIVE"
    assert routine["sample_set_count"] == 3
    assert routine["bookable"] is True
    assert routine["booking_block_reason"] is None
    assert routine["input_summary"] == [
        {"key": "A", "label": "Number of samples", "value": "3"},
        {"key": "B", "label": "Gas (student)", "value": "N2"},
        {"key": "C", "label": "Elements", "value": "Fe, Cu"},
        {"key": "D", "label": "Sample table", "value": "2 rows"},
    ]
    assert by_name["Quick scan"]["sample_set_count"] == 1
    assert by_name["Quick scan"]["input_summary"] == []


@pytest.mark.django_db
def test_list_filters_by_department_and_rejects_bad_ids(egs_factory):
    from iic_booking.users.models import Department

    eq = egs_factory.equipment()
    elsewhere_dept = Department.objects.create(
        name="Other Dept", code="OTHD", equipment_booking_enabled=True, equipment_visibility_enabled=True
    )
    elsewhere = egs_factory.equipment(internal_department=elsewhere_dept)
    user = egs_factory.student()
    BookingInputTemplate.objects.create(user=user, equipment=eq, name="Here")
    BookingInputTemplate.objects.create(user=user, equipment=elsewhere, name="There")
    client = egs_factory.client_for(user)

    resp = client.get(URL, {"department": elsewhere_dept.pk})
    assert [t["name"] for t in resp.data["templates"]] == ["There"]
    assert client.get(URL, {"department": "x"}).status_code == 400


@pytest.mark.django_db
def test_create_with_only_an_equipment_id_returns_page_fields(egs_factory):
    eq = egs_factory.equipment()
    _field(eq, "A", "Samples", "NUMERIC")
    client = egs_factory.client_for(egs_factory.student())

    resp = client.post(URL, {"equipment": eq.pk, "name": "Fresh", "input_values": {"A": "2"}}, format="json")
    assert resp.status_code == 201, resp.data
    assert resp.data["department_name"] == egs_factory.department.name
    assert resp.data["input_summary"] == [{"key": "A", "label": "Samples", "value": "2"}]
    assert resp.data["bookable"] is True
    assert resp.data["options"] == {}
    assert resp.data["preferred_slot"] is None


@pytest.mark.django_db
def test_create_refused_when_department_booking_is_disabled(egs_factory):
    eq = egs_factory.equipment()
    egs_factory.department.equipment_booking_enabled = False
    egs_factory.department.save(update_fields=["equipment_booking_enabled"])
    client = egs_factory.client_for(egs_factory.student())

    resp = client.post(URL, {"equipment": eq.pk, "name": "Nope"}, format="json")
    assert resp.status_code == 403
    assert resp.data["code"] == "equipment_not_bookable"
    assert not BookingInputTemplate.objects.exists()


@pytest.mark.django_db
def test_create_refused_for_equipment_hidden_from_the_user(egs_factory):
    from iic_booking.users.models.user_group import UserGroup, UserGroupMember

    group = UserGroup.objects.create(name="Restricted", code="RESTRICTED-TPL")
    eq = egs_factory.equipment(visibility_group=group)
    outsider = egs_factory.student()
    member = egs_factory.student()
    UserGroupMember.objects.create(user_group=group, user=member)

    refused = egs_factory.client_for(outsider).post(URL, {"equipment": eq.pk, "name": "Hidden"}, format="json")
    assert refused.status_code == 403
    allowed = egs_factory.client_for(member).post(URL, {"equipment": eq.pk, "name": "Visible"}, format="json")
    assert allowed.status_code == 201, allowed.data


@pytest.mark.django_db
def test_existing_template_is_flagged_when_equipment_is_not_operational(egs_factory):
    eq = egs_factory.equipment(status="REPAIR")
    user = egs_factory.student()
    template = BookingInputTemplate.objects.create(user=user, equipment=eq, name="Later")
    client = egs_factory.client_for(user)

    listed = client.get(URL).data["templates"][0]
    assert listed["bookable"] is False
    assert "not operational" in listed["booking_block_reason"]
    detail = client.get(f"{URL}{template.pk}/")
    assert detail.status_code == 200
    assert detail.data["bookable"] is False


@pytest.mark.django_db
def test_other_users_templates_never_appear_or_change(egs_factory):
    eq = egs_factory.equipment()
    owner = egs_factory.student()
    template = BookingInputTemplate.objects.create(user=owner, equipment=eq, name="Owner's")
    stranger = egs_factory.client_for(egs_factory.student())

    assert stranger.get(URL, {"equipment": eq.pk}).data["templates"] == []
    assert stranger.get(URL, {"department": egs_factory.department.pk}).data["templates"] == []
    assert stranger.put(
        f"{URL}{template.pk}/", {"name": "Mine now", "input_values": {}, "options": {}}, format="json"
    ).status_code == 404
    template.refresh_from_db()
    assert template.name == "Owner's"

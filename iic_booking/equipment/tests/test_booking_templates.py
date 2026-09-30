"""Named booking templates: saved booking inputs and options per user and equipment."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from iic_booking.equipment.booking_templates import MAX_TEMPLATES_PER_EQUIPMENT
from iic_booking.equipment.models import BookingInputTemplate

URL = "/api/booking-templates/"


def _payload(eq, **overrides):
    data = {
        "equipment": eq.pk,
        "name": "Routine TGA run",
        "input_values": {"A": "2", "B": "1", "gas": "N2"},
        "options": {
            "auto_slot_selection": True,
            "book_any_available_slots": True,
            "book_even_if_single_slot_available": False,
            "waitlist_on_failure": True,
            "auto_allocate_alternative": True,
        },
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
def test_create_and_list_own_templates(egs_factory):
    eq = egs_factory.equipment()
    other_eq = egs_factory.equipment()
    user = egs_factory.student()
    client = egs_factory.client_for(user)

    resp = client.post(URL, _payload(eq), format="json")
    assert resp.status_code == 201, resp.data
    assert resp.data["name"] == "Routine TGA run"
    assert resp.data["input_values"] == {"A": "2", "B": "1", "gas": "N2"}
    assert resp.data["options"]["book_any_available_slots"] is True
    assert resp.data["equipment_code"] == eq.code
    client.post(URL, _payload(other_eq, name="Other"), format="json")

    listed = client.get(URL, {"equipment": eq.pk})
    assert listed.status_code == 200
    assert [t["name"] for t in listed.data["templates"]] == ["Routine TGA run"]
    assert len(client.get(URL).data["templates"]) == 2


@pytest.mark.django_db
def test_templates_are_private_to_their_owner(egs_factory):
    eq = egs_factory.equipment()
    owner = egs_factory.student()
    stranger = egs_factory.student()
    template = BookingInputTemplate.objects.create(user=owner, equipment=eq, name="Mine")
    other = egs_factory.client_for(stranger)

    assert other.get(URL).data["templates"] == []
    assert other.get(f"{URL}{template.pk}/").status_code == 404
    assert other.patch(f"{URL}{template.pk}/", {"name": "Taken"}, format="json").status_code == 404
    assert other.delete(f"{URL}{template.pk}/").status_code == 404
    template.refresh_from_db()
    assert template.name == "Mine"


@pytest.mark.django_db
def test_login_required():
    assert APIClient().get(URL).status_code in (401, 403)


@pytest.mark.django_db
def test_duplicate_name_is_rejected_case_insensitively(egs_factory):
    eq = egs_factory.equipment()
    user = egs_factory.student()
    client = egs_factory.client_for(user)
    client.post(URL, _payload(eq), format="json")

    resp = client.post(URL, _payload(eq, name="  routine   tga RUN "), format="json")
    assert resp.status_code == 400
    assert "already have a template" in resp.data["error"]

    other_user = egs_factory.client_for(egs_factory.student())
    assert other_user.post(URL, _payload(eq), format="json").status_code == 201


@pytest.mark.django_db
def test_unknown_or_non_boolean_options_are_dropped(egs_factory):
    eq = egs_factory.equipment()
    client = egs_factory.client_for(egs_factory.student())

    resp = client.post(
        URL,
        _payload(eq, options={"auto_slot_selection": False, "book_any_available_slots": "yes", "total_charge": 1}),
        format="json",
    )
    assert resp.status_code == 201
    assert resp.data["options"] == {"auto_slot_selection": False}


@pytest.mark.django_db
def test_research_workspace_choice_is_saved(egs_factory):
    eq = egs_factory.equipment()
    client = egs_factory.client_for(egs_factory.student())
    workspace_id = "3f2b8c1e-5d4a-4f6b-9c2e-1a7d0e9b4c21"

    resp = client.post(URL, _payload(eq, options={"research_workspace": f" {workspace_id} "}), format="json")
    assert resp.status_code == 201, resp.data
    assert resp.data["options"] == {"research_workspace": workspace_id}

    cleared = client.patch(f"{URL}{resp.data['id']}/", {"options": {"research_workspace": None}}, format="json")
    assert cleared.status_code == 200
    assert cleared.data["options"] == {"research_workspace": None}

    for bad in (123, "x" * 65, ["id"]):
        rejected = client.post(URL, _payload(eq, name=f"Bad {bad!r}"[:40], options={"research_workspace": bad}), format="json")
        assert rejected.status_code == 400


@pytest.mark.django_db
def test_validation_errors(egs_factory):
    eq = egs_factory.equipment()
    client = egs_factory.client_for(egs_factory.student())

    assert client.post(URL, _payload(eq, name="   "), format="json").status_code == 400
    assert client.post(URL, _payload(eq, name="x" * 81), format="json").status_code == 400
    assert client.post(URL, _payload(eq, input_values=["A"]), format="json").status_code == 400
    assert client.post(URL, _payload(eq, input_values={"A": "x" * 60_000}), format="json").status_code == 400
    assert client.post(URL, _payload(eq, equipment=987654), format="json").status_code == 404


@pytest.mark.django_db
def test_update_rename_and_delete(egs_factory):
    eq = egs_factory.equipment()
    user = egs_factory.student()
    client = egs_factory.client_for(user)
    first = client.post(URL, _payload(eq), format="json").data
    client.post(URL, _payload(eq, name="Second"), format="json")

    resp = client.patch(
        f"{URL}{first['id']}/",
        {"name": "Renamed", "input_values": {"A": "3"}, "options": {"waitlist_on_failure": False}},
        format="json",
    )
    assert resp.status_code == 200, resp.data
    assert resp.data["name"] == "Renamed"
    assert resp.data["input_values"] == {"A": "3"}
    assert resp.data["options"] == {"waitlist_on_failure": False}

    clash = client.patch(f"{URL}{first['id']}/", {"name": "second"}, format="json")
    assert clash.status_code == 400

    assert client.delete(f"{URL}{first['id']}/").status_code == 204
    assert not BookingInputTemplate.objects.filter(pk=first["id"]).exists()


@pytest.mark.django_db
def test_per_equipment_limit(egs_factory):
    eq = egs_factory.equipment()
    user = egs_factory.student()
    BookingInputTemplate.objects.bulk_create(
        [BookingInputTemplate(user=user, equipment=eq, name=f"T{i}") for i in range(MAX_TEMPLATES_PER_EQUIPMENT)]
    )

    resp = egs_factory.client_for(user).post(URL, _payload(eq, name="One more"), format="json")
    assert resp.status_code == 400
    assert "up to" in resp.data["error"]

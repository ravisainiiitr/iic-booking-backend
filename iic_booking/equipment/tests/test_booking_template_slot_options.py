"""One way to choose slots and one "if my slots are taken" choice per booking template.

Older templates saved with contradictory flags are read consistently without rewriting their rows, and new
writes are normalised the same way.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from iic_booking.equipment.booking_templates import normalise_slot_options
from iic_booking.equipment.models import BookingInputTemplate
from iic_booking.equipment.template_slot_preference import effective_if_slot_taken, resolve_preferred_slot

from .test_booking_template_preferred_slot import _book, _student_with_wallet, _template, no_portal_lock  # noqa: F401

URL = "/api/booking-templates/"
PREFERRED = {"weekday": 3, "start_time": "11:00"}


def test_normalise_slot_options_rules():
    both = {"auto_slot_selection": True, "book_any_available_slots": False, "book_even_if_single_slot_available": True}
    assert normalise_slot_options(both, has_preferred=True) == {
        "auto_slot_selection": False, "book_any_available_slots": False, "book_even_if_single_slot_available": False,
    }
    # Without a preferred slot auto-select stays; "single slot" stays only with "any free slots".
    ok = {"auto_slot_selection": True, "book_any_available_slots": True, "book_even_if_single_slot_available": True}
    assert normalise_slot_options(ok, has_preferred=False) == ok
    assert normalise_slot_options(None, has_preferred=True) == {}
    assert normalise_slot_options({"waitlist_on_failure": True}, has_preferred=True) == {"waitlist_on_failure": True}


@pytest.mark.django_db
def test_auto_select_is_turned_off_when_a_preferred_slot_is_saved(egs_factory):
    eq = egs_factory.equipment()
    client = egs_factory.client_for(egs_factory.student())
    resp = client.post(
        URL,
        {"equipment": eq.pk, "name": "Pref", "options": {"auto_slot_selection": True}, "preferred_slot": PREFERRED},
        format="json",
    )
    assert resp.status_code == 201, resp.data
    assert resp.data["options"]["auto_slot_selection"] is False
    assert BookingInputTemplate.objects.get(pk=resp.data["id"]).options["auto_slot_selection"] is False

    # Adding a preferred slot later to a template that auto-selects also turns auto-select off.
    plain = client.post(
        URL, {"equipment": eq.pk, "name": "Auto", "options": {"auto_slot_selection": True}}, format="json"
    ).data
    assert plain["options"]["auto_slot_selection"] is True
    patched = client.patch(f"{URL}{plain['id']}/", {"preferred_slot": PREFERRED}, format="json")
    assert patched.status_code == 200
    assert patched.data["options"]["auto_slot_selection"] is False
    assert BookingInputTemplate.objects.get(pk=plain["id"]).options["auto_slot_selection"] is False


@pytest.mark.django_db
def test_single_slot_without_any_free_slots_is_dropped(egs_factory):
    eq = egs_factory.equipment()
    client = egs_factory.client_for(egs_factory.student())
    resp = client.post(
        URL,
        {"equipment": eq.pk, "name": "S", "options": {"book_even_if_single_slot_available": True}},
        format="json",
    )
    assert resp.status_code == 201
    assert resp.data["options"]["book_even_if_single_slot_available"] is False


@pytest.mark.django_db
def test_any_free_slots_replaces_the_preferred_slot_fallback_on_save(egs_factory):
    eq = egs_factory.equipment()
    client = egs_factory.client_for(egs_factory.student())
    resp = client.post(
        URL,
        {
            "equipment": eq.pk, "name": "Both", "preferred_slot": PREFERRED,
            "options": {"book_any_available_slots": True},
            "if_slot_taken": "next_available_same_day", "auto_book_consent": True,
        },
        format="json",
    )
    assert resp.status_code == 201, resp.data
    assert resp.data["if_slot_taken"] == "ask"
    assert resp.data["if_slot_taken_consented_at"] is None
    t = BookingInputTemplate.objects.get(pk=resp.data["id"])
    assert (t.if_slot_taken, t.if_slot_taken_consented_at) == ("ask", None)

    # Switching an automatic template to "any free slots" by editing only the options also resets it.
    auto = client.post(
        URL,
        {
            "equipment": eq.pk, "name": "Auto", "preferred_slot": PREFERRED,
            "if_slot_taken": "next_available_any", "auto_book_consent": True,
        },
        format="json",
    ).data
    assert auto["if_slot_taken"] == "next_available_any"
    patched = client.patch(f"{URL}{auto['id']}/", {"options": {"book_any_available_slots": True}}, format="json")
    assert patched.data["if_slot_taken"] == "ask"
    assert BookingInputTemplate.objects.get(pk=auto["id"]).if_slot_taken == "ask"


@pytest.mark.django_db
def test_legacy_contradictory_template_is_read_consistently_without_rewriting(egs_factory):
    eq = egs_factory.equipment()
    user = egs_factory.student()
    start = egs_factory.future(days=3, hour=10)
    legacy = _template(user, eq, start, if_slot_taken="next_available_same_day", consented=True)
    legacy.options = {
        "auto_slot_selection": True, "book_any_available_slots": True, "book_even_if_single_slot_available": True,
    }
    legacy.save()
    stored = BookingInputTemplate.objects.get(pk=legacy.pk)

    data = egs_factory.client_for(user).get(f"{URL}{legacy.pk}/").data
    assert data["options"]["auto_slot_selection"] is False
    assert data["options"]["book_any_available_slots"] is True
    assert data["options"]["book_even_if_single_slot_available"] is True
    assert data["if_slot_taken"] == "ask"
    assert data["if_slot_taken_consented_at"] is None
    assert effective_if_slot_taken(stored) == "ask"

    # Reading does not touch the row.
    after = BookingInputTemplate.objects.get(pk=legacy.pk)
    assert after.options == stored.options
    assert after.if_slot_taken == "next_available_same_day"
    assert after.if_slot_taken_consented_at == stored.if_slot_taken_consented_at
    assert after.updated_at == stored.updated_at


@pytest.mark.django_db
def test_legacy_template_with_only_a_preferred_fallback_is_unchanged(egs_factory):
    eq = egs_factory.equipment()
    user = egs_factory.student()
    start = egs_factory.future(days=3, hour=10)
    t = _template(user, eq, start, if_slot_taken="next_available_any", consented=True)
    data = egs_factory.client_for(user).get(f"{URL}{t.pk}/").data
    assert data["if_slot_taken"] == "next_available_any"
    assert data["if_slot_taken_consented_at"]


@pytest.mark.django_db
def test_preview_skips_auto_next_when_any_free_slots_wins(egs_factory):
    eq = egs_factory.equipment()
    user = egs_factory.student()
    start = egs_factory.future(days=3, hour=10)
    egs_factory.booking(egs_factory.student(), eq, start)
    egs_factory.slot(eq, start + timedelta(hours=2))
    t = _template(user, eq, start, if_slot_taken="next_available_same_day", consented=True)
    assert resolve_preferred_slot(t, user)["auto_next"] is not None
    t.options = {"book_any_available_slots": True}
    t.save()
    data = resolve_preferred_slot(t, user)
    assert data["if_slot_taken"] == "ask"
    assert data["auto_next"] is None


@pytest.mark.django_db
def test_booking_page_can_turn_the_template_fallback_off(egs_factory, no_portal_lock):  # noqa: F811
    eq = egs_factory.equipment()
    start = egs_factory.future(days=3, hour=10)
    slot = egs_factory.slot(eq, start)
    egs_factory.slot(eq, start + timedelta(hours=2))
    winner, _ = _student_with_wallet(egs_factory)
    student, _ = _student_with_wallet(egs_factory)
    template = _template(student, eq, start, if_slot_taken="next_available_same_day", consented=True)
    assert _book(egs_factory, winner, eq, [slot]).status_code == 201

    body = {
        "slot_ids": [slot.pk],
        "start_time": slot.start_datetime.isoformat(),
        "end_time": slot.end_datetime.isoformat(),
        "input_values": {},
        "waitlist_on_failure": False,
        "booking_template_id": template.pk,
        "use_template_slot_fallback": False,
    }
    refused = egs_factory.client_for(student).post(f"/api/equipments/{eq.pk}/book/", body, format="json")
    assert refused.status_code == 400
    assert "slot_fallback" not in refused.data

    # Without the flag (older clients) the template's choice still applies.
    booked = _book(egs_factory, student, eq, [slot], template)
    assert booked.status_code == 201, booked.data
    assert booked.data["slot_fallback"]["mode"] == "next_available_same_day"


@pytest.mark.django_db
def test_legacy_template_with_any_free_slots_does_not_auto_book_the_next_run(egs_factory, no_portal_lock):  # noqa: F811
    eq = egs_factory.equipment()
    start = egs_factory.future(days=3, hour=10)
    slot = egs_factory.slot(eq, start)
    egs_factory.slot(eq, start + timedelta(hours=2))
    winner, _ = _student_with_wallet(egs_factory)
    student, _ = _student_with_wallet(egs_factory)
    template = _template(student, eq, start, if_slot_taken="next_available_same_day", consented=True)
    BookingInputTemplate.objects.filter(pk=template.pk).update(options={"book_any_available_slots": True})
    assert _book(egs_factory, winner, eq, [slot]).status_code == 201
    resp = _book(egs_factory, student, eq, [slot], template)
    assert resp.status_code == 400
    assert "slot_fallback" not in resp.data

"""Booking Assistant guided flow (department -> equipment -> inputs -> slot -> summary) and its rule checks."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.utils import timezone

from iic_booking.research_copilot.services.v2.mutations import booking as booking_mut
from iic_booking.research_copilot.services.v2.mutations import proposals as prop_store
from iic_booking.research_copilot.tests.test_booking_assistant import (  # noqa: F401  (fixtures)
    BASE,
    LOCK,
    _actions,
    _card,
    _client,
    _new_conv,
    _post,
    flags,
    lab,
)

BOOKABLE = "iic_booking.research_copilot.services.v2.mutations.booking._slots_bookable_for_user"


def _flow(lab, conv, step, label="", **payload):
    return _post(lab, conv, label or step, action={"type": "ba_flow", "payload": {"step": step, **payload}})


def _start(lab, conv):
    return _client(lab.student).post(
        f"{BASE}/conversations/{conv}/messages/",
        {"content": "Book equipment", "choice": {"kind": "start", "value": "book"}},
        format="json",
    )


def _meta(resp):
    return resp.json()["message"]["metadata"]


def _dept(lab, name, **kw):
    import uuid

    from iic_booking.users.models import Department

    tag = uuid.uuid4().hex[:4].upper()
    return Department.objects.create(
        name=f"{name}-{tag}", code=f"D{tag}",
        equipment_booking_enabled=kw.pop("booking", True), equipment_visibility_enabled=kw.pop("visible", True),
    )


def _move(eq, dept):
    eq.internal_department = dept
    eq.save(update_fields=["internal_department"])
    return eq


def _field(eq, key, label, ftype, *, required=True, options=None, help_text=""):
    from iic_booking.equipment.models import DynamicInputField

    return DynamicInputField.objects.create(
        equipment=eq, field_key=key, field_label=label, field_type=ftype, is_required=required,
        options=options, help_text=help_text,
    )


# =========================================================================== pure portal rules


class TestSlotRules:
    @pytest.mark.parametrize(
        "analysis,slot,tol,expected",
        [(0, 60, 0, 0), (30, 60, 0, 1), (60, 60, 0, 1), (61, 60, 0, 2), (65, 60, 10, 1), (125.9, 60, 0, 3), (10, 60, 15, 1)],
    )
    def test_portal_slots_needed(self, analysis, slot, tol, expected):
        assert booking_mut.portal_slots_needed(analysis, slot, tol) == expected

    @staticmethod
    def _slots(n, minutes=60):
        start = timezone.now().replace(microsecond=0)
        return [
            SimpleNamespace(start_datetime=start + timedelta(minutes=minutes * i), end_datetime=start + timedelta(minutes=minutes * (i + 1)))
            for i in range(n)
        ]

    def test_selection_matches_like_booking_page(self):
        eq = SimpleNamespace(slot_duration_minutes=60, slot_tolerance_minutes=0)
        assert booking_mut.selection_matches_required_time(eq, self._slots(1), 45)
        assert not booking_mut.selection_matches_required_time(eq, self._slots(2), 45)
        assert booking_mut.selection_matches_required_time(eq, self._slots(2), 120)
        assert not booking_mut.selection_matches_required_time(eq, self._slots(1), 120)
        assert not booking_mut.selection_matches_required_time(eq, self._slots(4), 120)
        assert booking_mut.selection_matches_required_time(eq, self._slots(3), 150)

    def test_istem_ack_required_for_external_users_only(self):
        from iic_booking.users.models.user_type import UserType

        assert booking_mut.istem_ack_error(SimpleNamespace(user_type=UserType.EXTERNAL, istem_portal_acknowledged=False))
        assert not booking_mut.istem_ack_error(SimpleNamespace(user_type=UserType.EXTERNAL, istem_portal_acknowledged=True))
        assert not booking_mut.istem_ack_error(SimpleNamespace(user_type=UserType.STUDENT, istem_portal_acknowledged=False))

    def test_virtual_id_in_booked_message(self):
        msg = booking_mut._friendly_book_message(
            True, {"booking_id": "IICICPMS/MS202600001", "virtual_booking_id": "IICICPMS/MS202600001", "real_booking_id": 619}
        )
        assert "Booking ID: IICICPMS/MS202600001" in msg and "619" not in msg

    def test_find_virtual_ref(self):
        from iic_booking.research_copilot.services.booking_refs import find_virtual_ref

        assert find_virtual_ref("status of iicicpms/ms202600001 please") == "IICICPMS/MS202600001"
        assert find_virtual_ref("status of booking 619") is None


# =========================================================================== guided flow (API)


@pytest.mark.django_db
class TestGuidedFlow:
    def test_departments_only_with_bookable_visible_equipment(self, lab):
        from iic_booking.users.models.user_group import UserGroup

        open_dept = _dept(lab, "Physics")
        _move(lab.equipment("Raman Spectrometer", "RAMAN-9"), open_dept)
        hidden_catalogue = _dept(lab, "Hidden", visible=False)
        _move(lab.equipment("Hidden Catalogue XPS", "XPS-H"), hidden_catalogue)
        closed = _dept(lab, "Closed", booking=False)
        _move(lab.equipment("Closed Dept AFM", "AFM-C"), closed)
        inactive = _dept(lab, "Repairs")
        broken = _move(lab.equipment("Broken NMR", "NMR-X"), inactive)
        broken.status = "REPAIR"
        broken.save(update_fields=["status"])
        private = _dept(lab, "Private")
        eq = _move(lab.equipment("Private ICP-MS", "ICP-P"), private)
        eq.visibility_group = UserGroup.objects.create(name="PG", code="PG1")
        eq.save(update_fields=["visibility_group"])

        card, body = _card(_start(lab, _new_conv(lab)), "ba_flow_departments")
        ids = {d["department_id"] for d in card["items"]}
        assert ids == {lab.department.pk, open_dept.pk}
        assert card["step"] == {"index": 1, "total": 5, "label": "Department"}
        assert any(a.get("payload", {}).get("step") == "cancel" for a in _actions(body))

    def test_equipment_step_shows_parents_with_modes_and_back(self, lab):
        parent = lab.equipment("Raman Spectrometer", "RAMAN-1", enable_multi_mode=True)
        child = lab.equipment("Raman Spectrometer 532 nm mode", "RAMAN-1-532", parent_equipment=parent)
        broken = lab.equipment("Old FTIR", "FTIR-0")
        broken.status = "REPAIR"
        broken.save(update_fields=["status"])
        conv = _new_conv(lab)
        card, body = _card(_flow(lab, conv, "department", department_id=lab.department.pk), "ba_flow_equipment")
        ids = [r["equipment_id"] for r in card["items"]]
        assert parent.pk in ids and child.pk not in ids and broken.pk not in ids
        row = next(r for r in card["items"] if r["equipment_id"] == parent.pk)
        assert [m["equipment_id"] for m in row["modes"]] == [child.pk]
        assert card["step"]["index"] == 2
        assert any(a.get("payload", {}).get("step") == "change_department" for a in _actions(body))

    def test_typed_book_starts_the_flow(self, lab):
        for text in ("book", "I want to book equipment", "book a slot please"):
            meta = _meta(_post(lab, _new_conv(lab), text))
            assert meta["intent"] in ("assistant:flow_departments", "assistant:flow_equipment"), text

    def test_full_flow_to_confirm_token_and_navigation(self, lab, settings):
        from iic_booking.equipment.models import Booking

        settings.COPILOT_BOOKING_CREATE = True
        lab.fund()
        _field(lab.xrd, "B", "Sample type", "TEXT")
        slot = lab.slot(lab.xrd, lab.future(days=4, hour=10))
        conv = _new_conv(lab)
        with patch(LOCK, return_value=(False, "")):
            _card(_start(lab, conv), "ba_flow_equipment")
            form, _ = _card(_flow(lab, conv, "equipment", equipment_id=lab.xrd.pk), "ba_booking_form")
            assert form["step"]["index"] == 3 and form["flow"] is True
            assert [f["key"] for f in form["fields"]] == ["B"] and form["fields"][0]["required"]
            assert form["sample_sets"]["allowed"] is True

            again, _ = _card(_flow(lab, conv, "inputs", equipment_id=lab.xrd.pk, number_of_samples=1), "ba_booking_form")
            assert "Sample type" in again["error"]

            slots, _ = _card(
                _flow(lab, conv, "inputs", equipment_id=lab.xrd.pk, number_of_samples=1, input_values={"B": "Powder"}), "ba_slots"
            )
            assert slots["flow"] is True and slots["step"]["index"] == 4
            chip = next(c for d in slots["days"] for c in d["slots"] if slot.pk in c["slot_ids"])

            summary, body = _card(_flow(lab, conv, "slot", equipment_id=lab.xrd.pk, slot_ids=chip["slot_ids"]), "ba_booking_summary")
        assert summary["step"]["index"] == 5 and summary["executable"] is True
        assert summary["total_amount"] == 100.0
        assert summary["wallet_balance"] == 10000.0 and summary["balance_after_total"] == 9900.0
        assert summary["department_name"] == lab.department.name
        confirm = [a for a in _actions(body) if a.get("confirmation_token")]
        assert len(confirm) == 1 and confirm[0]["proposal_id"] == summary["proposal_id"]
        steps = {a.get("payload", {}).get("step") for a in _actions(body)}
        assert {"change_slot", "edit_inputs", "change_equipment", "cancel"} <= steps
        assert not Booking.objects.filter(equipment=lab.xrd).exists()

        with patch(LOCK, return_value=(False, "")):
            _card(_flow(lab, conv, "change_slot", equipment_id=lab.xrd.pk), "ba_slots")
            form, _ = _card(_flow(lab, conv, "edit_inputs", equipment_id=lab.xrd.pk), "ba_booking_form")
        assert form["values"]["B"] == "Powder"

        resp = _flow(lab, conv, "cancel")
        assert "nothing was booked" in resp.json()["message"]["content"]
        assert prop_store.get_proposal(summary["proposal_id"]) is None
        assert not Booking.objects.filter(equipment=lab.xrd).exists()

    def _to_slot_pick(self, lab, conv, **fund):
        lab.fund(**fund)
        slot = lab.slot(lab.xrd, lab.future(days=4, hour=10))
        with patch(LOCK, return_value=(False, "")):
            _card(_flow(lab, conv, "inputs", equipment_id=lab.xrd.pk, number_of_samples=1), "ba_slots")
        return slot

    def test_freeze_blocks_before_summary(self, lab, settings):
        settings.COPILOT_BOOKING_CREATE = True
        conv = _new_conv(lab)
        slot = self._to_slot_pick(lab, conv)
        with patch(LOCK, return_value=(True, "Bookings are frozen for the portal migration.")):
            resp = _flow(lab, conv, "slot", equipment_id=lab.xrd.pk, slot_ids=[slot.pk])
        assert "frozen" in resp.json()["message"]["content"]
        assert not any(a.get("confirmation_token") for a in _actions(resp.json()))

    def test_insufficient_wallet_blocks(self, lab, settings):
        settings.COPILOT_BOOKING_CREATE = True
        conv = _new_conv(lab)
        slot = self._to_slot_pick(lab, conv, balance="10.00")
        with patch(LOCK, return_value=(False, "")):
            resp = _flow(lab, conv, "slot", equipment_id=lab.xrd.pk, slot_ids=[slot.pk])
        meta = _meta(resp)
        assert meta["intent"] == "assistant:book_blocked"
        assert not any(a.get("confirmation_token") for a in _actions(resp.json()))
        assert any(a.get("href") == "/wallet" for a in _actions(resp.json()))

    def test_spending_limit_blocks(self, lab, settings):
        settings.COPILOT_BOOKING_CREATE = True
        conv = _new_conv(lab)
        slot = self._to_slot_pick(lab, conv, spending_limit_enabled=True, weekly_limit_inr=50)
        with patch(LOCK, return_value=(False, "")):
            resp = _flow(lab, conv, "slot", equipment_id=lab.xrd.pk, slot_ids=[slot.pk])
        assert _meta(resp)["intent"] == "assistant:book_blocked"
        assert not any(a.get("confirmation_token") for a in _actions(resp.json()))

    def test_closed_booking_window_returns_to_slots(self, lab, settings):
        settings.COPILOT_BOOKING_CREATE = True
        conv = _new_conv(lab)
        slot = self._to_slot_pick(lab, conv)
        with patch(LOCK, return_value=(False, "")), patch(BOOKABLE, return_value=False):
            resp = _flow(lab, conv, "slot", equipment_id=lab.xrd.pk, slot_ids=[slot.pk])
        card, body = _card(resp, "ba_slots")
        assert card["flow"] is True and "not bookable" in body["message"]["content"]

    def test_no_charge_profile_department_excluded(self, lab):
        from iic_booking.equipment.models import ChargeProfile

        ChargeProfile.objects.filter(equipment=lab.tem).update(is_active=False)
        card, _ = _card(_flow(lab, _new_conv(lab), "department", department_id=lab.department.pk), "ba_flow_equipment")
        assert lab.tem.pk not in [r["equipment_id"] for r in card["items"]]


# =========================================================================== input parity


@pytest.mark.django_db
class TestInputParity:
    def test_numeric_limits_choice_and_required_zero(self, lab):
        from iic_booking.research_copilot.services.assistant.booking_flow import check_inputs

        _field(lab.xrd, "B", "Scan count", "NUMERIC", options={"min": 1, "max": 10})
        _field(lab.xrd, "C", "Mode", "RADIO", options=["Fast", "Slow"], required=False)
        out = check_inputs(lab.student, lab.xrd, 1, {"B": "50"}, [])
        assert out["errors"] and any("10" in e or "Scan count" in e for e in out["errors"])
        out = check_inputs(lab.student, lab.xrd, 1, {"B": "0"}, [])
        assert any("Scan count" in e for e in out["errors"])
        out = check_inputs(lab.student, lab.xrd, 1, {"B": "3", "C": "Turbo"}, [])
        assert any("Mode" in e for e in out["errors"])
        out = check_inputs(lab.student, lab.xrd, 1, {"B": "3", "C": "Fast"}, [])
        assert not out["errors"] and out["required_minutes"] is not None

    def test_periodic_table_symbols(self, lab):
        from iic_booking.research_copilot.services.assistant.booking_flow import check_inputs

        _field(lab.xrd, "B", "Elements", "PERIODIC_TABLE")
        bad = check_inputs(lab.student, lab.xrd, 1, {"B_elements": "Fe,Xx"}, [])
        assert any("Xx" in e for e in bad["errors"])
        ok = check_inputs(lab.student, lab.xrd, 1, {"B_elements": "fe, Cu"}, [])
        assert not ok["errors"]
        assert ok["values"]["B_elements"] == "Fe,Cu" and ok["values"]["B"] == "2"

    def test_sample_sets_validated_each(self, lab):
        from iic_booking.research_copilot.services.assistant.booking_flow import check_inputs

        _field(lab.xrd, "B", "Scan count", "NUMERIC", options={"min": 1, "max": 10})
        bad = check_inputs(lab.student, lab.xrd, 1, {"B": "2"}, [{"A": "2", "B": "99"}])
        assert any(e.startswith("Sample set 2") for e in bad["errors"])
        ok = check_inputs(lab.student, lab.xrd, 1, {"B": "2"}, [{"A": "2", "B": "3"}])
        assert not ok["errors"] and len(ok["values"]["_sample_sets"]) == 1

    def test_prepare_rejects_slots_that_do_not_match_analysis_time(self, lab):
        lab.fund()
        lab.xrd.charge_profiles.update(time_formula="150")
        slot = lab.slot(lab.xrd, lab.future(days=4, hour=10))
        with patch(LOCK, return_value=(False, "")):
            out = booking_mut.prepare_booking_create(user=lab.student, equipment_id=lab.xrd.pk, slot_ids=[slot.pk], number_of_samples=1)
        assert out["ok"] is False and out["error"] == "SLOT_SELECTION_MISMATCH"


@pytest.mark.django_db
def test_booking_status_by_virtual_id(lab):
    b, _ = lab.booking(lab.student, lab.xrd, lab.future(days=6))
    b.virtual_booking_id = "IICXRD/B1202600123"
    b.save(update_fields=["virtual_booking_id"])
    body = _post(lab, _new_conv(lab), "status of booking IICXRD/B1202600123").json()
    text = body["message"]["content"] + str(body["message"]["metadata"].get("cards") or "")
    assert "IICXRD/B1202600123" in text
    assert f"#{b.booking_id}" not in body["message"]["content"]


@pytest.mark.django_db
def test_wallet_target_and_exact_charge(lab):
    from iic_booking.research_copilot.services.assistant import preflight

    lab.fund(balance="500.00")
    slot = lab.slot(lab.xrd, lab.future(days=4, hour=10))
    with patch(LOCK, return_value=(False, "")):
        out = preflight.run(lab.student, lab.xrd, {"A": "1"}, [slot])
    assert out["ok"], out["problems"]
    assert out["charge"]["total"] == 100.0 and out["charge"]["slot_minutes"] == 60
    assert out["wallet"]["balance"] == 500.0 and out["balance_after"] == 400.0
    assert Decimal(str(out["balance_after"])) == Decimal("400")

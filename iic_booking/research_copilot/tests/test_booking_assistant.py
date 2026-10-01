"""Booking Assistant: date parsing, fuzzy matching, availability, confirm-gated booking and scoping."""

from __future__ import annotations

from datetime import date, time, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from iic_booking.research_copilot.services.assistant import actions as A
from iic_booking.research_copilot.services.assistant import matching
from iic_booking.research_copilot.services.assistant.dates import parse_when, strip_when, when_from_payload
from iic_booking.research_copilot.services.v2.mutations import proposals as prop_store
from iic_booking.research_copilot.tests.test_copilot_intelligence import BASE, _client, _World

THU = date(2026, 10, 1)  # a Thursday
LOCK = "iic_booking.users.legacy_ledger.booking_lock.booking_is_locked"


# =============================================================================== dates (IST, pure)


class TestDates:
    def test_tomorrow(self):
        w = parse_when("I need FESEM tomorrow — what are my options?", today=THU)
        assert (w.start_date, w.end_date) == (date(2026, 10, 2), date(2026, 10, 2))
        assert w.explicit_date and w.label.startswith("tomorrow")

    def test_default_window_is_next_seven_days(self):
        w = parse_when("is xrd free", today=THU)
        assert (w.start_date, w.end_date) == (THU, THU + timedelta(days=6))
        assert not w.explicit

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("monday", date(2026, 10, 5)),
            ("next monday", date(2026, 10, 5)),
            ("on friday", date(2026, 10, 2)),
            ("this thursday", THU),
            ("next thursday", date(2026, 10, 8)),
            ("15 Oct", date(2026, 10, 15)),
            ("Oct 20th", date(2026, 10, 20)),
            ("2026-10-09", date(2026, 10, 9)),
            ("5/10", date(2026, 10, 5)),
            ("day after tomorrow", date(2026, 10, 3)),
            ("in 3 days", date(2026, 10, 4)),
        ],
    )
    def test_single_days(self, text, expected):
        w = parse_when(text, today=THU)
        assert w.start_date == expected and w.end_date == expected

    def test_past_day_of_month_rolls_to_next_year(self):
        assert parse_when("5 Jan", today=THU).start_date == date(2027, 1, 5)

    def test_explicit_past_date_is_flagged(self):
        assert parse_when("1 Jan 2020", today=THU).past

    def test_ranges(self):
        nw = parse_when("FESEM next week", today=THU)
        assert (nw.start_date, nw.end_date) == (date(2026, 10, 5), date(2026, 10, 11))
        assert nw.label.startswith("next week")
        tw = parse_when("this week", today=THU)
        assert (tw.start_date, tw.end_date) == (THU, date(2026, 10, 4))

    def test_times(self):
        w = parse_when("tomorrow morning", today=THU)
        assert w.period == "morning" and w.before == time(12, 0) and w.explicit_time
        w = parse_when("friday after 2pm", today=THU)
        assert w.after == time(14, 0) and w.start_date == date(2026, 10, 2)
        w = parse_when("between 10 and 1", today=THU)
        assert (w.after, w.before) == (time(10, 0), time(13, 0))
        w = parse_when("tomorrow at 3", today=THU)
        assert w.at == time(15, 0)
        assert parse_when("afternoon", today=THU).after == time(12, 0)

    def test_sample_and_hour_counts_are_not_times(self):
        w = parse_when("book XRD for 5 samples, 2.5 hours", today=THU)
        assert not w.explicit_time and w.at is None

    def test_strip_when_leaves_equipment(self):
        text = "I need FESEM tomorrow morning"
        assert matching.equipment_phrase(strip_when(text, parse_when(text, today=THU))) == "fesem"

    def test_payload_is_clamped_to_today(self):
        w = when_from_payload({"start": "2001-01-01", "end": "2001-01-02"})
        assert w.start_date == timezone.localdate()


# =============================================================================== actions whitelist (pure)


class TestActionParsing:
    def test_valid_review(self):
        out = A.parse({"type": "ba_review", "payload": {"equipment_id": 4, "slot_ids": [1, 2], "number_of_samples": "3",
                                                        "input_values": {"B": "Si wafer"}}})
        assert out == {"type": "ba_review", "payload": {"equipment_id": 4, "slot_ids": [1, 2], "number_of_samples": 3,
                                                        "input_values": {"B": "Si wafer"}}}

    @pytest.mark.parametrize(
        "raw",
        [
            {"type": "ba_execute", "payload": {}},
            {"type": "ba_review", "payload": {"equipment_id": 1}},
            {"type": "ba_review", "payload": {"equipment_id": 1, "slot_ids": [], "number_of_samples": 1}},
            {"type": "ba_review", "payload": {"equipment_id": 1, "slot_ids": [1], "number_of_samples": 9999}},
            {"type": "ba_review", "payload": {"equipment_id": 1, "slot_ids": [1], "input_values": {"zz": "1"}}},
            {"type": "ba_pick_slot", "payload": {"equipment_id": 1, "slot_ids": [1], "user_id": 7}},
            {"type": "ba_availability", "payload": {"equipment_id": "abc"}},
            {"type": "ba_availability", "payload": {"equipment_id": 1, "when": {"start": "tomorrow"}}},
            {"type": "ba_info", "payload": {"equipment_id": 1, "topic": "secrets"}},
        ],
    )
    def test_rejects(self, raw):
        with pytest.raises(A.InvalidAssistantAction):
            A.parse(raw)


# =============================================================================== LLM planner (pure)


class PlannerTests(SimpleTestCase):
    def _result(self, text):
        return SimpleNamespace(text=text)

    @override_settings(BOOKING_ASSISTANT_LLM_PLANNER="auto", OPENAI_API_KEY="", COPILOT_PROVIDER="ollama")
    def test_auto_without_key_is_off(self):
        from iic_booking.research_copilot.services.assistant import planner

        self.assertFalse(planner.planner_enabled())
        self.assertIsNone(planner.plan("could I grab the electron microscope sometime next week"))

    @override_settings(BOOKING_ASSISTANT_LLM_PLANNER="auto", OPENAI_API_KEY="sk-test", COPILOT_PROVIDER="ollama")
    def test_auto_with_key_uses_openai_even_when_chat_runs_on_ollama(self):
        from iic_booking.research_copilot.services.assistant import planner
        from iic_booking.research_copilot.services.llm_gateway import OpenAIGateway

        self.assertTrue(planner.planner_enabled())
        self.assertIsInstance(planner._planner_gateway(), OpenAIGateway)
        reply = '{"intent": "availability", "equipment": "electron microscope", "when": "next week", "topic": ""}'
        with patch.object(OpenAIGateway, "complete", return_value=self._result(reply)) as complete:
            out = planner.plan("could I grab the electron microscope sometime next week")
        self.assertEqual(out, {"intent": "availability", "equipment": "electron microscope", "when": "next week", "topic": None})
        self.assertLessEqual(len(complete.call_args.args[0][1]["content"]), 400)

    @override_settings(BOOKING_ASSISTANT_LLM_PLANNER="auto", OPENAI_API_KEY="sk-test")
    def test_invented_words_are_dropped_and_failures_fall_back(self):
        from iic_booking.research_copilot.services.assistant import planner
        from iic_booking.research_copilot.services.llm_gateway import OpenAIGateway

        reply = '{"intent": "info", "equipment": "TEM", "when": "", "topic": "location"}'
        with patch.object(OpenAIGateway, "complete", return_value=self._result(reply)):
            self.assertEqual(planner.plan("where can I find the scope")["equipment"], "")
        with patch.object(OpenAIGateway, "complete", side_effect=RuntimeError("down")):
            self.assertIsNone(planner.plan("where can I find the scope"))
        with patch.object(OpenAIGateway, "complete", return_value=self._result("not json")):
            self.assertIsNone(planner.plan("where can I find the scope"))

    @override_settings(BOOKING_ASSISTANT_LLM_PLANNER="off", OPENAI_API_KEY="sk-test")
    def test_off_wins_over_key(self):
        from iic_booking.research_copilot.services.assistant import planner

        self.assertFalse(planner.planner_enabled())


# =============================================================================== proposal token binding (pure)


def _u(pk):
    return SimpleNamespace(pk=pk, is_authenticated=True)


class ProposalTokenTests(SimpleTestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.payload = {"equipment_id": 10, "slot_ids": [100, 101], "number_of_samples": 2, "input_values": {"A": "2"}}
        self.rec = prop_store.create_proposal(user=_u(1), action="CREATE_BOOKING", payload=dict(self.payload))

    def _validate(self, user=None, token=None, pid=None):
        return prop_store.validate_proposal_for_user(
            user=user or _u(1),
            proposal_id=pid or self.rec["proposal_id"],
            confirmation_token=self.rec["confirmation_token"] if token is None else token,
            expected_action="CREATE_BOOKING",
        )

    def test_valid_token(self):
        prop, err = self._validate()
        self.assertIsNone(err)
        self.assertEqual(prop["payload"]["slot_ids"], [100, 101])
        self.assertEqual(prop["binding_version"], 2)

    def test_create_booking_ttl_is_ten_minutes(self):
        from datetime import datetime

        created = datetime.fromisoformat(self.rec["created_at"])
        expires = datetime.fromisoformat(self.rec["expires_at"])
        self.assertEqual(int((expires - created).total_seconds()), 600)

    def test_missing_token(self):
        self.assertEqual(self._validate(token="")[1], "CONFIRMATION_REQUIRED")

    def test_wrong_token(self):
        self.assertEqual(self._validate(token="abc.def")[1], "CONFIRMATION_INVALID")

    def test_other_user(self):
        self.assertEqual(self._validate(user=_u(2))[1], "PROPOSAL_FORBIDDEN")

    def test_token_of_another_proposal(self):
        other = prop_store.create_proposal(user=_u(1), action="CREATE_BOOKING", payload={"equipment_id": 11, "slot_ids": [5]})
        self.assertEqual(self._validate(token=other["confirmation_token"])[1], "CONFIRMATION_INVALID")

    def test_payload_tamper_breaks_binding(self):
        from django.core.cache import cache

        key = f"copilot_proposal:{self.rec['proposal_id']}"
        tampered = cache.get(key)
        tampered["payload"]["slot_ids"] = [999]
        cache.set(key, tampered, 600)
        self.assertEqual(self._validate()[1], "CONFIRMATION_INVALID")
        tampered["payload_fingerprint"] = prop_store._fingerprint(tampered["payload"])
        cache.set(key, tampered, 600)
        self.assertEqual(self._validate()[1], "CONFIRMATION_INVALID")

    def test_user_rebinding_in_store_is_rejected(self):
        from django.core.cache import cache

        key = f"copilot_proposal:{self.rec['proposal_id']}"
        moved = cache.get(key)
        moved["user_id"] = 2
        cache.set(key, moved, 600)
        self.assertEqual(self._validate(user=_u(2))[1], "CONFIRMATION_INVALID")

    def test_expired(self):
        with patch.object(prop_store.timezone, "now", return_value=timezone.now() + timedelta(seconds=601)):
            self.assertEqual(self._validate()[1], "PROPOSAL_EXPIRED")

    def test_claim_is_single_use(self):
        pid = self.rec["proposal_id"]
        self.assertTrue(prop_store.claim_proposal(pid))
        self.assertFalse(prop_store.claim_proposal(pid))
        prop_store.release_claim(pid)
        self.assertTrue(prop_store.claim_proposal(pid))

    @override_settings(COPILOT_BOOKING_CREATE=True)
    def test_execute_refuses_concurrent_confirm(self):
        from iic_booking.research_copilot.services.v2.mutations import booking as booking_mut

        prop_store.claim_proposal(self.rec["proposal_id"])
        with patch("iic_booking.research_copilot.services.v2.mutations.domain_bridge.call_book_equipment") as book:
            out = booking_mut.execute_booking_create(
                user=_u(1), proposal_id=self.rec["proposal_id"], confirmation_token=self.rec["confirmation_token"]
            )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "PROPOSAL_IN_PROGRESS")
        book.assert_not_called()

    @override_settings(COPILOT_BOOKING_CREATE=True)
    def test_execute_without_valid_token_never_books(self):
        from iic_booking.research_copilot.services.v2.mutations import booking as booking_mut

        with patch("iic_booking.research_copilot.services.v2.mutations.domain_bridge.call_book_equipment") as book:
            for token in ("", "forged.token"):
                out = booking_mut.execute_booking_create(user=_u(1), proposal_id=self.rec["proposal_id"], confirmation_token=token)
                self.assertFalse(out["ok"])
            out = booking_mut.execute_booking_create(
                user=_u(2), proposal_id=self.rec["proposal_id"], confirmation_token=self.rec["confirmation_token"]
            )
            self.assertFalse(out["ok"])
        book.assert_not_called()


# =============================================================================== DB-backed


FLAGS = {
    "RESEARCH_COPILOT_ENABLED": True,
    "RESEARCH_COPILOT_INTELLIGENCE_ENABLED": True,
    "RESEARCH_COPILOT_KNOWLEDGE_ENABLED": False,
    "RESEARCH_COPILOT_ACTIONS_ENABLED": False,
    "BOOKING_ASSISTANT_ENABLED": True,
    "BOOKING_ASSISTANT_LLM_PLANNER": "off",
    "OPENAI_API_KEY": "",
    "COPILOT_LLM_PROVIDER": "fallback",
}


@pytest.fixture
def flags(settings):
    for k, v in FLAGS.items():
        setattr(settings, k, v)
    return settings


class _Lab(_World):
    def __init__(self):
        super().__init__()
        self.fesem = self.equipment("Field Emission Scanning Electron Microscope (FESEM)", "FESEM-01")
        self.xrd = self.equipment("Bruker D8 Advance XRD", "XRD-B1")
        self.tem = self.equipment("Transmission Electron Microscope", "TEM-200")
        self.sem = self.equipment("Tabletop SEM", "SEM-T1")

    def hidden(self, name, code):
        from iic_booking.users.models.user_group import UserGroup

        group = UserGroup.objects.create(name=f"G-{code}", code=f"G{code}"[:20])
        eq = self.equipment(name, code)
        eq.visibility_group = group
        eq.save(update_fields=["visibility_group"])
        return eq, group


@pytest.fixture
def lab(db, flags):
    return _Lab()


@pytest.mark.django_db
class TestMatching:
    def test_fesem_variants_are_unique(self, lab):
        for q in ("FESEM", "fesem", "FE-SEM", "field emission sem", "Field Emission Scanning Electron Microscope", "fesem-01"):
            m = matching.match_equipment(lab.student, q)
            assert m.status == "unique" and m.equipment.pk == lab.fesem.pk, q

    def test_xrd_and_code(self, lab):
        assert matching.match_equipment(lab.student, "XRD").equipment.pk == lab.xrd.pk
        assert matching.match_equipment(lab.student, "x-ray diffraction").equipment.pk == lab.xrd.pk
        assert matching.match_equipment(lab.student, "TEM").equipment.pk == lab.tem.pk

    @pytest.mark.parametrize("typo,target", [("fesm", "fesem"), ("feesem", "fesem"), ("xdr", "xrd"), ("bruker d8 advanse", "xrd")])
    def test_misspellings_become_options(self, lab, typo, target):
        m = matching.match_equipment(lab.student, typo)
        assert m.status in {"options", "unique"}
        ids = [c.id for c in m.candidates]
        assert getattr(lab, target).pk in ids, (typo, [c.eq.name for c in m.candidates])
        if m.status == "options":
            assert m.misspelled or len(ids) > 1

    def test_generic_phrase_lists_options(self, lab):
        m = matching.match_equipment(lab.student, "electron microscope")
        assert m.status == "options"
        ids = {c.id for c in m.candidates}
        assert {lab.fesem.pk, lab.tem.pk} <= ids

    def test_inactive_not_offered(self, lab):
        old = lab.equipment("Rigaku XRD", "XRD-R2")
        old.status = "REPAIR"
        old.save(update_fields=["status"])
        m = matching.match_equipment(lab.student, "xrd")
        assert old.pk not in [c.id for c in m.candidates if m.status == "options"]
        assert lab.xrd.pk in [c.id for c in m.candidates]

    def test_hidden_equipment_never_matches(self, lab):
        from iic_booking.users.models.user_group import UserGroupMember

        icp, group = lab.hidden("Agilent 7900 ICP-MS", "ICPMS-1")
        assert icp.pk not in [c.id for c in matching.match_equipment(lab.student, "ICP-MS").candidates]
        UserGroupMember.objects.create(user_group=group, user=lab.student)
        assert matching.match_equipment(lab.student, "icpms").equipment.pk == icp.pk
        assert icp.pk not in [c.id for c in matching.match_equipment(lab.other, "ICP-MS").candidates]

    def test_mode_child_collapses_to_parent(self, lab):
        parent = lab.equipment("Raman Spectrometer", "RAMAN-1", enable_multi_mode=True)
        lab.equipment("Raman Spectrometer 532 nm mode", "RAMAN-1-532", parent_equipment=parent)
        m = matching.match_equipment(lab.student, "raman spectrometer")
        assert m.status == "unique" and m.equipment.pk == parent.pk


def _post(lab, conv_id, content, action=None, user=None):
    body = {"content": content}
    if action:
        body["action"] = action
    resp = _client(user or lab.student).post(f"{BASE}/conversations/{conv_id}/messages/", body, format="json")
    return resp


def _new_conv(lab, user=None):
    return _client(user or lab.student).post(f"{BASE}/conversations/", {}, format="json").json()["conversation"]["id"]


def _card(resp, kind):
    body = resp.json()
    cards = body["message"]["metadata"].get("cards") or body.get("cards") or []
    found = [c for c in cards if c.get("type") == kind]
    assert found, (resp.status_code, body["message"]["content"], [c.get("type") for c in cards])
    return found[0], body


def _actions(body):
    return body["message"].get("suggested_actions") or []


@pytest.mark.django_db
class TestAssistantTurns:
    def test_fesem_tomorrow_options(self, lab):
        slot = lab.slot(lab.fesem, lab.future(days=1, hour=15))
        conv = _new_conv(lab)
        with patch(LOCK, return_value=(False, "")):
            resp = _post(lab, conv, "I need FESEM tomorrow — what are my options?")
        assert resp.status_code == 200
        card, body = _card(resp, "ba_slots")
        assert card["equipment_id"] == lab.fesem.pk
        assert card["window_label"].startswith("tomorrow")
        chips = [s for d in card["days"] for s in d["slots"]]
        assert not chips or chips[0]["slot_ids"][0] == slot.pk or card["nearest_days"] is not None
        assert body["message"]["metadata"]["booking_assistant"] is True

    def test_slot_chip_for_specific_date(self, lab):
        start = lab.future(days=4, hour=10)
        slot = lab.slot(lab.xrd, start)
        conv = _new_conv(lab)
        with patch(LOCK, return_value=(False, "")):
            resp = _post(lab, conv, f"Is XRD available on {start:%d %b}?")
        card, _ = _card(resp, "ba_slots")
        ids = [sid for d in card["days"] for s in d["slots"] for sid in s["slot_ids"]]
        assert slot.pk in ids
        assert card["estimate"] is None or "charge" in card["estimate"] or card["estimate"]

    def test_fully_booked_offers_alternatives(self, lab):
        start = lab.future(days=3, hour=10)
        lab.slot(lab.fesem, start, status="BOOKED")
        lab.slot(lab.fesem, lab.future(days=5, hour=10))
        lab.slot(lab.sem, start)
        conv = _new_conv(lab)
        with patch(LOCK, return_value=(False, "")):
            resp = _post(lab, conv, f"FESEM slots on {start:%d %b}")
        card, _ = _card(resp, "ba_slots")
        assert not any(d["slots"] for d in card["days"])
        assert card["nearest_days"] or card["similar"]

    def test_misspelled_equipment_shows_clickable_options(self, lab):
        conv = _new_conv(lab)
        resp = _post(lab, conv, "is fesm free tomorrow")
        card, body = _card(resp, "ba_equipment_options")
        ids = [o["equipment_id"] for o in card["items"]]
        assert lab.fesem.pk in ids
        assert card["when"]["start"] == (timezone.localdate() + timedelta(days=1)).isoformat()
        opt = next(o for o in card["items"] if o["equipment_id"] == lab.fesem.pk)
        payload = {"equipment_id": opt["equipment_id"], "intent": card["intent"], "when": card["when"]}

        with patch(LOCK, return_value=(False, "")):
            resp = _post(lab, conv, opt["name"], action={"type": "ba_pick_equipment", "payload": payload})
        card, _ = _card(resp, "ba_slots")
        assert card["equipment_id"] == lab.fesem.pk

    def test_equipment_info_and_charges(self, lab):
        lab.fesem.location = "IIC Building, Room 105"
        lab.fesem.save(update_fields=["location"])
        conv = _new_conv(lab)
        card, body = _card(_post(lab, conv, "Where is the FESEM located?"), "ba_equipment_info")
        assert card["focus"] == "location" and "Room 105" in card["location"]
        card, _ = _card(_post(lab, conv, "What are the charges for XRD?"), "ba_equipment_info")
        assert card["focus"] == "charges" and card["equipment_id"] == lab.xrd.pk

    def test_capability_question(self, lab):
        card, _ = _card(_post(lab, _new_conv(lab), "Which equipment can do x-ray diffraction?"), "ba_equipment_options")
        ids = [o["equipment_id"] for o in card["items"]]
        assert lab.xrd.pk in ids and lab.fesem.pk not in ids

    def test_upcoming_and_status_are_own_bookings_only(self, lab):
        mine, _ = lab.booking(lab.student, lab.xrd, lab.future(days=6))
        theirs, _ = lab.booking(lab.other, lab.xrd, lab.future(days=7))
        conv = _new_conv(lab)
        card, _ = _card(_post(lab, conv, "show my upcoming bookings"), "ba_bookings")
        ids = [r["booking_id"] for r in card["items"]]
        assert mine.booking_id in ids and theirs.booking_id not in ids
        resp = _post(lab, conv, f"status of booking {theirs.booking_id}")
        body = resp.json()
        cards = body["message"]["metadata"].get("cards") or []
        assert not any(r.get("booking_id") == theirs.booking_id for c in cards for r in c.get("items") or [])
        assert theirs.virtual_booking_id not in body["message"]["content"]

    def test_policy_question(self, lab):
        resp = _post(lab, _new_conv(lab), "How do I recharge my wallet?")
        assert resp.status_code == 200
        assert resp.json()["message"]["content"]

    def test_bad_assistant_action_is_400(self, lab):
        conv = _new_conv(lab)
        resp = _post(lab, conv, "x", action={"type": "ba_review", "payload": {"equipment_id": lab.xrd.pk, "slot_ids": "1"}})
        assert resp.status_code == 400

    def test_hidden_equipment_action_is_refused(self, lab):
        icp, _group = lab.hidden("Agilent 7900 ICP-MS", "ICPMS-1")
        conv = _new_conv(lab)
        resp = _post(lab, conv, "ICP-MS", action={"type": "ba_info", "payload": {"equipment_id": icp.pk}})
        assert resp.status_code == 200
        body = resp.json()
        assert "no longer available" in body["message"]["content"]
        assert not (body["message"]["metadata"].get("cards") or [])

    def test_foreign_slot_ids_are_refused(self, lab):
        other_slot = lab.slot(lab.tem, lab.future(days=4, hour=11))
        conv = _new_conv(lab)
        with patch(LOCK, return_value=(False, "")):
            resp = _post(lab, conv, "slot", action={"type": "ba_pick_slot",
                                                   "payload": {"equipment_id": lab.xrd.pk, "slot_ids": [other_slot.pk]}})
        body = resp.json()
        cards = body["message"]["metadata"].get("cards") or []
        assert not any(c.get("type") == "ba_booking_form" for c in cards)

    def _to_summary(self, lab, settings_obj=None):
        start = lab.future(days=4, hour=10)
        slot = lab.slot(lab.xrd, start)
        conv = _new_conv(lab)
        with patch(LOCK, return_value=(False, "")):
            form, _ = _card(_post(lab, conv, "slot", action={"type": "ba_pick_slot",
                                                            "payload": {"equipment_id": lab.xrd.pk, "slot_ids": [slot.pk]}}),
                            "ba_booking_form")
            assert form["slot_ids"] == [slot.pk]
            resp = _post(lab, conv, "Review booking", action={"type": "ba_review", "payload": {
                "equipment_id": lab.xrd.pk, "slot_ids": [slot.pk], "number_of_samples": 1}})
        return slot, conv, resp

    def test_summary_without_flag_has_no_confirm(self, lab):
        from iic_booking.equipment.models import Booking

        slot, _conv, resp = self._to_summary(lab)
        card, body = _card(resp, "ba_booking_summary")
        assert card["executable"] is False and card["proposal_id"] is None
        assert not any(a.get("confirmation_token") for a in _actions(body))
        assert not Booking.objects.filter(equipment=lab.xrd).exists()

    def test_summary_with_flag_needs_explicit_confirm(self, lab, settings):
        from iic_booking.equipment.models import Booking

        settings.COPILOT_BOOKING_CREATE = True
        slot, conv, resp = self._to_summary(lab)
        card, body = _card(resp, "ba_booking_summary")
        assert card["executable"] is True and card["proposal_id"]
        confirm = [a for a in _actions(body) if a.get("confirmation_token")]
        assert len(confirm) == 1 and confirm[0]["mutation_action"] == "CREATE_BOOKING"
        assert card["equipment_name"] == lab.xrd.name and card["sample_count"] == 1
        assert not Booking.objects.filter(equipment=lab.xrd).exists()

        # Typing "confirm" never books; only the button's token does.
        resp = _post(lab, conv, "confirm")
        assert resp.json()["message"]["metadata"].get("typed_confirm_blocked") is True
        assert not Booking.objects.filter(equipment=lab.xrd).exists()

        # Another user cannot use the token.
        foreign = _client(lab.other).post(f"{BASE}/mutations/confirm/", {
            "proposal_id": confirm[0]["proposal_id"], "confirmation_token": confirm[0]["confirmation_token"]}, format="json")
        assert foreign.status_code == 403
        bad = _client(lab.student).post(f"{BASE}/mutations/confirm/", {
            "proposal_id": confirm[0]["proposal_id"], "confirmation_token": "x.y"}, format="json")
        assert bad.json()["ok"] is False
        assert not Booking.objects.filter(equipment=lab.xrd).exists()

    def test_bare_technique_and_choices_fall_through(self, lab):
        from iic_booking.research_copilot.services.assistant.engine import try_assistant_turn

        conv = lab.conversation()
        assert try_assistant_turn(user=lab.student, text="fesem", conversation=conv) is None
        assert try_assistant_turn(user=lab.student, text="Book XRD", conversation=conv,
                                  choice={"kind": "samples", "value": "2"}) is None
        assert try_assistant_turn(user=lab.student, text="cancel my booking", conversation=conv) is None
        assert try_assistant_turn(user=lab.student, text="What is my wallet balance?", conversation=conv) is None
        assert try_assistant_turn(user=lab.student, text="How do I book equipment on the portal?", conversation=conv) is None
        assert try_assistant_turn(user=lab.student, text="List my recent bookings.", conversation=conv) is None
        assert try_assistant_turn(user=lab.student, text="Estimate the cost of booking FESEM for 2 hours.", conversation=conv) is None
        assert try_assistant_turn(user=SimpleNamespace(is_authenticated=False), text="FESEM tomorrow", conversation=conv) is None

    @override_settings(BOOKING_ASSISTANT_ENABLED=False)
    def test_kill_switch(self, lab):
        from iic_booking.research_copilot.services.assistant.engine import try_assistant_turn

        assert try_assistant_turn(user=lab.student, text="I need FESEM tomorrow", conversation=lab.conversation()) is None

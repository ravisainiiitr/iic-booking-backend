"""Booking Assistant: "Need help?" after a failed booking, answers for the questions it used to log as
unanswered, next-step chips, role-based starter chips and the thumbs-down comment."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.utils import timezone

from iic_booking.research_copilot.services.assistant import actions as A
from iic_booking.research_copilot.services.assistant import help_offer
from iic_booking.research_copilot.services.assistant.intents import detect
from iic_booking.research_copilot.tests.test_booking_assistant import _Lab, _card, _new_conv, _post, flags  # noqa: F401
from iic_booking.research_copilot.tests.test_copilot_intelligence import BASE, _client

# =============================================================================== intent table (pure)

NEW_INTENTS = [
    ("What is FWHM?", "glossary", {"terms": ["fwhm"]}),
    ("What is XRD?", "glossary", {"terms": ["xrd"]}),
    ("What is PXRD?", "glossary", {"terms": ["pxrd"]}),
    ("What is the difference between XRD and SEM?", "glossary", {"terms": ["xrd", "sem"]}),
    ("What is Bragg law briefly?", "glossary", {"terms": ["bragg"]}),
    ("Can XRD do thin-film measurements?", "glossary", {"terms": ["gixrd"]}),
    ("Lab access hours policy", "lab_hours", None),
    ("Can I?", "vague", None),
    ("Okay.", "smalltalk", None),
    ("thank you", "smalltalk", None),
    ("What software can I use for PXRD?", "analysis_help", {"topic": "software"}),
    ("Recommend ImageJ for TEM", "analysis_help", {"topic": "software"}),
    ("Which software for .dm4 files?", "analysis_help", {"topic": "software"}),
    ("Analysis workstation software list", "analysis_help", {"topic": "software"}),
    ("Can I analyze my data remotely?", "analysis_help", {"topic": "remote"}),
    ("Which data can I send to the analysis PC?", "analysis_help", {"topic": "remote"}),
    ("Open remote analysis for my booking", "analysis_help", {"topic": "remote"}),
    ("What files are available?", "analysis_help", {"topic": "files"}),
    ("What happened to my data after analysis?", "analysis_help", {"topic": "files"}),
    ("Download analyzed data", "analysis_help", {"topic": "files"}),
    ("Where are analyzed files after the session?", "analysis_help", {"topic": "files"}),
    ("What should I prepare before my XRD booking?", "prepare", {"with_cost": False}),
    ("What should I prepare?", "prepare", None),
    ("How do I prepare a sample for FESEM?", "prepare", None),
    ("My PXRD booking is tomorrow. What will it cost and what should I prepare?", "prepare", {"with_cost": True}),
    ("Submit by when?", "sample_submission", None),
    ("When should I submit my sample?", "sample_submission", None),
    ("How much does 5 XRD samples cost?", "estimate", {"samples": 5}),
    ("Cost of FESEM booking?", "estimate", {"samples": None}),
    ("How much will it cost?", "estimate", None),
    ("PI pricing for 5 PXRD samples", "who_pays", {"samples": 5}),
    ("Am I charged as wallet owner PI for PXRD?", "who_pays", None),
    ("And the fee?", "charges_generic", None),
    ("Show how students join my wallet.", "students", None),
    ("link wallet", "students", None),
    ("how to link wallet", "students", None),
    ("could not find supervisor name while linking wallet", "students", None),
    ("Summarize Booking Assistant capabilities.", "help", None),
    ("Help me find suitable equipment for my sample.", "find_equipment", None),
]

STILL_OTHER_LAYERS = [
    "What are the charges for XRD?",
    "I need FESEM tomorrow - what are my options?",
    "fesem",
    "Estimate the cost of booking FESEM for 2 hours.",
    "Is XRD free next monday",
    "What is my Remote Analysis status?",
    "hello",
]


@pytest.mark.parametrize("text,intent,params", NEW_INTENTS)
def test_new_intents(text, intent, params):
    d = detect(text)
    assert d is not None and d.intent == intent, (text, d)
    for k, v in (params or {}).items():
        assert d.params.get(k) == v, (text, d.params)


@pytest.mark.parametrize("text", STILL_OTHER_LAYERS)
def test_other_layers_keep_their_questions(text):
    d = detect(text)
    assert d is None or d.intent not in {"glossary", "estimate", "analysis_help", "smalltalk", "vague", "prepare"}, (text, d)


def test_existing_answers_unchanged():
    assert detect("List my recent bookings.").intent == "bookings"
    assert detect("how much money is left").intent == "balance"
    assert detect("What is my next XRD booking?").intent == "bookings"
    assert detect("Where can I download my results?").intent == "results"
    assert detect("proforma invoice").intent == "invoices"


# =============================================================================== ba_help action (pure)


class TestHelpAction:
    def test_parse_minimal(self):
        assert A.parse({"type": "ba_help", "payload": {"code": "slot_taken"}}) == {"type": "ba_help", "payload": {"code": "slot_taken"}}

    def test_parse_full(self):
        out = A.parse({"type": "ba_help", "payload": {
            "code": "charge_error", "equipment_id": "7", "message": "  Charge   failed ", "missing_fields": ["No. of samples"],
            "date": "2026-10-09",
        }})
        assert out["payload"] == {"code": "charge_error", "equipment_id": 7, "message": "Charge failed",
                                  "missing_fields": ["No. of samples"], "date": "2026-10-09"}

    @pytest.mark.parametrize("payload", [
        {},
        {"code": "explode"},
        {"code": "slot_taken", "user_id": 3},
        {"code": "slot_taken", "message": "x" * 401},
        {"code": "slot_taken", "missing_fields": ["a"] * 13},
        {"code": "slot_taken", "missing_fields": [{"a": 1}]},
        {"code": "slot_taken", "date": "tomorrow"},
        {"code": "slot_taken", "equipment_id": "abc"},
    ])
    def test_rejects(self, payload):
        with pytest.raises(A.InvalidAssistantAction):
            A.parse({"type": "ba_help", "payload": payload})

    @pytest.mark.parametrize("message,code", [
        ("The selected slot is no longer available. Please select another available slot.", "slot_taken"),
        ("Individual Weekly quota exceeded: current usage 240 min + requested 120 min = 360 min; configured limit 300 min; "
         "remaining before this request 60 min.", "quota_exceeded"),
        ("You don't have access to any wallet. Please link to your supervisor's wallet.", "no_wallet"),
        ("Insufficient wallet balance for this booking.", "insufficient_funds"),
        ("Something odd happened", "booking_failed"),
    ])
    def test_classify(self, message, code):
        assert help_offer.classify(message) == code

    def test_reset_dates(self):
        assert help_offer.reset_date("weekly", date(2026, 10, 1)) == date(2026, 10, 5)  # Thu -> Mon
        assert help_offer.reset_date("weekly", date(2026, 10, 5)) == date(2026, 10, 12)  # Mon -> next Mon
        assert help_offer.reset_date("monthly", date(2026, 12, 15)) == date(2027, 1, 1)


# =============================================================================== DB-backed


@pytest.fixture
def lab(db, flags):  # noqa: F811
    return _Lab()


def _help(lab, conv, payload, user=None):
    return _post(lab, conv, "Need help with my booking", action={"type": "ba_help", "payload": payload}, user=user)


def _body(resp):
    assert resp.status_code == 200, resp.content
    return resp.json()


def _meta(body):
    return body["message"]["metadata"]


def _labels(body):
    return [a.get("label") for a in body["message"].get("suggested_actions") or []]


@pytest.mark.django_db
class TestFailureHelp:
    def test_slot_taken_offers_next_free_slots(self, lab):
        start = lab.future(days=2, hour=11)
        lab.slot(lab.xrd, start)
        conv = _new_conv(lab)
        resp = _help(lab, conv, {"code": "slot_taken", "equipment_id": lab.xrd.pk, "date": start.date().isoformat()})
        card, body = _card(resp, "ba_slots")
        assert card["equipment_id"] == lab.xrd.pk and card["days"], card
        assert "Someone else booked" in body["message"]["content"]
        assert _meta(body)["failure_context"] == {"code": "slot_taken", "equipment_id": lab.xrd.pk}
        assert _meta(body)["llm_used"] is False

    def test_slot_taken_offers_waitlist_when_enabled(self, lab):
        lab.xrd.waitlist_queue_depth = 3
        lab.xrd.save(update_fields=["waitlist_queue_depth"])
        conv = _new_conv(lab)
        body = _body(_help(lab, conv, {"code": "no_slots", "equipment_id": lab.xrd.pk}))
        assert "Join the waitlist" in _labels(body)

    def test_quota_explains_remaining_and_reset(self, lab):
        conv = _new_conv(lab)
        msg = ("Individual Weekly quota exceeded: current usage 240 min + requested 120 min = 360 min; "
               "configured limit 300 min; remaining before this request 60 min.")
        d = timezone.localdate() + timedelta(days=3)
        body = _body(_help(lab, conv, {"code": "quota_exceeded", "equipment_id": lab.xrd.pk, "message": msg, "date": d.isoformat()}))
        text = body["message"]["content"]
        assert "**2 h**" in text and "**1 h**" in text and "**4 h**" in text and "**5 h**" in text
        reset = help_offer.reset_date("weekly", d)
        assert f"{reset:%d %b %Y}" in text
        assert "Free slots after the reset" in _labels(body)
        assert _meta(body)["intent"] == "assistant:help_quota_exceeded"

    def test_no_wallet_student_gets_link_steps_and_supervisor_note(self, lab):
        conv = _new_conv(lab)
        body = _body(_help(lab, conv, {"code": "no_wallet", "equipment_id": lab.xrd.pk}))
        text = body["message"]["content"]
        assert "Request to Join Wallet" in text and "Invite your supervisor" in text
        link = next(a for a in body["message"]["suggested_actions"] if a.get("label") == "Link my supervisor's wallet")
        assert link["href"] == "/wallet"

    def test_insufficient_funds_student_asks_supervisor(self, lab):
        conv = _new_conv(lab)
        body = _body(_help(lab, conv, {"code": "insufficient_funds", "equipment_id": lab.xrd.pk}))
        assert "supervisor" in body["message"]["content"]
        assert {"Wallet balance", "How to recharge", "Back to booking form"} <= set(_labels(body))
        assert _meta(body)["intent"] == "assistant:help_insufficient_funds"

    def test_charge_error_lists_missing_fields(self, lab):
        conv = _new_conv(lab)
        body = _body(_help(lab, conv, {"code": "charge_error", "equipment_id": lab.xrd.pk, "missing_fields": ["Sample type", "No. of samples"]}))
        assert "Sample type, No. of samples" in body["message"]["content"]
        assert "Required inputs" in _labels(body)

    def test_hidden_equipment_is_not_revealed(self, lab):
        icp, _ = lab.hidden("Agilent 7900 ICP-MS", "ICPMS-9")
        conv = _new_conv(lab)
        body = _body(_help(lab, conv, {"code": "slot_taken", "equipment_id": icp.pk}))
        assert "Agilent" not in body["message"]["content"]
        assert _meta(body)["failure_context"]["equipment_id"] is None

    def test_invalid_payload_is_rejected(self, lab):
        conv = _new_conv(lab)
        assert _help(lab, conv, {"code": "drop_tables"}).status_code == 400


@pytest.mark.django_db
class TestUnansweredThemes:
    @pytest.mark.parametrize("text,intent", [
        ("What is FWHM?", "assistant:glossary"),
        ("Lab access hours policy", "assistant:lab_hours"),
        ("Can I?", "assistant:vague"),
        ("What files are available?", "assistant:analysis_files"),
        ("Can I analyze my data remotely?", "assistant:analysis_remote"),
        ("What software can I use for XRD?", "assistant:analysis_software"),
        ("How much does 5 XRD samples cost?", "assistant:estimate"),
        ("Am I charged as wallet owner PI for XRD?", "assistant:who_pays"),
        ("What should I prepare before my XRD booking?", "assistant:prepare"),
        ("Submit by when?", "assistant:policy_sample_submission"),
    ])
    def test_answered_without_llm_with_chips(self, lab, text, intent):
        conv = _new_conv(lab)
        body = _body(_post(lab, conv, text))
        meta = _meta(body)
        assert meta["intent"] == intent, (text, meta.get("intent"), body["message"]["content"][:200])
        assert meta["llm_used"] is False
        assert len(body["message"]["suggested_actions"]) >= 2, (text, _labels(body))

    def test_estimate_uses_charge_engine_for_n_samples(self, lab):
        conv = _new_conv(lab)
        body = _body(_post(lab, conv, "How much does 5 XRD samples cost?"))
        text = body["message"]["content"]
        assert "5 samples" in text and lab.xrd.name in text and "₹" in text

    def test_follow_up_fee_uses_conversation_equipment(self, lab):
        conv = _new_conv(lab)
        _body(_post(lab, conv, "What should I prepare before my XRD booking?"))
        body = _body(_post(lab, conv, "And the fee?"))
        assert _meta(body)["intent"] == "assistant:info_charges"
        assert lab.xrd.name in body["message"]["content"]

    def test_prepare_uses_my_upcoming_booking(self, lab):
        b, _ = lab.booking(lab.student, lab.xrd, lab.future(days=1))
        conv = _new_conv(lab)
        body = _body(_post(lab, conv, "My XRD booking is tomorrow. What will it cost and what should I prepare?"))
        text = body["message"]["content"]
        assert b.virtual_booking_id in text and "₹200.00" in text
        assert "Sample submission" in text

    def test_software_lists_catalog_mapped_to_equipment(self, lab):
        from iic_booking.remote_analysis.catalog_models import AnalysisSoftwareCatalog, EquipmentAnalysisSoftware

        cat = AnalysisSoftwareCatalog.objects.create(name="HighScore Plus", typical_usage="Phase identification",
                                                     accepted_file_types=[".xrdml", ".raw"])
        EquipmentAnalysisSoftware.objects.create(equipment=lab.xrd, catalog=cat, is_default=True)
        conv = _new_conv(lab)
        body = _body(_post(lab, conv, "What software can I use for XRD?"))
        assert "HighScore Plus" in body["message"]["content"] and ".xrdml" in body["message"]["content"]

    def test_dm4_falls_back_to_general_note(self, lab):
        conv = _new_conv(lab)
        text = _body(_post(lab, conv, "Which software for .dm4 files?"))["message"]["content"]
        assert "DigitalMicrograph" in text and "ImageJ" in text


@pytest.mark.django_db
class TestNextStepsAndStarters:
    def test_policy_answer_gets_topped_up_to_three_chips(self, lab):
        conv = _new_conv(lab)
        body = _body(_post(lab, conv, "Submit by when?"))
        labels = _labels(body)
        assert len(labels) == 3 and len(set(labels)) == 3, labels

    def test_equipment_answer_gets_equipment_chips(self, lab):
        conv = _new_conv(lab)
        body = _body(_post(lab, conv, "How should I prepare an XRD sample?"))
        assert len(_labels(body)) >= 3

    def test_choice_cards_are_not_padded(self, lab):
        from iic_booking.research_copilot.services.assistant.next_steps import ensure_next_steps

        out = ensure_next_steps(lab.student, [], cards=[{"type": "ba_equipment_options", "items": []}])
        assert out == []
        assert ensure_next_steps(lab.student, [], metadata={"proposal_id": "x"}) == []

    def test_bootstrap_starter_chips_by_role(self, lab):
        from iic_booking.users.models.user_type import UserType
        from iic_booking.users.tests.factories import UserFactory

        body = _client(lab.student).get(f"{BASE}/bootstrap/").json()
        labels = [a["label"] for a in body["starter_actions"]]
        assert 4 <= len(labels) <= 6 and "Link supervisor's wallet" in labels
        op = UserFactory(user_type=UserType.OPERATOR, department=lab.department, is_active=True)
        op_labels = [a["label"] for a in _client(op).get(f"{BASE}/bootstrap/").json()["starter_actions"]]
        assert "Pending approvals" in op_labels and "Link supervisor's wallet" not in op_labels


@pytest.mark.django_db
class TestFeedbackComment:
    def test_thumbs_down_then_comment_updates_same_row(self, lab):
        from iic_booking.research_copilot.models import MessageFeedback

        conv = _new_conv(lab)
        msg_id = _body(_post(lab, conv, "What is FWHM?"))["message"]["id"]
        c = _client(lab.student)
        first = c.post(f"{BASE}/conversations/{conv}/feedback/", {"rating": "down", "message_id": msg_id}, format="json")
        assert first.status_code == 201
        fid = first.json()["id"]
        second = c.post(f"{BASE}/conversations/{conv}/feedback/",
                        {"rating": "down", "message_id": msg_id, "feedback_id": fid, "comment": "Scherrer example"}, format="json")
        assert second.status_code == 201 and second.json()["id"] == fid
        rows = list(MessageFeedback.objects.filter(conversation_id=conv))
        assert len(rows) == 1 and rows[0].comment == "Scherrer example" and rows[0].rating == "down"
        assert rows[0].intent == "assistant:glossary"

    def test_cannot_edit_someone_elses_feedback(self, lab):
        from iic_booking.research_copilot.models import MessageFeedback

        conv = _new_conv(lab)
        fid = _client(lab.student).post(f"{BASE}/conversations/{conv}/feedback/", {"rating": "down"}, format="json").json()["id"]
        other_conv = _new_conv(lab, user=lab.other)
        _client(lab.other).post(f"{BASE}/conversations/{other_conv}/feedback/",
                                {"rating": "down", "feedback_id": fid, "comment": "hijack"}, format="json")
        assert MessageFeedback.objects.get(pk=fid).comment == ""

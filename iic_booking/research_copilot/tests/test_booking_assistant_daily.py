"""Booking Assistant day-to-day layer: intent table, booking cards with next-step chips, confirm-gated changes,
role-aware help, wallet/recharge answers, clean sources and the latency guards."""

from __future__ import annotations

import time
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from iic_booking.research_copilot.services.assistant import actions as A
from iic_booking.research_copilot.services.assistant import bookings as B
from iic_booking.research_copilot.services.assistant.intents import detect, normalize
from iic_booking.research_copilot.tests.test_booking_assistant import _Lab, _card, _new_conv, _post, flags  # noqa: F401
from iic_booking.research_copilot.tests.test_copilot_intelligence import _client

# =============================================================================== intent table (pure)

INTENT_TABLE = [
    ("List my recent bookings.", "bookings", {"scope": "recent"}),
    ("show my upcoming bookings", "bookings", {"scope": "upcoming"}),
    ("my bokings", "bookings", None),
    ("mera booking dikhao", "bookings", None),
    ("what is my next booking?", "bookings", {"scope": "upcoming", "limit": 1}),
    ("past bookings", "bookings", {"scope": "past"}),
    ("my cancelled bookings", "bookings", None),
    ("status of booking IICSQUID202600004", "booking_details", {"ref": "IICSQUID202600004"}),
    ("show details of the first one", "booking_details", {"ordinal": 1}),
    ("cancel the second one", "cancel", {"ordinal": 2}),
    ("Cancel my next booking.", "cancel", {"next": True}),
    ("cancel IICSQUID202600004", "cancel", {"ref": "IICSQUID202600004"}),
    ("booking radd karo", "cancel", None),
    ("how do I cancel a booking?", "howto_cancel", None),
    ("Reschedule my next booking.", "reschedule", {"next": True}),
    ("edit parameters of the first one", "edit", {"ordinal": 1}),
    ("change number of samples", "edit", None),
    ("message the lab", "message_lab", None),
    ("Show my latest results.", "results", None),
    ("wallet balance", "balance", None),
    ("walet balence kitna hai", "balance", None),
    ("how much money is left", "balance", None),
    ("how to recharge wallet", "recharge", None),
    ("wallet recharge kaise kare", "recharge", None),
    ("add money to wallet", "recharge", None),
    ("Show my recent wallet transactions.", "transactions", None),
    ("proforma invoice", "invoices", None),
    ("join waitlist", "waitlist", {"join": True}),
    ("urgent booking", "urgent", None),
    ("booking templates", "templates", None),
    ("rate my experience", "rate", None),
    ("I want to raise a support ticket.", "ticket_create", None),
    ("my tickets", "tickets", None),
    ("set spending limit for a student", "students", None),
    ("my research", "my_research", None),
    ("today's bookings on my equipment", "staff_today", None),
    ("pending approvals", "staff_approvals", None),
    ("urgent requests queue on my equipment", "staff_urgent", None),
    ("waitlist queue on my equipment", "staff_waitlist", None),
    ("what can you do", "help", None),
]

NOT_DAILY = [
    "What are the charges for XRD?",
    "I need FESEM tomorrow - what are my options?",
    "fesem",
    "How do I book equipment on the portal?",
    "How do I prepare a sample for FESEM?",
    "Estimate the cost of booking FESEM for 2 hours.",
    "Which technique should I use for elemental composition?",
    "Is XRD free next monday",
]


@pytest.mark.parametrize("text,intent,params", INTENT_TABLE)
def test_intent_table(text, intent, params):
    d = detect(text)
    assert d is not None and d.intent == intent, (text, d)
    for k, v in (params or {}).items():
        assert d.params.get(k) == v, (text, d.params)


@pytest.mark.parametrize("text", NOT_DAILY)
def test_equipment_and_knowledge_questions_are_left_to_other_layers(text):
    assert detect(text) is None, text


@pytest.mark.parametrize("prompt,intent", [
    ("Show my upcoming bookings", "bookings"),
    ("Results of my last booking", "results"),
    ("Manage my students and spending limits", "students"),
    ("Where are my invoices?", "invoices"),
    ("Show my support tickets.", "tickets"),
    ("Open reports", "reports"),
    ("Pending approvals on my equipment", "staff_approvals"),
    ("How do I recharge my wallet?", "recharge"),
])
def test_bootstrap_prompts_route_to_instant_answers(prompt, intent):
    from iic_booking.research_copilot.constants import SUGGESTED_PROMPTS

    assert any(prompt in v for v in SUGGESTED_PROMPTS.values()) or prompt in {"Show my support tickets.", "Where are my invoices?"}
    d = detect(prompt)
    assert d is not None and d.intent == intent, (prompt, d)


def test_normalize_typos_and_hinglish():
    assert normalize("Mera walet balence kitna hai?") == "my wallet balance how much is"
    assert "cancel" in normalize("booking radd karo")


# =============================================================================== fixtures


class _DailyLab(_Lab):
    def staff(self, user_type):
        from iic_booking.users.tests.factories import UserFactory

        return UserFactory(user_type=user_type, department=self.department, is_active=True)


@pytest.fixture
def lab(db, flags):  # noqa: F811
    return _DailyLab()


def _body(resp):
    assert resp.status_code == 200, resp.content
    return resp.json()


def _items(body):
    cards = body["message"]["metadata"].get("cards") or body.get("cards") or []
    return [i for c in cards if c.get("type") == "ba_bookings" for i in c.get("items") or []]


def _ops(row):
    return [a["payload"]["op"] for a in row.get("actions") or [] if a.get("action_type") == A.BOOKING]


# =============================================================================== bookings + chips


@pytest.mark.django_db
class TestBookingCards:
    def test_recent_bookings_card_with_next_step_chips(self, lab):
        b, _ = lab.booking(lab.student, lab.xrd, lab.future(days=6))
        lab.booking(lab.other, lab.fesem, lab.future(days=7))
        conv = _new_conv(lab)
        started = time.perf_counter()
        body = _body(_post(lab, conv, "List my recent bookings."))
        elapsed = time.perf_counter() - started
        assert elapsed < 1.5, elapsed
        items = _items(body)
        assert [i["booking_id"] for i in items] == [b.pk], "only the caller's own bookings"
        ops = _ops(items[0])
        assert {"reschedule", "cancel"} <= set(ops)
        assert B.NEXT_PROMPT in body["message"]["content"]
        assert body["message"]["citations"] == []
        assert "To book equipment" not in body["message"]["content"]

    def test_completed_booking_offers_after_service_chips_only(self, lab):
        b, slots = lab.booking(lab.student, lab.xrd, timezone.now() - timedelta(days=3))
        b.status = "COMPLETED"
        b.save(update_fields=["status"])
        conv = _new_conv(lab)
        ops = _ops(_items(_body(_post(lab, conv, "show my past bookings")))[0])
        assert "cancel" not in ops and "reschedule" not in ops
        assert "invoice" in ops or "rebook" in ops

    def test_cancel_the_second_one_goes_through_confirmation(self, lab):
        b1, _ = lab.booking(lab.student, lab.xrd, lab.future(days=6))
        b2, _ = lab.booking(lab.student, lab.tem, lab.future(days=8))
        conv = _new_conv(lab)
        items = _items(_body(_post(lab, conv, "show my upcoming bookings")))
        assert [i["booking_id"] for i in items] == [b1.pk, b2.pk]
        body = _body(_post(lab, conv, "cancel the second one"))
        from iic_booking.research_copilot.models import Conversation
        from iic_booking.research_copilot.services.intelligence import messages as M
        from iic_booking.research_copilot.services.intelligence import state as intel_state

        assert body["message"]["metadata"].get("message_type") == M.CANCELLATION_SELECTION
        values = [a["choice"]["value"] for a in body["message"]["suggested_actions"] if a.get("choice")]
        assert "entire" in values and "keep" in values
        assert intel_state.load(Conversation.objects.get(pk=conv)).get("booking_id") == b2.pk
        assert b1.virtual_booking_id not in body["message"]["content"]
        for b in (b1, b2):
            b.refresh_from_db()
            assert b.status == "BOOKED"

    def test_typed_confirm_never_executes_a_cancel(self, lab):
        b, _ = lab.booking(lab.student, lab.xrd, lab.future(days=6))
        conv = _new_conv(lab)
        _post(lab, conv, "cancel my booking")
        _post(lab, conv, "confirm")
        b.refresh_from_db()
        assert b.status == "BOOKED"

    @pytest.mark.parametrize("op", ["cancel", "reschedule"])
    def test_chip_change_requires_explicit_confirm(self, lab, op):
        b, _ = lab.booking(lab.student, lab.xrd, lab.future(days=6), slot_count=1)
        conv = _new_conv(lab)
        body = _body(_post(lab, conv, op, action={"type": A.BOOKING, "payload": {"booking_id": b.pk, "op": op}}))
        assert body["message"]["content"]
        assert not any(a.get("id") == "confirm_proposal" and a.get("executed") for a in body["message"]["suggested_actions"])
        b.refresh_from_db()
        assert b.status == "BOOKED"

    def test_edit_is_a_deep_link_only(self, lab):
        b, _ = lab.booking(lab.student, lab.xrd, lab.future(days=6), input_values={"A": "3"})
        conv = _new_conv(lab)
        body = _body(_post(lab, conv, "edit", action={"type": A.BOOKING, "payload": {"booking_id": b.pk, "op": "edit"}}))
        hrefs = [a.get("href") for a in body["message"]["suggested_actions"]]
        assert f"/my-bookings?booking={b.pk}&edit_inputs=1" in hrefs
        b.refresh_from_db()
        assert b.input_values == {"A": "3"} and b.status == "BOOKED"

    def test_foreign_booking_chip_is_refused(self, lab):
        foreign, _ = lab.booking(lab.other, lab.xrd, lab.future(days=6))
        conv = _new_conv(lab)
        body = _body(_post(lab, conv, "cancel", action={"type": A.BOOKING, "payload": {"booking_id": foreign.pk, "op": "cancel"}}))
        assert "couldn't find that booking" in body["message"]["content"]
        assert foreign.virtual_booking_id not in body["message"]["content"]
        foreign.refresh_from_db()
        assert foreign.status == "BOOKED"

    def test_bad_booking_op_is_rejected(self, lab):
        b, _ = lab.booking(lab.student, lab.xrd, lab.future(days=6))
        conv = _new_conv(lab)
        resp = _post(lab, conv, "x", action={"type": A.BOOKING, "payload": {"booking_id": b.pk, "op": "delete_everything"}})
        assert resp.status_code == 400
        with pytest.raises(A.InvalidAssistantAction):
            A.parse({"type": A.BOOKING, "payload": {"booking_id": b.pk}})
        with pytest.raises(A.InvalidAssistantAction):
            A.parse({"type": A.BOOKING, "payload": {"booking_id": b.pk, "op": "details", "user_id": 1}})

    def test_closed_window_offers_message_and_ticket_not_cancel(self, lab):
        b, _ = lab.booking(lab.student, lab.xrd, timezone.now() + timedelta(hours=5), slot_count=1)
        elig = B.eligibility(b)
        assert elig["active"] and not elig["cancel"] and not elig["reschedule"]
        conv = _new_conv(lab)
        body = _body(_post(lab, conv, "cancel", action={"type": A.BOOKING, "payload": {"booking_id": b.pk, "op": "cancel"}}))
        assert "can't be cancelled" in body["message"]["content"]
        b.refresh_from_db()
        assert b.status == "BOOKED"


# =============================================================================== roles, wallet, help


@pytest.mark.django_db
class TestRolesAndWallet:
    def test_staff_help_offers_todays_bookings(self, lab):
        manager = lab.staff("manager")
        conv = _new_conv(lab, user=manager)
        body = _body(_post(lab, conv, "help", user=manager))
        labels = [a["label"] for a in body["message"]["suggested_actions"]]
        assert "Today's bookings on my equipment" in labels
        student_body = _body(_post(lab, _new_conv(lab), "help"))
        assert "My upcoming bookings" in [a["label"] for a in student_body["message"]["suggested_actions"]]

    def test_bootstrap_is_role_specific(self, lab):
        manager = lab.staff("manager")
        staff_body = _client(manager).get("/api/v1/research-copilot/bootstrap/").json()
        assert staff_body["command_groups"][0]["id"] == "lab"
        assert "Today's bookings on my equipment" in staff_body["suggested_prompts"]
        student_body = _client(lab.student).get("/api/v1/research-copilot/bootstrap/").json()
        assert [g["id"] for g in student_body["command_groups"]] == ["booking", "research", "account", "help"]
        assert "Show my upcoming bookings" in student_body["suggested_prompts"]

    def test_staff_today_lists_bookings_on_handled_equipment(self, lab):
        manager = lab.staff("manager")
        start = timezone.localtime(timezone.now()).replace(minute=0, second=0, microsecond=0)
        b, _ = lab.booking(lab.student, lab.xrd, start, slot_count=1)
        with patch("iic_booking.research_copilot.services.assistant.daily.staff_equipment_ids", return_value=[lab.xrd.pk]):
            body = _body(_post(lab, _new_conv(lab, user=manager), "today's bookings on my equipment", user=manager))
        assert b.virtual_booking_id in str(body["message"])

    def test_student_cannot_use_staff_queue(self, lab):
        b, _ = lab.booking(lab.other, lab.xrd, lab.future(days=1))
        body = _body(_post(lab, _new_conv(lab), "pending approvals on my equipment"))
        assert b.virtual_booking_id not in str(body["message"])

    def test_wallet_balance_has_follow_up_chips(self, lab):
        lab.fund(balance="2500.00")
        body = _body(_post(lab, _new_conv(lab), "What is my wallet balance?"))
        assert "2,500" in body["message"]["content"] or "2500" in body["message"]["content"]
        labels = [a["label"] for a in body["message"]["suggested_actions"]]
        assert "View transactions" in labels

    def test_recharge_steps_follow_live_flags(self, lab):
        from iic_booking.research_copilot.services.assistant import daily

        flags_ = {"direct_cash_recharge_enabled": True, "online_gateway_recharge_enabled": False,
                  "project_grant_recharge_enabled": True}
        with patch("iic_booking.users.models.wallet_sric_settings.wallet_mode_flags", return_value=flags_):
            student_text = daily.recharge_steps_text(lab.student)
            faculty_text = daily.recharge_steps_text(lab.staff("faculty"))
        assert "Direct Cash Deposit / Bank Transfer" in student_text
        assert "Pay online** — Awaiting Competent Authority Approval" in student_text
        assert "Receipt upload is no longer used" in student_text
        assert "Project Grant" not in student_text and "Project Grant" in faculty_text
        assert "SBIePay" not in student_text

    def test_ticket_request_is_not_auto_created(self, lab):
        from iic_booking.support.models import Ticket

        before = Ticket.objects.count()
        body = _body(_post(lab, _new_conv(lab), "I want to raise a support ticket."))
        assert body["message"]["escalate_hint"] is True
        assert Ticket.objects.count() == before


# =============================================================================== formatting + latency guards


def test_safe_citation_url_drops_internal_uris():
    from iic_booking.research_copilot.services.rag import safe_citation_url

    assert safe_citation_url("seed://What's New — October 2026") == ""
    assert safe_citation_url("/admin-settings/knowledge?doc=1") == ""
    assert safe_citation_url("s3://bucket/key") == ""
    assert safe_citation_url("//evil.example") == ""
    assert safe_citation_url("/booking-templates") == "/booking-templates"
    assert safe_citation_url("https://equip.iitr.ac.in/x") == "https://equip.iitr.ac.in/x"


def test_sources_footer_is_stripped_and_citations_filtered():
    from iic_booking.research_copilot.services import conversation as conv
    from iic_booking.research_copilot.services.rag import Citation, citations_as_dicts

    reply = "Answer.\n\n---\n**Sources**\n- [Booking Templates](/booking-templates)\n- [What's New](seed://What's New)\n"
    assert conv._strip_sources_tail(reply) == "Answer."
    cites = [
        Citation(source_id="1", title="What's New", snippet="x", score=0.9, url="seed://What's New"),
        Citation(source_id="2", title="Weak", snippet="x", score=0.4, url="/wallet"),
    ]
    shown = citations_as_dicts(conv._display_citations(cites))
    assert [c["title"] for c in shown] == ["What's New"]
    assert all("seed://" not in c["url"] for c in shown)


@pytest.mark.django_db
def test_planner_is_not_called_without_a_key(lab, settings):
    settings.BOOKING_ASSISTANT_LLM_PLANNER = "on"
    settings.OPENAI_API_KEY = ""
    from iic_booking.research_copilot.services.assistant import planner

    with patch.object(planner, "_planner_gateway") as gw:
        _post(lab, _new_conv(lab), "zzqx frobnicate the thing")
    gw.assert_not_called()
    assert planner.planner_enabled() is False


@pytest.mark.django_db
def test_llm_outage_gives_quick_useful_fallback_and_trips_breaker(lab, settings):
    from iic_booking.research_copilot.services.llm_gateway import LLMResult

    settings.COPILOT_LLM_PROVIDER = "ollama"
    settings.COPILOT_PROVIDER = "ollama"
    failing = MagicMock(provider_name="ollama", model="llama3.2:3b", timeout_seconds=60)
    failing.generate.return_value = LLMResult(text="", model="llama3.2:3b", provider="ollama", error_category="timeout")
    with patch("iic_booking.research_copilot.services.conversation.get_gateway", return_value=failing):
        conv = _new_conv(lab)
        first = _body(_post(lab, conv, "zzqx frobnicate the thing"))
        second = _body(_post(lab, conv, "zzqy frobnicate another thing"))
    assert failing.generate.call_count == 1, "second message must skip the model that just timed out"
    assert failing.timeout_seconds <= 30
    for body in (first, second):
        content = body["message"]["content"]
        assert "To book equipment" not in content and "seed://" not in content
        assert len(body["message"]["suggested_actions"]) >= 3


@pytest.mark.django_db
def test_query_embeddings_are_cached_with_short_timeout(settings):
    from iic_booking.research_copilot.services.embeddings import OllamaEmbedding

    resp = MagicMock(status_code=200)
    resp.json.return_value = {"embeddings": [[0.1, 0.2, 0.3]]}
    with patch("requests.post", return_value=resp) as post:
        emb = OllamaEmbedding(base_url="http://ollama:11434", model="nomic-embed-text", timeout=120)
        assert emb.embed_query("How do I recharge?") == [0.1, 0.2, 0.3]
        assert emb.embed_query("how do i   recharge?") == [0.1, 0.2, 0.3]
    assert post.call_count == 1
    assert post.call_args.kwargs["timeout"] <= 8
    assert emb.timeout == 120

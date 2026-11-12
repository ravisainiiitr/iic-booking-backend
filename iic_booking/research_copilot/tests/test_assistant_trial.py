"""IITR Equipment Booking Assistant trial scenarios: the same words mean different things for each role, nothing changes without a
button press, and the server refuses what a role may not do."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from iic_booking.research_copilot.tests.test_booking_assistant import _Lab, _new_conv, _post, flags  # noqa: F401
from iic_booking.research_copilot.tests.test_copilot_intelligence import BASE, _client
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

PORTAL = "ba_portal"


def _user(user_type, **kw):
    return UserFactory(user_type=user_type, is_active=True, admin_approved=True, **kw)


def _ask(lab, text, user, conv=None, action=None):
    conv = conv or _new_conv(lab, user)
    resp = _post(lab, conv, text, action=action, user=user)
    assert resp.status_code in (200, 201), resp.content
    message = resp.json()["message"]
    return message, conv


def _intent(message):
    return (message.get("metadata") or {}).get("intent") or ""


def _labels(message):
    return [a.get("label") or "" for a in message.get("suggested_actions") or []]


def _links(message):
    return [a.get("href") or (a.get("payload") or {}).get("href") or "" for a in message.get("suggested_actions") or []]


def _cards(message):
    return [c.get("type") for c in (message.get("metadata") or {}).get("cards") or []]


def _portal_action(message, equipment_id):
    for a in message.get("suggested_actions") or []:
        p = a.get("payload") or {}
        if p.get("equipment_id") == equipment_id:
            return a
    raise AssertionError(f"no button for equipment {equipment_id}: {_labels(message)}")


# --- Main Administrator ----------------------------------------------------------------------------------------------


@pytest.fixture
def lab(db, flags):
    return _Lab()


@pytest.fixture
def admin(db):
    return _user(UserType.ADMIN)


@pytest.mark.parametrize("text", [
    "exchange slot", "slot exchange", "change exchange settings", "configure exchange for equipment",
    "enable slot exchange", "where do I change exchange rules?",
])
def test_admin_exchange_phrasings_mean_the_slot_exchange_setting(lab, admin, text):
    message, _ = _ask(lab, text, admin)
    assert _intent(message) == "assistant:portal:slot_exchange", message["content"]
    assert "which instrument" in message["content"].lower()
    assert "ba_equipment_picker" in _cards(message)
    assert "No Conversation matches" not in message["content"]


def test_admin_picker_search_finds_the_instrument(lab, admin):
    res = _client(admin).get("/api/portal-actions/slot_exchange/equipment/", {"q": "FESEM"})
    assert res.status_code == 200, res.content
    ids = [r["id"] for r in res.json()["equipment"]]
    assert lab.fesem.pk in ids


def test_admin_selecting_an_instrument_offers_the_switch_and_settings_without_changing_it(lab, admin):
    message, conv = _ask(lab, "exchange slot", admin)
    button = _portal_action(message, lab.fesem.pk)
    message, _ = _ask(lab, button["label"], admin, conv=conv, action={"type": PORTAL, "payload": button["payload"]})
    assert f"/admin/section/equipment?edit={lab.fesem.pk}" in _links(message)
    assert "Nothing changes until you press the button" in message["content"]
    lab.fesem.refresh_from_db()
    assert lab.fesem.slot_exchange_enabled is False


def test_admin_typed_enable_never_changes_the_setting(lab, admin):
    message, conv = _ask(lab, "enable slot exchange for FESEM-01", admin)
    _ask(lab, "yes", admin, conv=conv)
    lab.fesem.refresh_from_db()
    assert lab.fesem.slot_exchange_enabled is False


@pytest.mark.parametrize("text, intent, path", [
    ("equipment under maintenance", "assistant:portal:disruptions", "/disruptions"),
    ("pending refund requests", "assistant:portal:refunds", "/accounts/refunds"),
    ("open booking calendar", "assistant:portal:booking_calendar", "/booking-calendar"),
])
def test_admin_pages_by_description(lab, admin, text, intent, path):
    message, _ = _ask(lab, text, admin)
    assert _intent(message) == intent, message["content"]
    assert any(h.startswith(path) for h in _links(message)), _links(message)


def test_admin_show_booking_by_id_resolves_any_booking(lab, admin):
    booking, _ = lab.booking(lab.student, lab.fesem, lab.future())
    message, _ = _ask(lab, f"show booking {booking.pk}", admin)
    assert booking.virtual_booking_id in message["content"] or str(booking.booking_id) in message["content"], message["content"]
    assert "couldn't find" not in message["content"].lower()


def test_unknown_booking_id_is_a_helpful_not_found(lab, admin):
    message, _ = _ask(lab, "show booking 987654", admin)
    assert "987654" in message["content"]
    assert "No Conversation matches" not in message["content"]


# --- OIC / Lab Operator ----------------------------------------------------------------------------------------------


def _oic_of(lab, eq, user_type=UserType.MANAGER):
    from iic_booking.equipment.models import EquipmentManager, EquipmentOperator

    user = _user(user_type, department=lab.department)
    if user_type == UserType.MANAGER:
        EquipmentManager.objects.create(equipment=eq, manager=user)
    else:
        EquipmentOperator.objects.create(equipment=eq, operator=user)
    return user


@pytest.mark.parametrize("user_type", [UserType.MANAGER, UserType.OPERATOR])
def test_oic_and_operator_exchange_slot_explains_who_can_change_it(lab, user_type):
    staff = _oic_of(lab, lab.fesem, user_type)
    message, _ = _ask(lab, "exchange slot", staff)
    assert _intent(message) == "assistant:portal:slot_exchange_admin_only", message["content"]
    assert "Main Administrator" in message["content"]
    assert "ba_equipment_picker" not in _cards(message)


def test_oic_equipment_choices_are_only_their_instruments(lab):
    oic = _oic_of(lab, lab.fesem)
    message, _ = _ask(lab, "block slots", oic)
    ids = {(a.get("payload") or {}).get("equipment_id") for a in message.get("suggested_actions") or []} - {None}
    assert ids == {lab.fesem.pk}
    res = _client(oic).get("/api/portal-actions/change_slot_status/equipment/", {"q": "XRD"})
    assert res.status_code == 200
    assert res.json()["equipment"] == []


def test_oic_switch_is_refused_server_side(lab):
    oic = _oic_of(lab, lab.fesem)
    payload = {"action": "slot_exchange", "equipment_id": lab.fesem.pk, "on": 1}
    message, _ = _ask(lab, "Turn on", oic, action={"type": PORTAL, "payload": payload})
    assert "not available" in message["content"].lower()
    lab.fesem.refresh_from_db()
    assert lab.fesem.slot_exchange_enabled is False


# --- Faculty / Student / External ------------------------------------------------------------------------------------


def _exchangeable(lab):
    lab.fesem.slot_exchange_enabled = True
    lab.fesem.save(update_fields=["slot_exchange_enabled"])


def test_student_exchange_slot_lists_only_their_eligible_bookings(lab):
    _exchangeable(lab)
    mine, _ = lab.booking(lab.student, lab.fesem, lab.future(days=4))
    not_exchangeable, _ = lab.booking(lab.student, lab.xrd, lab.future(days=4, hour=14))
    theirs, _ = lab.booking(lab.other, lab.fesem, lab.future(days=6))
    message, _ = _ask(lab, "exchange slot", lab.student)
    assert _intent(message) == "assistant:portal:slot_exchanges", message["content"]
    text = message["content"] + " ".join(_labels(message))
    assert mine.virtual_booking_id in text
    assert not_exchangeable.virtual_booking_id not in text
    assert theirs.virtual_booking_id not in text
    assert any(h.startswith("/slot-exchanges") for h in _links(message))
    assert "ba_equipment_picker" not in _cards(message)


def test_student_without_eligible_bookings_is_told_why(lab):
    message, _ = _ask(lab, "exchange slot", lab.student)
    assert _intent(message) == "assistant:portal:slot_exchanges"
    assert "no upcoming booking" in message["content"].lower()


def test_faculty_exchange_slot_is_their_own_bookings(lab):
    faculty = _user(UserType.FACULTY, department=lab.department)
    message, _ = _ask(lab, "exchange slot", faculty)
    assert _intent(message) == "assistant:portal:slot_exchanges"
    assert "/admin/section/equipment" not in " ".join(_links(message))


def test_external_user_is_offered_reschedule_instead(lab):
    external = _user(UserType.EXTERNAL)
    message, _ = _ask(lab, "exchange slot", external)
    assert _intent(message) == "assistant:portal:slot_exchange_external", message["content"]
    assert any(h.startswith("/my-bookings") for h in _links(message))


def test_student_cannot_press_the_admin_switch(lab):
    payload = {"action": "slot_exchange", "equipment_id": lab.fesem.pk, "on": 1}
    message, _ = _ask(lab, "Turn on", lab.student, action={"type": PORTAL, "payload": payload})
    assert "not available" in message["content"].lower()
    lab.fesem.refresh_from_db()
    assert lab.fesem.slot_exchange_enabled is False


# --- General ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["find FESEM", "FESEM", "field emission SEM"])
def test_equipment_search_variants_find_the_instrument(lab, text):
    message, _ = _ask(lab, text, lab.student)
    assert "FESEM" in message["content"] or "Field Emission" in message["content"], message["content"]


def test_equipment_search_by_manufacturer_or_model(lab):
    lab.xrd.make = "Bruker AXS"
    lab.xrd.model_information = "D8 Advance"
    lab.xrd.name = "X-ray Diffractometer"
    lab.xrd.save()
    from iic_booking.research_copilot.services.assistant import matching

    for q in ("Bruker", "D8 Advance"):
        m = matching.match_equipment(lab.student, q)
        assert lab.xrd.pk in [c.id for c in m.candidates] or (m.equipment and m.equipment.pk == lab.xrd.pk), q


def test_charges_question(lab):
    message, _ = _ask(lab, "how much does XRD cost?", lab.student)
    assert "charges" in _intent(message) or "₹" in message["content"], (_intent(message), message["content"])


def test_how_to_cancel(lab):
    message, _ = _ask(lab, "how do I cancel my booking?", lab.student)
    assert "cancel" in message["content"].lower()


def test_unknown_query_gets_a_clarification_not_an_error(lab):
    message, _ = _ask(lab, "qwzx plorb", lab.student)
    assert "No Conversation matches" not in message["content"]
    assert message["content"].strip()


def test_every_catalogue_page_is_a_portal_route():
    from iic_booking.equipment import portal_actions as P

    for a in P.ACTIONS:
        path = a.path.split("?")[0]
        if path:
            assert path.startswith("/") and " " not in path, a.key
            assert path.split("/")[1] in FRONTEND_TOP_LEVEL, (a.key, path)


def test_assistant_errors_are_handled(lab):
    with patch("iic_booking.research_copilot.services.assistant.portal.exchange_turn", side_effect=RuntimeError("boom")):
        conv = _new_conv(lab, lab.student)
        resp = _post(lab, conv, "exchange slot")
    assert resp.status_code in (200, 201)
    assert resp.json()["message"]["content"].strip()


def test_trial_metrics_count_intents_without_message_text(lab, admin):
    from iic_booking.research_copilot.models import AuditAction, CopilotAuditEvent

    _ask(lab, "exchange slot", admin)
    rows = CopilotAuditEvent.objects.filter(action=AuditAction.ASSISTANT_TURN)
    assert rows.count() == 1
    row = rows.get()
    assert row.detail["intent"] == "assistant:portal:slot_exchange"
    assert row.detail["outcome"] == "clarification"
    assert row.detail["recognised"] is True
    assert row.detail["roles"] == ["admin"]
    assert "exchange slot" not in str(row.detail) + row.message


def test_trial_metrics_endpoint_is_main_admin_only(lab, admin):
    _ask(lab, "exchange slot", admin)
    _ask(lab, "exchange slot", lab.student)
    assert _client(lab.student).get(f"{BASE}/assistant/metrics/").status_code == 403
    res = _client(admin).get(f"{BASE}/assistant/metrics/", {"days": 7})
    assert res.status_code == 200, res.content
    body = res.json()
    assert body["turns"] >= 2
    assert body["recognition_rate"] == 1.0
    assert {"intent": "assistant:portal:slot_exchange", "count": 1} in body["top_intents"]
    assert set(body["feedback"]) == {"up", "down"}


# Top-level route segments of the frontend router (src/App.tsx); a catalogue page outside these would 404.
FRONTEND_TOP_LEVEL = {
    "accounts",
    "admin",
    "admin-settings",
    "analysis-charges",
    "analysis-launch",
    "analysis-workspace",
    "app",
    "app-download",
    "auth",
    "availability",
    "book-equipment",
    "booking-attempt-logs",
    "booking-calendar",
    "booking-management",
    "booking-templates",
    "bookings",
    "calendar-colors",
    "calls",
    "change-slot-status",
    "content-management",
    "courier-tracking",
    "dashboard",
    "department-sync",
    "deployment-center",
    "device-provisioning",
    "disruptions",
    "duty",
    "equipment",
    "equipment-flash-messages",
    "equipment-lifecycle",
    "equipment-references",
    "equipment-waitlist",
    "equipments",
    "extension-decision",
    "external-checkout",
    "external-special-requests",
    "find-instrument",
    "inventory-management",
    "laboratory-infrastructure",
    "leave-management",
    "login",
    "manage",
    "multi-mode-equipment",
    "my-authorisations",
    "my-booking-attempts",
    "my-bookings",
    "my-duty",
    "my-nomination-requests",
    "my-publications",
    "my-research",
    "my-results",
    "my-trainings",
    "my-urgent-requests",
    "notice-board-requests",
    "notifications",
    "oic",
    "oic-leave-management",
    "oic-substitute",
    "organization",
    "page",
    "periodic-table",
    "procurement",
    "procurement-workflow",
    "profile",
    "proforma-invoice",
    "programme-extension",
    "propose-equipment",
    "publication-claims",
    "publications",
    "registration-approvals",
    "registration-decision",
    "remote-analysis",
    "repeat-sample-requests",
    "reports",
    "rewards",
    "shared-data",
    "slot-exchanges",
    "staff",
    "student-management",
    "ta-assignments",
    "ta-nomination-call",
    "ta-nominations-log",
    "team-calendar",
    "temporary-oic",
    "tickets",
    "training",
    "unsubscribe",
    "ups-health",
    "urgent-requests",
    "urgent-requests-wallet",
    "user-guide",
    "user-management",
    "verify",
    "wallet",
}

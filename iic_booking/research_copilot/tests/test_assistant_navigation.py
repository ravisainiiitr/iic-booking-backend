"""IITR Assistant as a portal navigator: everyday phrasings reach the right page instead of an equipment search."""

from __future__ import annotations

import pytest

from iic_booking.research_copilot.services.assistant.intents import detect
from iic_booking.research_copilot.tests.test_booking_assistant import _Lab, _new_conv, _post, flags  # noqa: F401

pytestmark = pytest.mark.django_db


def _ask(lab, text, user=None):
    conv = _new_conv(lab, user)
    message = _post(lab, conv, text, user=user).json()["message"]
    return message, [a.get("label") for a in message.get("suggested_actions") or []]


@pytest.mark.parametrize("text", ["exchange slot", "slot exchange", "swap my slot", "exchange my booking",
                                  "how do I exchange my slot with someone"])
def test_slot_exchange_phrasings_open_slot_exchanges(flags, text):
    message, actions = _ask(_Lab(), text)
    assert message["metadata"]["intent"] == "assistant:portal:slot_exchanges", message["content"]
    assert "Open slot exchanges" in actions
    assert not any(label.startswith("Find equipment for") for label in actions)


def test_a_single_word_still_offers_the_page_rather_than_no_equipment_found(flags):
    message, actions = _ask(_Lab(), "exchange")
    assert message["metadata"]["intent"] == "assistant:portal:slot_exchanges"
    assert "couldn't find any equipment" not in message["content"]


@pytest.mark.parametrize("text", ["swap my slot", "exchange slot"])
def test_swapping_is_not_read_as_a_booking_list(text):
    found = detect(text)
    assert found is None or found.intent != "bookings"


def test_my_bookings_is_still_a_booking_list(flags):
    message, _ = _ask(_Lab(), "my bookings")
    assert message["metadata"]["intent"] == "assistant:bookings"


@pytest.mark.parametrize("text, intent", [
    ("why did my booking fail", "assistant:portal:booking_attempts"),
    ("change password", "assistant:portal:profile"),
    ("booking calendar", "assistant:portal:booking_calendar"),
    ("refund status", "assistant:portal:wallet"),
    ("transfer booking to my friend", "assistant:portal:slot_exchanges"),
])
def test_everyday_student_questions_reach_their_page(flags, text, intent):
    message, _ = _ask(_Lab(), text)
    assert message["metadata"]["intent"] == intent, message["content"]


@pytest.mark.parametrize("text, intent, label", [
    ("approve recharge requests", "assistant:portal:wallet_recharge_requests", "Open wallet recharge requests"),
    ("email templates", "assistant:portal:email_templates", "Open communication and email templates"),
    ("wallet ledger", "assistant:portal:wallet_ledger", "Open wallet ledger"),
    ("experience ratings", "assistant:portal:experience_ratings", "Open experience ratings"),
    ("failed booking attempts", "assistant:portal:attempt_log", "Open booking attempt log"),
    ("istem mapping", "assistant:portal:istem_equipment", "Open I-STEM equipment mapping"),
])
def test_staff_page_names_open_the_staff_page_not_their_own_wallet_or_bookings(flags, text, intent, label):
    from iic_booking.users.models.user_type import UserType
    from iic_booking.users.tests.factories import UserFactory

    lab = _Lab()
    admin = UserFactory(user_type=UserType.ADMIN, is_active=True, admin_approved=True)
    message, actions = _ask(lab, text, user=admin)
    assert message["metadata"]["intent"] == intent, message["content"]
    assert label in actions


def test_a_student_asking_to_recharge_still_gets_recharge_help(flags):
    message, _ = _ask(_Lab(), "how to recharge wallet")
    assert message["metadata"]["intent"] == "assistant:recharge"

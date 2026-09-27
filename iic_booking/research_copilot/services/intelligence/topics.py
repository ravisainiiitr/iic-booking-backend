"""
Topic clarification: a bare topic word ("wallet", "credit", "results", "help") is ambiguous, so Copilot
asks what the user wants and offers only the actions that fit this topic and this user.

Menus are data, not branches: each entry names the action, its label, the typed message it stands
for, and the capability that must hold for the user to see it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from iic_booking.research_copilot.services.intelligence import actions as A
from iic_booking.research_copilot.services.intelligence import messages as M
from iic_booking.research_copilot.services.intelligence.capabilities import Capabilities


@dataclass(frozen=True)
class Entry:
    action_type: str
    label: str
    utterance: str
    requires: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    primary: bool = False


@dataclass(frozen=True)
class Topic:
    key: str
    title: str
    prompt: str
    entries: tuple[Entry, ...]
    requires: str | None = None
    unavailable_prompt: str = ""
    unavailable_entries: tuple[Entry, ...] = ()


def _else(topic: str, about: str) -> Entry:
    return Entry(A.SOMETHING_ELSE, "Something else", f"I want something else regarding {about}", payload={"topic": topic})


def _topic(topic: str, label: str) -> Entry:
    return Entry(A.ASK_CLARIFICATION, label, label, payload={"topic": topic})


TOPICS: dict[str, Topic] = {
    "help": Topic(
        "help",
        "Help",
        "Sure. What can I help you with?",
        (
            _topic("equipment", "Equipment"),
            _topic("bookings", "Bookings"),
            _topic("wallet", "Wallet"),
            _topic("results", "Results"),
            Entry(A.ASK_CLARIFICATION, "My Research", "My Research", "my_research_available", {"topic": "my_research"}),
            Entry(A.PORTAL_HELP, "Portal help", "I have a question about the portal", payload={"topic": "portal"}),
            _topic("support", "Support"),
        ),
    ),
    "equipment": Topic(
        "equipment",
        "Equipment",
        "Which kind of equipment are you looking for?",
        (),
    ),
    "wallet": Topic(
        "wallet",
        "Wallet",
        "I can help with your wallet. What would you like to do?",
        (
            Entry(A.VIEW_WALLET_BALANCE, "View balance", "Show my wallet balance", primary=True),
            Entry(A.RECHARGE_WALLET, "Recharge wallet", "Recharge my wallet", "can_recharge"),
            Entry(A.VIEW_WALLET_TRANSACTIONS, "View transactions", "Show my wallet transactions"),
            Entry(A.VIEW_CREDIT_STATUS, "Credit status", "Show my wallet credit status", "credit_visible"),
            Entry(A.PORTAL_HELP, "How wallet works", "How does the wallet work?", payload={"topic": "wallet"}),
            _else("wallet", "my wallet"),
        ),
        requires="has_wallet",
        unavailable_prompt=(
            "You don't have access to a wallet yet. Students book against their faculty's wallet once the faculty "
            "approves their joining request. What would you like to do?"
        ),
        unavailable_entries=(
            Entry(A.PORTAL_HELP, "How wallet works", "How does the wallet work?", payload={"topic": "wallet"}),
            Entry(A.PORTAL_HELP, "Join my faculty's wallet", "How can a student associate with a faculty?",
                  payload={"topic": "faculty_association"}),
            _else("wallet", "my wallet"),
        ),
    ),
    "credit": Topic(
        "credit",
        "Wallet credit",
        "Are you asking about your credit status, requesting credit, or settling an existing credit?",
        (
            Entry(A.VIEW_CREDIT_STATUS, "Credit status", "Show my wallet credit status", primary=True),
            Entry(A.REQUEST_WALLET_CREDIT, "Request credit", "Request wallet credit", "credit_can_request"),
            Entry(A.PORTAL_HELP, "Credit settlement", "How do I settle my wallet credit?", "credit_has_facility",
                  {"topic": "credit_settlement"}),
            _else("credit", "wallet credit"),
        ),
        requires="credit_visible",
        unavailable_prompt="Wallet credit isn't available for your account. What would you like to do?",
        unavailable_entries=(
            Entry(A.PORTAL_HELP, "How wallet credit works", "How does wallet credit work?", payload={"topic": "credit"}),
            _topic("wallet", "Wallet options"),
            _else("credit", "wallet credit"),
        ),
    ),
    "bookings": Topic(
        "bookings",
        "Bookings",
        "What would you like to do with your bookings?",
        (
            Entry(A.VIEW_BOOKINGS, "View my bookings", "Show my bookings", primary=True),
            Entry(A.BOOK_EQUIPMENT, "Book equipment", "I want to book equipment"),
            Entry(A.CANCEL_BOOKING, "Cancel a booking", "Cancel my booking", "has_upcoming_bookings"),
            Entry(A.RESCHEDULE_BOOKING, "Reschedule a booking", "Reschedule my booking", "has_upcoming_bookings"),
            Entry(A.CHECK_AVAILABILITY, "Check availability", "Check equipment availability"),
            _else("bookings", "my bookings"),
        ),
    ),
    "results": Topic(
        "results",
        "Results",
        "What would you like to know about your results?",
        (
            Entry(A.VIEW_RESULTS, "Result status", "Show my results", primary=True),
            Entry(A.SAMPLE_STATUS, "Sample status", "Show my sample status"),
            Entry(A.PORTAL_HELP, "How results are shared", "How do I view and download my results?",
                  payload={"topic": "results"}),
            _else("results", "my results"),
        ),
    ),
    "my_research": Topic(
        "my_research",
        "My Research",
        "What would you like to do in My Research?",
        (
            Entry(A.OPEN_MY_RESEARCH, "My workspaces", "Show my research workspaces", primary=True),
            Entry(A.CREATE_WORKSPACE, "New workspace", "Create a research workspace", "can_create_workspace"),
            Entry(A.VIEW_RESEARCH_GROUP, "Research groups", "Show my research groups", "groups_available"),
            Entry(A.PORTAL_HELP, "How My Research works", "How does My Research work?", payload={"topic": "my_research"}),
            _else("my_research", "My Research"),
        ),
        requires="my_research_available",
        unavailable_prompt="My Research is available only to IIT Roorkee students and faculty. What would you like to do?",
        unavailable_entries=(
            Entry(A.PORTAL_HELP, "How My Research works", "How does My Research work?", payload={"topic": "my_research"}),
            _topic("help", "Other topics"),
        ),
    ),
    "support": Topic(
        "support",
        "Support",
        "How can the IIC support team help?",
        (
            Entry(A.CREATE_SUPPORT_TICKET, "Create ticket", "I want to raise a support ticket", primary=True),
            Entry(A.VIEW_SUPPORT_TICKETS, "View my tickets", "Show my support tickets"),
            _else("support", "support"),
        ),
    ),
    "faculty": Topic(
        "faculty",
        "Faculty",
        "What are you trying to do with faculty information?",
        (
            Entry(A.VIEW_AFFILIATIONS, "My faculty / supervisor", "Who is my faculty?", "is_student", primary=True),
            Entry(A.PORTAL_HELP, "Associate with a faculty", "How can a student associate with a faculty?",
                  "!is_faculty", {"topic": "faculty_association"}),
            Entry(A.PORTAL_HELP, "How students join my wallet", "How can a student associate with a faculty?",
                  "is_faculty", {"topic": "faculty_association"}),
            Entry(A.VIEW_RESEARCH_GROUP, "Research groups", "Show my research groups", "groups_available"),
            _else("faculty", "faculty"),
        ),
    ),
}

_ALIASES: dict[str, str] = {}
for _topic_key, _words in {
    "help": ("help", "help me", "menu", "options", "what can you do", "i need help", "can you help", "can you help me",
             "start", "get started"),
    "equipment": ("equipment", "equipments", "instrument", "instruments", "instrumentation", "machines"),
    "wallet": ("wallet", "my wallet", "wallet help", "wallet options", "payment", "payments"),
    "credit": ("credit", "credits", "wallet credit", "credit facility", "my credit"),
    "bookings": ("booking", "bookings", "booking help", "slot booking"),
    "results": ("result", "results", "report", "reports", "sample", "samples"),
    "my_research": ("my research", "research", "workspace", "workspaces", "research workspace", "research workspaces",
                    "my workspace", "my workspaces", "research group", "research groups", "groups"),
    "support": ("support", "ticket", "tickets", "support ticket", "support tickets", "helpdesk", "help desk", "complaint"),
    "faculty": ("faculty", "supervisor", "guide", "professor", "faculty association"),
}.items():
    for _w in _words:
        _ALIASES[_w] = _topic_key

_TRIM_RE = re.compile(r"^(please\s+|about\s+|regarding\s+|the\s+)+|(\s+please)+$")


def bare_topic(text: str) -> str | None:
    """Topic key when the whole message is just a topic word ("wallet", "Results?", "help please")."""
    lower = re.sub(r"[^a-z ]+", " ", (text or "").lower())
    lower = " ".join(lower.split())
    if not lower or len(lower) > 40:
        return None
    lower = _TRIM_RE.sub("", lower).strip()
    return _ALIASES.get(lower)


def build_actions(topic: Topic, caps: Capabilities) -> tuple[str, list[dict[str, Any]]]:
    available = caps.allows(topic.requires)
    prompt = topic.prompt if available else (topic.unavailable_prompt or topic.prompt)
    entries = topic.entries if available else topic.unavailable_entries
    out = []
    for e in entries:
        if not caps.allows(e.requires):
            continue
        out.append(A.make(e.action_type, e.label, payload=e.payload, utterance=e.utterance,
                          style=A.PRIMARY if e.primary else A.SECONDARY))
    return prompt, out


def menu(turn, key: str) -> dict[str, Any]:
    from iic_booking.research_copilot.services.intelligence import flows
    from iic_booking.research_copilot.services.intelligence import state as st

    topic = TOPICS[key]
    if key == "equipment":
        return flows.technique_prompt(turn, workflow="equipment", question=topic.prompt)
    caps = Capabilities(turn.user)
    prompt, acts = build_actions(topic, caps)
    st.restart(turn.state, "topic", topic=key)
    turn.state.pop("context_technique", None)
    return turn.respond(
        message_type=M.CHOICE_LIST,
        content=prompt,
        actions=acts,
        source_label=M.SOURCE_COPILOT,
        extra={"topic": key, "response_type": "CLARIFICATION"},
    )

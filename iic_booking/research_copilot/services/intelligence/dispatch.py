"""
One handler per structured action.

Handlers reuse the existing workflows (equipment/availability/estimate/booking in `flows`, cancel and
reschedule in `booking_changes`, wallet prepare in `v2.mutations.wallet`) and the portal read rules.
Every id in a payload is re-resolved under the signed-in user; nothing here writes portal records.
Each response ends with next actions that belong to that response only.
"""

from __future__ import annotations

import re
from typing import Any

from django.db.models import Q
from django.utils import timezone

from iic_booking.research_copilot.services.intelligence import actions as A
from iic_booking.research_copilot.services.intelligence import booking_changes as changes
from iic_booking.research_copilot.services.intelligence import entities as entity_svc
from iic_booking.research_copilot.services.intelligence import equipment as eqsvc
from iic_booking.research_copilot.services.intelligence import flows
from iic_booking.research_copilot.services.intelligence import intents as I
from iic_booking.research_copilot.services.intelligence import messages as M
from iic_booking.research_copilot.services.intelligence import state as st
from iic_booking.research_copilot.services.intelligence import terminology
from iic_booking.research_copilot.services.intelligence import topics
from iic_booking.research_copilot.services.intelligence.capabilities import Capabilities
from iic_booking.research_copilot.services.intelligence.flows import Turn

ACTION_INTENTS = {
    A.BOOK_EQUIPMENT: I.BOOKING_REQUEST,
    A.CHECK_AVAILABILITY: I.AVAILABILITY,
    A.ESTIMATE_COST: I.COST_ESTIMATE,
    A.VIEW_EQUIPMENT: I.EQUIPMENT_SEARCH,
    A.SEARCH_EQUIPMENT: I.EQUIPMENT_SEARCH,
    A.LEARN_TECHNIQUE: I.EQUIPMENT_INFORMATION,
    A.FIND_RELATED_TECHNIQUES: I.EQUIPMENT_RECOMMENDATION,
    A.VIEW_BOOKINGS: I.MY_BOOKINGS,
    A.BOOKING_DETAILS: I.MY_BOOKINGS,
    A.CANCEL_BOOKING: I.CANCELLATION_REQUEST,
    A.PARTIAL_CANCEL_BOOKING: I.PARTIAL_CANCELLATION,
    A.RESCHEDULE_BOOKING: I.RESCHEDULING_REQUEST,
    A.VIEW_WALLET: I.WALLET_BALANCE,
    A.VIEW_WALLET_BALANCE: I.WALLET_BALANCE,
    A.VIEW_WALLET_TRANSACTIONS: I.WALLET_TRANSACTIONS,
    A.RECHARGE_WALLET: I.WALLET_RECHARGE,
    A.VIEW_CREDIT_STATUS: I.CREDIT_STATUS,
    A.REQUEST_WALLET_CREDIT: I.CREDIT_STATUS,
    A.VIEW_RESULTS: I.RESULT_STATUS,
    A.VIEW_RESULT: I.RESULT_STATUS,
    A.SAMPLE_STATUS: I.SAMPLE_STATUS,
    A.OPEN_MY_RESEARCH: I.MY_RESEARCH,
    A.CREATE_WORKSPACE: I.MY_RESEARCH,
    A.VIEW_RESEARCH_GROUP: I.RESEARCH_GROUP,
    A.CREATE_SUPPORT_TICKET: I.SUPPORT_REQUEST,
    A.VIEW_SUPPORT_TICKETS: I.SUPPORT_REQUEST,
    A.PORTAL_HELP: I.PORTAL_HELP,
}

HELP_QUERIES = {
    "wallet": "How does the wallet work",
    "credit": "How does wallet credit work",
    "credit_settlement": "How do I settle or repay my wallet credit",
    "results": "How do I view and download my results",
    "my_research": "What is My Research and how do I use it",
    "faculty_association": "How can a student associate with a faculty",
    "bookings": "How do I book equipment",
    "support": "How do I raise a support ticket",
    "equipment": "How do I choose the right equipment",
}

MAX_ROWS = 8
_ACTIVE_STATUSES = ("PENDING", "BOOKED", "DISRUPTION_PENDING")


def _short(key: str) -> str:
    return terminology.TECHNIQUES[key].label.split(" (")[0]


def _clone(turn: Turn, *, text: str) -> Turn:
    return Turn(turn.user, text, turn.conversation, turn.state, entity_svc.extract(text), turn.intent, turn.confidence)


def _with_techniques(turn: Turn, key: str) -> Turn:
    return Turn(turn.user, turn.text, turn.conversation, turn.state, entity_svc.Entities(techniques=[key]),
                turn.intent, turn.confidence)


def _technique(payload: dict[str, Any]) -> str | None:
    key = payload.get("technique")
    if key in terminology.TECHNIQUES:
        return key
    query = payload.get("equipment_query") or ""
    found = terminology.find_techniques(query) if query else []
    return found[0].key if found else None


def _remember_context(state: dict[str, Any], key: str | None) -> None:
    if key:
        state["context_technique"] = key


def _denied_equipment(turn: Turn) -> dict[str, Any]:
    return M.error("That equipment is not available to your account.", intent=turn.intent)


def _respond(turn: Turn, content: str, actions: list[dict[str, Any]] | None = None, *, cards=None,
             message_type: str = M.TEXT, source: str = M.SOURCE_PORTAL, extra=None) -> dict[str, Any]:
    return turn.respond(message_type=message_type, content=content, actions=actions or [], cards=cards,
                        source_label=source, extra=extra)


# ============================================================================ equipment


def _equipment_turn(turn: Turn, payload: dict[str, Any]):
    """(equipment or None, technique key or None, error response or None)."""
    key = _technique(payload)
    eq = None
    if payload.get("equipment_id"):
        eq = eqsvc.get_visible(turn.user, payload["equipment_id"])
        if eq is None:
            return None, key, _denied_equipment(turn)
    return eq, key, None


def book_equipment(turn: Turn, payload: dict[str, Any]) -> dict[str, Any]:
    eq, key, err = _equipment_turn(turn, payload)
    if err:
        return err
    _remember_context(turn.state, key)
    if eq is not None:
        st.restart(turn.state, "booking", context_technique=key)
        flows.remember_equipment(turn.state, eq)
        return flows.continue_booking(turn)
    if key:
        return flows.start_booking(_with_techniques(turn, key))
    if payload.get("equipment_query"):
        return flows.start_booking(_clone(turn, text=f"book {payload['equipment_query']}"))
    return flows.technique_prompt(turn, workflow="booking", question="Which equipment would you like to book?")


def view_equipment(turn: Turn, payload: dict[str, Any]) -> dict[str, Any]:
    eq, key, err = _equipment_turn(turn, payload)
    if err:
        return err
    _remember_context(turn.state, key)
    if eq is not None:
        return flows.equipment_card(turn, eq)
    if key:
        return flows.search_equipment(_with_techniques(turn, key))
    if payload.get("equipment_query"):
        return flows.search_equipment(_clone(turn, text=f"find {payload['equipment_query']}"))
    return flows.technique_prompt(turn, workflow="equipment", question="Which kind of equipment are you looking for?")


def check_availability(turn: Turn, payload: dict[str, Any]) -> dict[str, Any]:
    eq, key, err = _equipment_turn(turn, payload)
    if err:
        return err
    _remember_context(turn.state, key)
    if eq is not None:
        st.restart(turn.state, "availability", context_technique=key)
        return flows.show_slots(turn, eq)
    if key:
        return flows.start_availability(_with_techniques(turn, key))
    return flows.technique_prompt(turn, workflow="availability", question="Which equipment should I check availability for?")


def estimate_cost(turn: Turn, payload: dict[str, Any]) -> dict[str, Any]:
    eq, key, err = _equipment_turn(turn, payload)
    if err:
        return err
    _remember_context(turn.state, key)
    if eq is not None:
        st.restart(turn.state, "estimate", context_technique=key)
        return flows.estimate_for(turn, [eq], None)
    if key:
        return flows.start_estimate(_with_techniques(turn, key))
    return flows.technique_prompt(turn, workflow="estimate", question="Which equipment should I estimate the cost for?")


def learn_technique(turn: Turn, payload: dict[str, Any]) -> dict[str, Any]:
    from iic_booking.research_copilot.services.intelligence import engine, knowledge, knowledge_enabled

    eq, key, err = _equipment_turn(turn, payload)
    if err:
        return err
    if key is None and eq is not None:
        return flows.equipment_card(turn, eq)
    if key is None:
        return flows.technique_prompt(turn, workflow="equipment", question="Which technique would you like to learn about?")
    _remember_context(turn.state, key)
    if knowledge_enabled():
        band, hits = knowledge.best(text=f"What is {_short(key)}", user=turn.user)
        if band == knowledge.HIGH and hits:
            return engine.knowledge_answer(turn, hits[0].article)
    resp = flows.technique_overview(turn, key)
    has_equipment = any(c.get("type") == "equipment_list" for c in resp.get("cards") or [])
    kept = [
        a for a in (resp.get("suggested_actions") or [])
        if a.get("escalate") or str((a.get("choice") or {}).get("value") or "").startswith("more:")
    ]
    resp["suggested_actions"] = technique_actions(
        key, has_equipment=has_equipment, exclude={A.LEARN_TECHNIQUE, A.VIEW_EQUIPMENT}
    ) + kept
    return resp


def related(turn: Turn, payload: dict[str, Any]) -> dict[str, Any]:
    from iic_booking.research_copilot.services.intelligence import engine

    key = _technique(payload)
    if key is None:
        return flows.technique_prompt(turn, workflow="equipment", question="Which technique should I find alternatives for?")
    _remember_context(turn.state, key)
    return engine.related_techniques(turn, key)


def technique_actions(key: str, *, has_equipment: bool = True, exclude: set[str] | None = None) -> list[dict[str, Any]]:
    short = _short(key)
    p = {"technique": key}
    rows = [
        (A.BOOK_EQUIPMENT, "Book", f"Book {short}", True),
        (A.CHECK_AVAILABILITY, "Check availability", f"Check availability for {short}", True),
        (A.VIEW_EQUIPMENT, "View instruments", f"Show {short} instruments", True),
        (A.ESTIMATE_COST, "Estimate cost", f"Estimate the cost for {short}", True),
        (A.LEARN_TECHNIQUE, f"Learn about {short}", f"Tell me about {short}", False),
        (A.FIND_RELATED_TECHNIQUES, "Related techniques", f"Find techniques related to {short}", False),
        (A.SOMETHING_ELSE, "Something else", f"I want something else regarding {short}", False),
    ]
    out = []
    for action_type, label, said, needs_equipment in rows:
        if exclude and action_type in exclude:
            continue
        if needs_equipment and not has_equipment:
            continue
        out.append(A.make(action_type, label, payload=p, utterance=said,
                          style=A.PRIMARY if action_type == A.BOOK_EQUIPMENT else A.SECONDARY))
    return out


# ============================================================================ bookings


def _booking_line(b: dict[str, Any]) -> str:
    return f"- {b['label']} ({str(b.get('status') or '').replace('_', ' ').title()})"


def view_bookings(turn: Turn, payload: dict[str, Any]) -> dict[str, Any]:
    from iic_booking.equipment.models import Booking

    caps = Capabilities(turn.user)
    upcoming = caps.upcoming_bookings
    st.restart(turn.state, "bookings")
    actions: list[dict[str, Any]] = []
    if upcoming:
        lines = ["**Your upcoming bookings**", ""] + [_booking_line(b) for b in upcoming[:MAX_ROWS]]
        for b in upcoming[:3]:
            actions.append(A.make(A.BOOKING_DETAILS, f"Details #{b['booking_id']}", payload={"booking_id": b["booking_id"]},
                                  utterance=f"Show booking #{b['booking_id']}"))
        actions += [
            A.make(A.RESCHEDULE_BOOKING, "Reschedule", utterance="Reschedule my booking"),
            A.make(A.CANCEL_BOOKING, "Cancel", utterance="Cancel my booking"),
        ]
        cards = [{"type": "bookings", "items": upcoming[:MAX_ROWS]}]
    else:
        recent = list(Booking.objects.filter(user=turn.user).select_related("equipment").order_by("-created_at")[:5])
        lines = ["You have no upcoming bookings."]
        if recent:
            lines += ["", "**Recent bookings**", ""]
            lines += [f"- #{b.booking_id} {getattr(b.equipment, 'name', '')} ({str(b.status).replace('_', ' ').title()})"
                      for b in recent]
        cards = None
    actions.append(A.make(A.BOOK_EQUIPMENT, "Book equipment", utterance="I want to book equipment",
                          style=A.SECONDARY if upcoming else A.PRIMARY))
    actions.append(M.link("my_bookings", "Open My Bookings", "/my-bookings"))
    return _respond(turn, "\n".join(lines), actions, cards=cards)


def booking_details(turn: Turn, payload: dict[str, Any]) -> dict[str, Any]:
    from iic_booking.equipment.models import Booking

    bid = payload.get("booking_id")
    b = (
        Booking.objects.select_related("equipment").prefetch_related("daily_slots")
        .filter(user=turn.user, booking_id=bid).first()
        if bid else None
    )
    if b is None:
        return M.error("I couldn't find that booking among yours.", intent=turn.intent,
                       actions=[A.make(A.VIEW_BOOKINGS, "View my bookings", utterance="Show my bookings")])
    info = changes._summary(b)
    lines = [f"**{info['label']}**", "", f"- Status: {str(b.status).replace('_', ' ').title()}", f"- Slots: {info['slot_count']}"]
    if info.get("total_charge"):
        lines.append(f"- Charge: {M.money(info['total_charge'])}")
    samples = (b.input_values or {}).get("A") if isinstance(b.input_values, dict) else None
    if samples:
        lines.append(f"- Samples: {samples}")
    now = timezone.now()
    active = b.status in _ACTIVE_STATUSES and any(s.start_datetime and s.start_datetime > now for s in b.daily_slots.all())
    actions: list[dict[str, Any]] = []
    if active and info["self_service_open"]:
        lines.append(f"- Self-service changes open until {info['cutoff']}" if info.get("cutoff") else "")
        actions += [
            A.make(A.RESCHEDULE_BOOKING, "Reschedule", payload={"booking_id": b.booking_id},
                   utterance=f"Reschedule booking #{b.booking_id}"),
            A.make(A.CANCEL_BOOKING, "Cancel", payload={"booking_id": b.booking_id}, utterance=f"Cancel booking #{b.booking_id}"),
        ]
    elif active:
        lines.append("- The self-service change window has closed; an admin can still help.")
        actions.append(M.ticket_action("user_requested", "Ask the admin (support ticket)"))
    if not active:
        actions.append(A.make(A.VIEW_RESULT, "Results", payload={"booking_id": b.booking_id},
                              utterance=f"Show results for booking #{b.booking_id}"))
    actions.append(M.link("open_booking", "Open in My Bookings", f"/my-bookings?booking={b.booking_id}"))
    return _respond(turn, "\n".join(x for x in lines if x), actions, extra={"booking_id": int(b.booking_id)})


def cancel_booking(turn: Turn, payload: dict[str, Any]) -> dict[str, Any]:
    bid = payload.get("booking_id")
    if not bid:
        return changes.start_cancel(turn)
    b = changes._owned(turn.user, bid)
    if b is None:
        return M.error("That booking isn't one of your active bookings.", intent=turn.intent,
                       actions=[A.make(A.VIEW_BOOKINGS, "View my bookings", utterance="Show my bookings")])
    st.restart(turn.state, "cancel")
    return changes.cancel_selected(turn, b)


def partial_cancel_booking(turn: Turn, payload: dict[str, Any]) -> dict[str, Any]:
    bid = payload.get("booking_id")
    if not bid:
        return changes.start_cancel(_clone(turn, text="cancel part of my booking"))
    b = changes._owned(turn.user, bid)
    if b is None:
        return M.error("That booking isn't one of your active bookings.", intent=turn.intent)
    st.restart(turn.state, "cancel")
    resp = changes.cancel_selected(turn, b)
    offered = {o.get("value") for o in ((turn.state.get("pending_choice") or {}).get("options") or [])}
    for mode in ("selected", "reduce"):
        if mode in offered:
            st.clear_choice(turn.state)
            return changes.cancel_mode(turn, mode)
    return resp


def reschedule_booking(turn: Turn, payload: dict[str, Any]) -> dict[str, Any]:
    bid = payload.get("booking_id")
    if not bid:
        return changes.start_reschedule(turn)
    b = changes._owned(turn.user, bid)
    if b is None:
        return M.error("That booking isn't one of your active bookings.", intent=turn.intent,
                       actions=[A.make(A.VIEW_BOOKINGS, "View my bookings", utterance="Show my bookings")])
    st.restart(turn.state, "reschedule")
    return changes.reschedule_selected(turn, b)


# ============================================================================ wallet


def _wallet_next(caps: Capabilities, *, exclude: set[str]) -> list[dict[str, Any]]:
    rows = [
        (A.VIEW_WALLET_BALANCE, "View balance", "Show my wallet balance", None),
        (A.VIEW_WALLET_TRANSACTIONS, "View transactions", "Show my wallet transactions", None),
        (A.RECHARGE_WALLET, "Recharge wallet", "Recharge my wallet", "can_recharge"),
        (A.VIEW_CREDIT_STATUS, "Credit status", "Show my wallet credit status", "credit_visible"),
    ]
    return [A.make(t, label, utterance=said) for t, label, said, req in rows if t not in exclude and caps.allows(req)]


def wallet_balance(turn: Turn, payload: dict[str, Any]) -> dict[str, Any]:
    from iic_booking.research_copilot.services import tools as tools_svc

    caps = Capabilities(turn.user)
    if not caps.has_wallet:
        return topics.menu(turn, "wallet")
    st.restart(turn.state, "wallet")
    data = (tools_svc._get_wallet(arguments={}, user=turn.user) or {}).get("data") or {}
    bal = data.get("balance")
    if bal is None:
        return _respond(turn, "Your wallet hasn't been set up yet. It is created when you first open the Wallet page.",
                        [M.link("open_wallet", "Open Wallet", "/wallet")])
    lines = [f"Your current wallet balance is **{M.money(bal)}**."]
    subs = data.get("sub_wallets") or []
    if len(subs) > 1:
        lines += ["", "By department:"] + [f"- {s.get('department') or 'Department'}: {M.money(s.get('balance'))}" for s in subs]
    if caps.wallet_is_shared:
        owner = f" ({caps.wallet_owner_name})" if caps.wallet_owner_name else ""
        lines += ["", f"This is your faculty's wallet{owner}; your bookings are charged to it."]
    return _respond(
        turn,
        "\n".join(lines),
        _wallet_next(caps, exclude={A.VIEW_WALLET_BALANCE}),
        cards=[{"type": "wallet", "balance": bal, "currency": "INR", "sub_wallets": subs}],
    )


def wallet_transactions(turn: Turn, payload: dict[str, Any]) -> dict[str, Any]:
    from iic_booking.users.models import SubWalletTransaction

    caps = Capabilities(turn.user)
    if caps.wallet is None:
        return topics.menu(turn, "wallet") if not caps.has_wallet else _respond(
            turn, "Your wallet has no transactions yet.", _wallet_next(caps, exclude={A.VIEW_WALLET_TRANSACTIONS}))
    st.restart(turn.state, "wallet")
    qs = (
        SubWalletTransaction.objects.filter(sub_wallet__wallet=caps.wallet)
        .select_related("sub_wallet__department")
        .order_by("-created_at")
    )
    if caps.wallet_is_shared:
        # Same visibility as the Wallet page: on a shared wallet a student sees only their own debits plus credits.
        qs = qs.filter(Q(related_user_id=turn.user.pk) | Q(transaction_type=SubWalletTransaction.TransactionType.CREDIT))
    rows = list(qs[:MAX_ROWS])
    if not rows:
        lines = ["No wallet transactions yet."]
    else:
        lines = ["**Recent wallet transactions**", ""]
        for t in rows:
            when = timezone.localtime(t.created_at).strftime("%d %b %Y") if t.created_at else ""
            kind = "Credit" if t.transaction_type == SubWalletTransaction.TransactionType.CREDIT else "Debit"
            desc = " ".join(str(t.description or "").split())[:90]
            lines.append(f"- {when}: {kind} {M.money(t.amount)}" + (f" ({desc})" if desc else ""))
    items = [
        {"date": t.created_at.isoformat() if t.created_at else None, "type": t.transaction_type,
         "amount": str(t.amount), "description": str(t.description or "")[:200]}
        for t in rows
    ]
    actions = _wallet_next(caps, exclude={A.VIEW_WALLET_TRANSACTIONS})
    actions.append(M.link("statement", "Full statement", "/wallet"))
    return _respond(turn, "\n".join(lines), actions, cards=[{"type": "transactions", "items": items}])


def recharge_wallet(turn: Turn, payload: dict[str, Any]) -> dict[str, Any]:
    from iic_booking.research_copilot.services.v2.mutations import wallet as wallet_mut
    from iic_booking.research_copilot.services.v2.orchestrator import _prep_to_response, _store_context

    caps = Capabilities(turn.user)
    if not caps.has_wallet:
        return topics.menu(turn, "wallet")
    if not caps.can_recharge:
        owner = caps.wallet_owner_name or "your faculty"
        return _respond(
            turn,
            f"Recharging isn't available from your account. You book against {owner}'s wallet, so the wallet owner "
            "recharges it.",
            [A.make(A.VIEW_WALLET_BALANCE, "View balance", utterance="Show my wallet balance"),
             A.make(A.PORTAL_HELP, "How wallet works", payload={"topic": "wallet"}, utterance="How does the wallet work?")],
        )
    st.restart(turn.state, "wallet")
    prep = wallet_mut.prepare_wallet_recharge(user=turn.user, text=turn.text)
    if prep.get("status") == "RECHARGE_GUIDANCE":
        href = prep.get("portal_href") or "/wallet?recharge=1"
        return _respond(
            turn,
            "Sure. I'll help you recharge your wallet.\n\n" + (prep.get("message") or ""),
            [
                {**M.link("open_recharge", "Open recharge form", href), "primary": True, "style": A.PRIMARY},
                A.make(A.VIEW_WALLET_BALANCE, "View balance", utterance="Show my wallet balance"),
                A.make(A.PORTAL_HELP, "How wallet works", payload={"topic": "wallet"}, utterance="How does the wallet work?"),
            ],
            cards=[{"type": "recharge_guidance", "title": "Recharge your wallet", "wallet_balance": prep.get("wallet_balance"),
                    "sub_wallets": prep.get("sub_wallets") or [], "amount": prep.get("amount"), "portal_href": href}],
        )
    resp = _prep_to_response(prep)
    _store_context(turn.conversation, resp.get("metadata") or {})
    resp["content"] = "Sure. I'll help you recharge your wallet.\n\n" + (resp.get("content") or "")
    return resp


def credit_status(turn: Turn, payload: dict[str, Any]) -> dict[str, Any]:
    caps = Capabilities(turn.user)
    summary = caps.credit_summary
    if not summary.get("feature_enabled"):
        return _respond(turn, "Wallet credit isn't enabled on the portal right now.",
                        _wallet_next(caps, exclude={A.VIEW_CREDIT_STATUS}))
    st.restart(turn.state, "wallet")
    elig = caps.credit_eligibility
    lines = ["**Wallet credit**", ""]
    if caps.credit_has_facility:
        lines.append(f"- Active credit {summary.get('active_facility_reference')}: "
                     f"{M.money(summary.get('existing_outstanding_credit'))} outstanding")
    else:
        lines.append("- No outstanding credit.")
    lines.append("- New request: " + ("you can request credit." if caps.credit_can_request else (elig.get("message") or "not available.")))
    policy = summary.get("policy") or {}
    if policy.get("max_credit_amount"):
        lines.append(f"- Maximum credit: {M.money(policy['max_credit_amount'])}"
                     + (f", repaid within {policy['max_credit_duration_days']} days" if policy.get("max_credit_duration_days") else ""))
    lines += ["", "The Main Administrator approves every credit request."]
    actions: list[dict[str, Any]] = []
    if caps.credit_can_request:
        actions.append(A.make(A.REQUEST_WALLET_CREDIT, "Request credit", utterance="Request wallet credit", style=A.PRIMARY))
    if caps.credit_has_facility:
        actions.append(A.make(A.PORTAL_HELP, "Credit settlement", payload={"topic": "credit_settlement"},
                              utterance="How do I settle my wallet credit?"))
    actions.append(M.link("credit_facility", "Credit Facility page", "/wallet/credit-facility"))
    return _respond(turn, "\n".join(lines), actions,
                    cards=[{"type": "credit_status", "eligibility": elig,
                            "outstanding": summary.get("existing_outstanding_credit"),
                            "active_reference": summary.get("active_facility_reference")}])


def request_credit(turn: Turn, payload: dict[str, Any]) -> dict[str, Any]:
    from iic_booking.research_copilot.services.v2.mutations import wallet as wallet_mut
    from iic_booking.research_copilot.services.v2.orchestrator import _prep_to_response, _store_context

    caps = Capabilities(turn.user)
    if not caps.credit_can_request:
        reason = caps.credit_eligibility.get("message") or "Wallet credit isn't available for your account."
        return _respond(turn, f"You can't request wallet credit right now. {reason}",
                        [A.make(A.VIEW_CREDIT_STATUS, "Credit status", utterance="Show my wallet credit status")]
                        if caps.credit_visible else _wallet_next(caps, exclude={A.VIEW_CREDIT_STATUS}))
    st.restart(turn.state, "wallet")
    prep = wallet_mut.prepare_wallet_credit(user=turn.user, text=turn.text)
    resp = _prep_to_response(prep)
    _store_context(turn.conversation, resp.get("metadata") or {})
    return resp


# ============================================================================ results


def view_results(turn: Turn, payload: dict[str, Any]) -> dict[str, Any]:
    from iic_booking.equipment.booking_results_service import has_material_result_files
    from iic_booking.equipment.models import Booking

    st.restart(turn.state, "results")
    recent = list(Booking.objects.filter(user=turn.user).select_related("equipment").order_by("-created_at")[:6])
    if not recent:
        return _respond(turn, "You have no bookings yet, so there are no results to show.",
                        [A.make(A.BOOK_EQUIPMENT, "Book equipment", utterance="I want to book equipment")])
    lines = ["**Results for your recent bookings**", ""]
    actions: list[dict[str, Any]] = []
    for b in recent:
        try:
            ready = bool(has_material_result_files(b))
        except Exception:  # noqa: BLE001
            ready = False
        status = str(b.status).replace("_", " ").title()
        lines.append(f"- #{b.booking_id} {getattr(b.equipment, 'name', '')} ({status}): "
                     + ("results available" if ready else "no results uploaded yet"))
        if ready and len(actions) < 3:
            actions.append(A.make(A.VIEW_RESULT, f"Results #{b.booking_id}", payload={"booking_id": b.booking_id},
                                  utterance=f"Show results for booking #{b.booking_id}"))
    actions.append(A.make(A.SAMPLE_STATUS, "Sample status", utterance="Show my sample status"))
    actions.append(M.link("my_bookings", "Open My Bookings", "/my-bookings"))
    return _respond(turn, "\n".join(lines), actions)


def view_result(turn: Turn, payload: dict[str, Any]) -> dict[str, Any]:
    from iic_booking.research_copilot.services import tools as tools_svc

    if not payload.get("booking_id"):
        return view_results(turn, payload)
    res = tools_svc._get_booking_results(arguments={"booking_id": payload["booking_id"]}, user=turn.user)
    if not res.get("ok"):
        return M.error("I couldn't find that booking among yours.", intent=turn.intent,
                       actions=[A.make(A.VIEW_RESULTS, "My results", utterance="Show my results")])
    d = res.get("data") or {}
    lines = [f"**Booking #{d.get('booking_id')} {d.get('equipment') or ''}**", ""]
    if d.get("results_available"):
        lines.append("Results are available.")
        names = d.get("file_names") or []
        if names:
            lines += [""] + [f"- {n}" for n in names[:10]]
        lines += ["", "Download them from the Results tab of the booking; Copilot doesn't share file links."]
    else:
        lines.append("No results have been uploaded for this booking yet.")
    bid = d.get("booking_id")
    return _respond(
        turn,
        "\n".join(lines),
        [
            {**M.link("open_results", "Open results", f"/my-bookings?booking={bid}&tab=results"), "primary": True, "style": A.PRIMARY},
            A.make(A.SAMPLE_STATUS, "Sample status", payload={"booking_id": bid}, utterance=f"Sample status for booking #{bid}"),
        ],
        extra={"booking_id": bid},
    )


def sample_status(turn: Turn, payload: dict[str, Any]) -> dict[str, Any]:
    from iic_booking.research_copilot.services import tools as tools_svc

    res = tools_svc._get_sample_status(arguments={"booking_id": payload.get("booking_id")}, user=turn.user)
    if not res.get("ok"):
        return M.error("I couldn't find a booking of yours to check.", intent=turn.intent,
                       actions=[A.make(A.VIEW_BOOKINGS, "View my bookings", utterance="Show my bookings")])
    d = res.get("data") or {}
    bid = d.get("booking_id")
    latest = str(d.get("latest_sample_status") or "").replace("_", " ").title() or "No sample updates yet"
    lines = [f"**Booking #{bid} {d.get('equipment') or ''}**", "", f"- Sample status: {latest}",
             f"- Booking status: {str(d.get('booking_status') or '').replace('_', ' ').title()}"]
    for e in (d.get("events") or [])[:3]:
        when = str(e.get("created_at") or "")[:10]
        lines.append(f"  - {when}: {str(e.get('status') or '').replace('_', ' ').title()}"
                     + (f" ({e['reason']})" if e.get("reason") else ""))
    return _respond(
        turn,
        "\n".join(lines),
        [A.make(A.VIEW_RESULT, "Results", payload={"booking_id": bid}, utterance=f"Show results for booking #{bid}"),
         M.link("open_booking", "Open booking", f"/my-bookings?booking={bid}")],
        extra={"booking_id": bid},
    )


# ============================================================================ My Research


def open_my_research(turn: Turn, payload: dict[str, Any]) -> dict[str, Any]:
    from iic_booking.my_research.models import MemberRole, ResearchWorkspace, ResearchWorkspaceMember, WorkspaceStatus

    caps = Capabilities(turn.user)
    if not caps.my_research_available:
        return topics.menu(turn, "my_research")
    owned = list(ResearchWorkspace.objects.filter(owner=turn.user, status=WorkspaceStatus.ACTIVE).order_by("name")
                 .values_list("name", flat=True)[:6])
    shared = ResearchWorkspaceMember.objects.filter(
        user=turn.user, role=MemberRole.VIEWER, revoked_at__isnull=True, workspace__status=WorkspaceStatus.ACTIVE
    ).count()
    lines = ["**My Research**", ""]
    lines.append(f"- Your workspaces: {len(owned)}" + (f" ({', '.join(owned)})" if owned else ""))
    lines.append(f"- Shared with you: {shared}")
    actions = [{**M.link("open_my_research", "Open My Research", "/my-research"), "primary": True, "style": A.PRIMARY}]
    if caps.can_create_workspace:
        actions.append(A.make(A.CREATE_WORKSPACE, "New workspace", utterance="Create a research workspace"))
    if caps.groups_available:
        actions.append(A.make(A.VIEW_RESEARCH_GROUP, "Research groups", utterance="Show my research groups"))
    return _respond(turn, "\n".join(lines), actions)


def create_workspace(turn: Turn, payload: dict[str, Any]) -> dict[str, Any]:
    caps = Capabilities(turn.user)
    if not caps.can_create_workspace:
        return _respond(turn, "Creating workspaces isn't available for your account.",
                        [A.make(A.PORTAL_HELP, "How My Research works", payload={"topic": "my_research"},
                                utterance="How does My Research work?")])
    return _respond(
        turn,
        "Workspaces are created on the My Research page: press **New Workspace**, give it a name, and choose who can see it.",
        [{**M.link("open_my_research", "Open My Research", "/my-research"), "primary": True, "style": A.PRIMARY}],
    )


def research_groups(turn: Turn, payload: dict[str, Any]) -> dict[str, Any]:
    from iic_booking.my_research.group_models import GroupMemberStatus, GroupStatus, ResearchGroupMember

    caps = Capabilities(turn.user)
    if not caps.groups_available:
        return _respond(turn, "Research groups aren't available for your account.",
                        [A.make(A.PORTAL_HELP, "How My Research works", payload={"topic": "my_research"},
                                utterance="How does My Research work?")])
    rows = list(
        ResearchGroupMember.objects.select_related("group")
        .filter(user=turn.user, status=GroupMemberStatus.ACTIVE, group__status=GroupStatus.ACTIVE)
        .order_by("group__name")[:MAX_ROWS]
    )
    if not rows:
        lines = ["You aren't a member of any research group yet."]
    else:
        lines = ["**Your research groups**", ""] + [
            f"- {m.group.name} ({m.get_role_display()})" for m in rows
        ]
    actions = [M.link(f"group_{m.group_id}", m.group.name[:40], f"/my-research/groups/{m.group_id}") for m in rows[:3]]
    actions.append(M.link("open_my_research", "Open My Research", "/my-research"))
    return _respond(turn, "\n".join(lines), actions)


# ============================================================================ faculty / support / general


def affiliations(turn: Turn, payload: dict[str, Any]) -> dict[str, Any]:
    from iic_booking.research_copilot.services.v2 import read_tools

    base = read_tools.affiliations(user=turn.user)
    return _respond(
        turn,
        base.get("content") or "",
        [A.make(A.PORTAL_HELP, "Associate with a faculty", payload={"topic": "faculty_association"},
                utterance="How can a student associate with a faculty?"),
         M.link("profile", "Open Profile", "/profile")],
    )


def create_ticket(turn: Turn, payload: dict[str, Any]) -> dict[str, Any]:
    from iic_booking.research_copilot.services.intelligence import engine

    return engine.support_offer(turn)


def support_tickets(turn: Turn, payload: dict[str, Any]) -> dict[str, Any]:
    from iic_booking.support.models import Ticket

    rows = list(Ticket.objects.filter(user=turn.user).order_by("-created_at")[:5])
    st.restart(turn.state, "support")
    if not rows:
        lines = ["You have no support tickets."]
    else:
        lines = ["**Your recent support tickets**", ""] + [
            f"- #{t.ticket_id} {t.subject[:80]} ({t.get_status_display()})" for t in rows
        ]
    actions = [M.link(f"ticket_{t.ticket_id}", f"Ticket #{t.ticket_id}", f"/tickets?ticket={t.ticket_id}") for t in rows[:3]]
    actions.append(A.make(A.CREATE_SUPPORT_TICKET, "Create ticket", utterance="I want to raise a support ticket"))
    return _respond(turn, "\n".join(lines), actions, source=M.SOURCE_SUPPORT)


def ask_clarification(turn: Turn, payload: dict[str, Any]) -> dict[str, Any]:
    topic = payload.get("topic") or "help"
    if topic in topics.TOPICS:
        return topics.menu(turn, topic)
    return portal_help(turn, payload)


def faculty_association(turn: Turn) -> dict[str, Any]:
    """The portal's own process: a student sends a wallet joining request from the Wallet page and the faculty approves it."""
    from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus

    caps = Capabilities(turn.user)
    st.restart(turn.state, "help")
    if caps.is_faculty:
        pending = WalletJoinRequest.objects.filter(faculty=turn.user, status=WalletJoinRequestStatus.PENDING).count()
        lines = [
            "**How students associate with you**",
            "",
            "1. The student opens **Wallet** and searches for you by name or email.",
            "2. They send a joining request for your wallet.",
            "3. You approve or reject it under **Join requests** on your Wallet page.",
            "4. Once approved, the student's bookings are charged to your wallet.",
            "",
            f"You have **{pending}** pending joining request{'s' if pending != 1 else ''}.",
        ]
        return _respond(turn, "\n".join(lines), [{**M.link("open_wallet", "Review join requests", "/wallet"),
                                                   "primary": True, "style": A.PRIMARY}])
    latest = WalletJoinRequest.objects.filter(student=turn.user).select_related("faculty").order_by("-created_at").first()
    lines = [
        "**How to associate with a faculty**",
        "",
        "1. Open **Wallet**.",
        "2. Search for your faculty by name or email and send a joining request.",
        "3. Your faculty approves it from their Wallet page.",
        "4. After approval you book equipment against your faculty's wallet.",
    ]
    if latest is not None:
        fac = getattr(latest.faculty, "name", "") or getattr(latest.faculty, "email", "") or "your faculty"
        lines += ["", f"Your latest request to {fac} is **{latest.get_status_display()}**."]
    if caps.wallet_is_shared:
        owner = f" ({caps.wallet_owner_name})" if caps.wallet_owner_name else ""
        lines += ["", f"You are already linked to a faculty wallet{owner}."]
    actions = [{**M.link("open_wallet", "Open Wallet", "/wallet"), "primary": True, "style": A.PRIMARY}]
    if caps.is_student:
        actions.append(A.make(A.VIEW_AFFILIATIONS, "My faculty", utterance="Who is my faculty?"))
    return _respond(turn, "\n".join(lines), actions)


def portal_help(turn: Turn, payload: dict[str, Any]) -> dict[str, Any]:
    from iic_booking.research_copilot.services.intelligence import engine, knowledge, knowledge_enabled
    from iic_booking.research_copilot.services.v2 import read_tools

    topic = payload.get("topic") or "portal"
    query = HELP_QUERIES.get(topic)
    if not query:
        st.restart(turn.state, "help")
        return _respond(turn, "Sure. Ask me your question about the IIC portal.", source=M.SOURCE_COPILOT)
    t = Turn(turn.user, query, turn.conversation, turn.state, entity_svc.extract(query), I.PORTAL_HELP, turn.confidence)
    if topic == "faculty_association":
        if knowledge_enabled():
            band, hits = knowledge.best(text=query, user=turn.user)
            if band == knowledge.HIGH and hits:
                return engine.knowledge_answer(t, hits[0].article)
        return faculty_association(t)
    resp = engine.help_answer(t)
    if resp is None:
        resp = read_tools.docs_rag(user=turn.user, text=query)
    return resp


def something_else(turn: Turn, payload: dict[str, Any]) -> dict[str, Any]:
    key = _technique(payload)
    topic = payload.get("topic")
    about = _short(key) if key else (topics.TOPICS[topic].title.lower() if topic in topics.TOPICS else "")
    st.restart(turn.state, "open_question", context_technique=key)
    text = f"Sure. What would you like to know or do with {about}?" if about else "Sure. What would you like to know or do?"
    return _respond(turn, text, source=M.SOURCE_COPILOT)


def start_over(turn: Turn, payload: dict[str, Any]) -> dict[str, Any]:
    from iic_booking.research_copilot.services.intelligence import engine

    return engine.opening(turn)


_HANDLERS = {
    A.BOOK_EQUIPMENT: book_equipment,
    A.VIEW_EQUIPMENT: view_equipment,
    A.SEARCH_EQUIPMENT: view_equipment,
    A.CHECK_AVAILABILITY: check_availability,
    A.ESTIMATE_COST: estimate_cost,
    A.LEARN_TECHNIQUE: learn_technique,
    A.FIND_RELATED_TECHNIQUES: related,
    A.VIEW_BOOKINGS: view_bookings,
    A.BOOKING_DETAILS: booking_details,
    A.CANCEL_BOOKING: cancel_booking,
    A.PARTIAL_CANCEL_BOOKING: partial_cancel_booking,
    A.RESCHEDULE_BOOKING: reschedule_booking,
    A.VIEW_WALLET: lambda turn, payload: topics.menu(turn, "wallet"),
    A.VIEW_WALLET_BALANCE: wallet_balance,
    A.VIEW_WALLET_TRANSACTIONS: wallet_transactions,
    A.RECHARGE_WALLET: recharge_wallet,
    A.VIEW_CREDIT_STATUS: credit_status,
    A.REQUEST_WALLET_CREDIT: request_credit,
    A.VIEW_RESULTS: view_results,
    A.VIEW_RESULT: view_result,
    A.SAMPLE_STATUS: sample_status,
    A.OPEN_MY_RESEARCH: open_my_research,
    A.CREATE_WORKSPACE: create_workspace,
    A.VIEW_RESEARCH_GROUP: research_groups,
    A.VIEW_AFFILIATIONS: affiliations,
    A.CREATE_SUPPORT_TICKET: create_ticket,
    A.VIEW_SUPPORT_TICKETS: support_tickets,
    A.ASK_CLARIFICATION: ask_clarification,
    A.PORTAL_HELP: portal_help,
    A.SOMETHING_ELSE: something_else,
    A.START_OVER: start_over,
}
assert set(_HANDLERS) == A.ACTION_TYPES


_EQUIPMENT_ACTIONS = {
    A.BOOK_EQUIPMENT, A.VIEW_EQUIPMENT, A.SEARCH_EQUIPMENT, A.CHECK_AVAILABILITY, A.ESTIMATE_COST,
    A.LEARN_TECHNIQUE, A.FIND_RELATED_TECHNIQUES, A.SOMETHING_ELSE,
}


def dispatch(turn: Turn, action_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    turn.intent = ACTION_INTENTS.get(action_type, turn.intent or action_type)
    st.clear_choice(turn.state)
    if action_type not in _EQUIPMENT_ACTIONS:
        turn.state.pop("context_technique", None)
    resp = _HANDLERS[action_type](turn, payload or {})
    meta = dict(resp.get("metadata") or {})
    meta["action_type"] = action_type
    meta.setdefault("intelligence", True)
    resp["metadata"] = meta
    return resp


# ============================================================================ typed text -> action

_HOWTO_RE = re.compile(r"\bhow\s+(does|do|can|to|should|is)\b|\bexplain\b|\bpolic(y|ies)\b|\bworks?\b|\bprocess\b|\brules?\b")
_REQUEST_CREDIT_RE = re.compile(r"\b(request|need|apply|want|get|take)\b")
_MY_TICKETS_RE = re.compile(r"\b(my|show|list|view|see|check)\b.*\btickets?\b")
_NEW_TICKET_RE = re.compile(r"\b(raise|create|new|file|submit|open a|log a|lodge)\b")
_MY_FACULTY_RE = re.compile(r"\b(who\s+is\s+)?my\s+(faculty|supervisor|guide|professor)\b")
_ASSOCIATE_RE = re.compile(
    r"\b(add|associate|join|link|connect|find|select|choose|change)\b.{0,30}\b(faculty|supervisor|guide|professor)\b"
    r"|\bfaculty\s+association\b|\bwhat\s+faculty\b|\bwhich\s+faculty\b"
)


def action_for_text(turn: Turn) -> tuple[str, dict[str, Any]] | None:
    """Map a classified typed message to a structured action when one handler owns that intent."""
    text = terminology.normalize(turn.text)
    intent = turn.intent
    ref = turn.ents.booking_ref
    if _ASSOCIATE_RE.search(text):
        return A.PORTAL_HELP, {"topic": "faculty_association"}
    if _MY_FACULTY_RE.search(text) and not _HOWTO_RE.search(text):
        return A.VIEW_AFFILIATIONS, {}
    if _MY_TICKETS_RE.search(text) and not _NEW_TICKET_RE.search(text):
        return A.VIEW_SUPPORT_TICKETS, {}
    howto = bool(_HOWTO_RE.search(text))
    if intent in {I.WALLET_BALANCE, I.WALLET_TRANSACTIONS, I.WALLET_RECHARGE, I.CREDIT_STATUS} and howto:
        topic = "credit" if intent == I.CREDIT_STATUS or "credit" in text else "wallet"
        return A.PORTAL_HELP, {"topic": topic}
    if intent == I.WALLET_BALANCE:
        return A.VIEW_WALLET_BALANCE, {}
    if intent == I.WALLET_TRANSACTIONS:
        return A.VIEW_WALLET_TRANSACTIONS, {}
    if intent == I.WALLET_RECHARGE:
        return A.RECHARGE_WALLET, {}
    if intent == I.CREDIT_STATUS:
        return (A.REQUEST_WALLET_CREDIT if _REQUEST_CREDIT_RE.search(text) else A.VIEW_CREDIT_STATUS), {}
    if intent == I.MY_BOOKINGS:
        return (A.BOOKING_DETAILS, {"booking_id": ref}) if ref else (A.VIEW_BOOKINGS, {})
    if intent == I.RESULT_STATUS and not howto:
        return (A.VIEW_RESULT, {"booking_id": ref}) if ref else (A.VIEW_RESULTS, {})
    if intent == I.SAMPLE_STATUS and not howto:
        return A.SAMPLE_STATUS, ({"booking_id": ref} if ref else {})
    if intent == I.MY_RESEARCH and not howto:
        return A.OPEN_MY_RESEARCH, {}
    if intent == I.RESEARCH_GROUP and not howto and re.search(r"\b(my|show|list|view|see)\b", text):
        return A.VIEW_RESEARCH_GROUP, {}
    return None

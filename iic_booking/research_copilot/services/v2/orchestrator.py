"""Phase A/B deterministic-first orchestrator."""

from __future__ import annotations

from typing import Any

from iic_booking.research_copilot.services.v2 import flag, v2_enabled
from iic_booking.research_copilot.services.v2.intent_resolver import resolve_intent
from iic_booking.research_copilot.services.v2 import read_tools
from iic_booking.research_copilot.services.v2.response_builder import build_response


def _ctx(conversation) -> dict[str, Any]:
    if conversation is None:
        return {}
    from django.core.cache import cache

    meta = cache.get(f"copilot_ctx:{conversation.id}") or {}
    return meta if isinstance(meta, dict) else {}


def _store_context(conversation, metadata: dict[str, Any]) -> None:
    if conversation is None:
        return
    from django.core.cache import cache

    meta = _ctx(conversation)
    for k in (
        "equipment_id",
        "equipment_name",
        "slot_id",
        "earliest_slot_id",
        "proposal_id",
        "confirmation_token",
        "pending_action",
        "booking_id",
    ):
        if metadata.get(k) is not None:
            if k == "equipment_id":
                meta["last_equipment_id"] = metadata[k]
            meta[k] = metadata[k]
    if metadata.get("equipment_id"):
        meta["last_equipment_id"] = metadata["equipment_id"]
    cache.set(f"copilot_ctx:{conversation.id}", meta, 3600 * 6)


def _context_equipment_id(conversation) -> int | None:
    meta = _ctx(conversation)
    eid = meta.get("last_equipment_id") or meta.get("equipment_id")
    try:
        return int(eid) if eid is not None else None
    except (TypeError, ValueError):
        return None


def _proposal_card(prep: dict[str, Any]) -> dict[str, Any]:
    action = prep.get("action") or "CREATE_BOOKING"
    card_type = {
        "CREATE_BOOKING": "booking_proposal",
        "CANCEL_BOOKING": "cancellation_proposal",
        "RESCHEDULE_BOOKING": "reschedule_proposal",
        "WALLET_RECHARGE": "recharge_proposal",
        "WALLET_CREDIT": "credit_proposal",
    }.get(action, "booking_proposal")
    return {
        "type": card_type,
        "title": {
            "CREATE_BOOKING": "Booking confirmation",
            "CANCEL_BOOKING": "Cancel booking?",
            "RESCHEDULE_BOOKING": "Reschedule confirmation",
            "WALLET_RECHARGE": "Wallet recharge",
            "WALLET_CREDIT": "Wallet credit request",
        }.get(action, "Confirmation"),
        "action": action,
        "proposal_id": prep.get("proposal_id"),
        "confirmation_token": prep.get("confirmation_token"),
        "executable": bool(prep.get("executable")),
        "expires_at": prep.get("expires_at"),
        "equipment_name": prep.get("equipment_name"),
        "booking_id": prep.get("booking_id"),
        "date": prep.get("date"),
        "start_time": prep.get("start_time"),
        "end_time": prep.get("end_time"),
        "duration_minutes": prep.get("duration_minutes"),
        "sample_count": prep.get("sample_count"),
        "estimated_amount": prep.get("estimated_amount"),
        "wallet_balance": prep.get("wallet_balance"),
        "approx_balance_after": prep.get("approx_balance_after"),
        "expected_balance_after": prep.get("expected_balance_after"),
        "amount": prep.get("amount") or prep.get("requested_amount"),
        "requested_amount": prep.get("requested_amount"),
        "outstanding_credit": prep.get("outstanding_credit"),
        "purpose": prep.get("purpose"),
        "cancellation_policy_note": prep.get("cancellation_policy_note"),
        "portal_href": prep.get("portal_href"),
    }


def _prep_to_response(prep: dict[str, Any]) -> dict[str, Any]:
    if not prep.get("ok"):
        return build_response(
            kind="ERROR",
            content=prep.get("message") or "Unable to prepare that action.",
            actions=[{"id": "my_bookings", "label": "My bookings", "href": "/my-bookings", "enabled": True}],
            metadata={"deterministic": True, "mutation_prepare": True, "error": prep.get("error")},
        )

    if prep.get("status") == "CLARIFICATION":
        cands = prep.get("candidates") or []
        return build_response(
            kind="CLARIFICATION",
            content=prep.get("message") or "Which equipment?",
            cards=[{"type": "equipment_choice", "title": "Select equipment", "items": cands}],
            actions=[
                {
                    "id": f"eq_{c.get('id')}",
                    "label": c.get("name") or str(c.get("id")),
                    "prompt": f"Book {c.get('name')}",
                    "enabled": True,
                }
                for c in cands[:6]
            ],
            metadata={"deterministic": True},
        )

    if prep.get("status") == "NEEDS_SLOT":
        actions = []
        if prep.get("equipment_id"):
            actions.append(
                {
                    "id": "find_slots",
                    "label": "Find available slots",
                    "prompt": f"Search available slots for {prep.get('equipment_name') or 'equipment'} this week",
                    "enabled": True,
                }
            )
        if prep.get("portal_href"):
            actions.append({"id": "portal", "label": "Open portal", "href": prep["portal_href"], "enabled": True})
        return build_response(
            kind="ACTION_REQUIRED",
            content=prep.get("message") or "Choose a slot first.",
            actions=actions,
            metadata={"deterministic": True, "equipment_id": prep.get("equipment_id")},
        )

    if prep.get("status") == "NEEDS_AMOUNT":
        return build_response(
            kind="CLARIFICATION",
            content=prep.get("message") or "Specify an amount.",
            actions=list(prep.get("suggested_actions") or [])
            or [{"id": "portal", "label": "Open Wallet", "href": prep.get("portal_href") or "/wallet", "enabled": True}],
            metadata={"deterministic": True, "pending_action": prep.get("action")},
        )

    card = _proposal_card(prep)
    confirm_label = {
        "CREATE_BOOKING": "Confirm Booking",
        "CANCEL_BOOKING": "Confirm Cancellation",
        "RESCHEDULE_BOOKING": "Confirm Reschedule",
        "WALLET_RECHARGE": "Proceed to Payment",
        "WALLET_CREDIT": "Confirm Credit Request",
    }.get(prep.get("action") or "", "Confirm")
    change_prompt = {
        "WALLET_RECHARGE": "Recharge ₹5000",
        "WALLET_CREDIT": "Request ₹10000 wallet credit",
    }.get(prep.get("action") or "", "Search available slots this week")
    actions = [
        {
            "id": "confirm_proposal",
            "label": confirm_label,
            "prompt": "Confirm",
            "enabled": True,
            "requires_confirmation": True,
            "hint": "Confirms the prepared proposal (execute only if mutation flag is ON).",
            "proposal_id": prep.get("proposal_id"),
            "confirmation_token": prep.get("confirmation_token"),
            "mutation_action": prep.get("action"),
        },
        {
            "id": "change",
            "label": "Change",
            "prompt": change_prompt,
            "enabled": True,
        },
    ]
    if prep.get("portal_href"):
        actions.append(
            {
                "id": "portal_fallback",
                "label": "Open portal instead",
                "href": prep["portal_href"],
                "enabled": True,
                "requires_confirmation": True,
            }
        )

    lines = [f"**{card['title']}**", ""]
    if prep.get("equipment_name"):
        lines.append(f"- Equipment: **{prep['equipment_name']}**")
    if prep.get("booking_id"):
        lines.append(f"- Booking: **{prep['booking_id']}**")
    if prep.get("date"):
        lines.append(f"- Date: {prep['date']}")
    if prep.get("start_time"):
        lines.append(f"- Start: {prep['start_time']}")
    if prep.get("end_time"):
        lines.append(f"- End: {prep['end_time']}")
    if prep.get("duration_minutes"):
        lines.append(f"- Duration: {prep['duration_minutes']} minutes")
    if prep.get("sample_count"):
        lines.append(f"- Samples: {prep['sample_count']}")
    if prep.get("estimated_amount") is not None:
        lines.append(f"- Estimated charge: ₹{prep['estimated_amount']}")
    if prep.get("amount") is not None:
        lines.append(f"- Amount: ₹{prep['amount']}")
    if prep.get("requested_amount") is not None:
        lines.append(f"- Requested credit: ₹{prep['requested_amount']}")
    if prep.get("purpose"):
        lines.append(f"- Purpose: {prep['purpose']}")
    if prep.get("wallet_balance") is not None:
        lines.append(f"- Wallet balance: ₹{prep['wallet_balance']}")
    if prep.get("approx_balance_after") is not None:
        lines.append(f"- After booking (approx): ₹{prep['approx_balance_after']}")
    if prep.get("expected_balance_after") is not None:
        lines.append(f"- Expected after recharge (approx): ₹{prep['expected_balance_after']}")
    if prep.get("outstanding_credit") is not None:
        lines.append(f"- Outstanding credit: ₹{prep['outstanding_credit']}")
    if prep.get("cancellation_policy_note"):
        lines.append(f"- Policy: {prep['cancellation_policy_note']}")
    lines.append("")
    lines.append(prep.get("message") or "Confirm to proceed.")
    if not prep.get("executable"):
        lines.append("")
        lines.append("_Mutation execute flag is OFF — Confirm will not move money until financial enablement._")

    return build_response(
        kind="ACTION_PREPARATION",
        content="\n".join(lines),
        cards=[card],
        actions=actions,
        metadata={
            "deterministic": True,
            "mutation_prepare": True,
            "proposal_id": prep.get("proposal_id"),
            "confirmation_token": prep.get("confirmation_token"),
            "pending_action": prep.get("action"),
            "equipment_id": prep.get("equipment_id"),
            "booking_id": prep.get("booking_id"),
            "executable": bool(prep.get("executable")),
        },
    )


def _exec_to_response(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("ok"):
        data = result.get("data") or {}
        bid = result.get("booking_id") or data.get("real_booking_id") or data.get("booking_id")
        actions = []
        if bid:
            actions.append({"id": "view_booking", "label": "View Booking", "href": f"/my-bookings?booking={bid}", "enabled": True})
            actions.append(
                {
                    "id": "analysis",
                    "label": "Open Analysis Workspace",
                    "href": f"/analysis-workspace/{bid}",
                    "enabled": True,
                }
            )
        return build_response(
            kind="LIVE_DATA",
            content=result.get("message") or "Done.",
            cards=[{"type": "booking_success", "booking_id": bid, "action": result.get("action"), "replay": result.get("idempotent_replay")}],
            actions=actions,
            metadata={"deterministic": True, "mutation_execute": True, "ok": True, "booking_id": bid},
        )
    return build_response(
        kind="ERROR",
        content=result.get("message") or "Action failed.",
        cards=[{"type": "booking_error", "error": result.get("error"), "action": result.get("action")}],
        actions=[
            {"id": "my_bookings", "label": "My bookings", "href": "/my-bookings", "enabled": True},
            {"id": "find_slots", "label": "Find slots", "prompt": "Search available slots this week", "enabled": True},
        ],
        metadata={"deterministic": True, "mutation_execute": True, "ok": False, "error": result.get("error")},
    )


def try_deterministic_turn(*, user, text: str, conversation=None, public: bool = False) -> dict[str, Any] | None:
    """
    If this turn can be answered without the LLM, return a response envelope.
    Otherwise return None so the caller continues with RAG/LLM.
    """
    if not v2_enabled() or not flag("COPILOT_DETERMINISTIC_READS", True):
        return None

    intent = resolve_intent(text)
    if not intent.deterministic:
        return None

    if intent.needs_auth and (user is None or not getattr(user, "is_authenticated", False)):
        return build_response(
            kind="ACTION_REQUIRED",
            content="Sign in to use personal Copilot tools (bookings, wallet, results).",
            actions=[{"id": "sign_in", "label": "Sign in", "href": "/auth", "enabled": True}],
            metadata={"intent": intent.intent, "deterministic": True},
        )

    ctx = _ctx(conversation)
    ctx_eq = _context_equipment_id(conversation)
    result: dict[str, Any] | None = None

    if intent.intent == "search_slots":
        result = read_tools.search_available_slots(user=user, text=text, context_equipment_id=ctx_eq)
        # Remember earliest slot for “book it”
        try:
            cards = (result or {}).get("cards") or []
            for card in cards:
                if card.get("type") == "slots" and card.get("items"):
                    first = card["items"][0]
                    meta = dict((result or {}).get("metadata") or {})
                    if first.get("slot_id"):
                        meta["earliest_slot_id"] = first["slot_id"]
                        meta["slot_id"] = first["slot_id"]
                    result["metadata"] = meta
                    break
        except Exception:  # noqa: BLE001
            pass
    elif intent.intent == "search_equipment":
        result = read_tools.search_equipment_catalog(user=user, text=text)
    elif intent.intent == "estimate_cost":
        result = read_tools.estimate_cost(user=user, text=text, context_equipment_id=ctx_eq)
    elif intent.intent == "my_bookings":
        result = read_tools.my_bookings(user=user)
    elif intent.intent == "next_booking":
        result = read_tools.next_booking(user=user)
    elif intent.intent == "wallet_balance":
        result = read_tools.wallet_balance(user=user)
    elif intent.intent == "wallet_transactions":
        result = read_tools.wallet_transactions(user=user)
    elif intent.intent == "wallet_spend_month":
        result = read_tools.wallet_spend_month(user=user)
    elif intent.intent == "credit_status":
        result = read_tools.credit_status(user=user)
    elif intent.intent == "prepare_recharge":
        from iic_booking.research_copilot.services.v2.mutations import wallet as wallet_mut

        prep = wallet_mut.prepare_wallet_recharge(user=user, text=text)
        result = _prep_to_response(prep)
    elif intent.intent == "prepare_credit":
        from iic_booking.research_copilot.services.v2.mutations import wallet as wallet_mut

        prep = wallet_mut.prepare_wallet_credit(user=user, text=text)
        result = _prep_to_response(prep)
    elif intent.intent == "sample_status":
        result = read_tools.sample_or_results(user=user, text=text, which="sample")
    elif intent.intent == "results":
        result = read_tools.sample_or_results(user=user, text=text, which="results")
    elif intent.intent == "ra_status":
        result = read_tools.ra_status(user=user)
    elif intent.intent == "affiliations":
        result = read_tools.affiliations(user=user)
    elif intent.intent == "pending_actions":
        result = read_tools.pending_actions(user=user)
    elif intent.intent == "docs_rag":
        result = read_tools.docs_rag(user=user, text=text)
    elif intent.intent == "prepare_booking":
        from iic_booking.research_copilot.services.v2.mutations import booking as booking_mut

        prep = booking_mut.prepare_booking_create(user=user, text=text, context=ctx)
        result = _prep_to_response(prep)
    elif intent.intent == "prepare_cancel":
        from iic_booking.research_copilot.services.v2.mutations import booking as booking_mut

        prep = booking_mut.prepare_cancellation(user=user, text=text)
        result = _prep_to_response(prep)
    elif intent.intent == "prepare_reschedule":
        from iic_booking.research_copilot.services.v2.mutations import booking as booking_mut

        prep = booking_mut.prepare_reschedule(user=user, text=text)
        result = _prep_to_response(prep)
    elif intent.intent == "confirm_proposal":
        from iic_booking.research_copilot.services.v2.mutations import booking as booking_mut
        from iic_booking.research_copilot.services.v2.mutations import proposals as prop_store
        from iic_booking.research_copilot.services.v2.mutations import wallet as wallet_mut

        pending = ctx.get("pending_action")
        proposal_id = ctx.get("proposal_id")
        token = ctx.get("confirmation_token")
        if not proposal_id or not token:
            for action in (
                "CREATE_BOOKING",
                "CANCEL_BOOKING",
                "RESCHEDULE_BOOKING",
                "WALLET_RECHARGE",
                "WALLET_CREDIT",
            ):
                latest = prop_store.get_latest_proposal(user=user, action=action)
                if latest:
                    proposal_id = latest["proposal_id"]
                    token = latest["confirmation_token"]
                    pending = action
                    break
        if not proposal_id or not token:
            result = build_response(
                kind="CLARIFICATION",
                content="There is no pending action to confirm. Prepare a booking or financial proposal first.",
                metadata={"deterministic": True},
            )
        else:
            if pending == "CANCEL_BOOKING":
                exec_result = booking_mut.execute_booking_cancel(
                    user=user, proposal_id=proposal_id, confirmation_token=token
                )
            elif pending == "RESCHEDULE_BOOKING":
                exec_result = booking_mut.execute_booking_reschedule(
                    user=user, proposal_id=proposal_id, confirmation_token=token
                )
            elif pending == "WALLET_RECHARGE":
                exec_result = wallet_mut.execute_wallet_recharge(
                    user=user, proposal_id=proposal_id, confirmation_token=token
                )
            elif pending == "WALLET_CREDIT":
                exec_result = wallet_mut.execute_wallet_credit_request(
                    user=user, proposal_id=proposal_id, confirmation_token=token
                )
            else:
                exec_result = booking_mut.execute_booking_create(
                    user=user, proposal_id=proposal_id, confirmation_token=token
                )
            result = _exec_to_response(exec_result)
    else:
        return None

    if result is None:
        return None

    meta = dict(result.get("metadata") or {})
    meta["intent"] = intent.intent
    meta["v2"] = True
    meta["llm_used"] = False
    result["metadata"] = meta
    _store_context(conversation, meta)
    return result

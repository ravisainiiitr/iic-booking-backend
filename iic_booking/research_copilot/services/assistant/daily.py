"""
Day-to-day answers for the Booking Assistant: bookings, wallet, recharge, results, invoices, waitlist,
urgent requests, templates, ratings, tickets, students, staff queues.

Every answer comes from live portal data or a short verified how-to, never from the LLM, and ends with
the next steps the user can actually take (chips). Booking changes reuse the existing cancel / reschedule
proposal flow, so nothing changes until the user presses its Confirm button. Edits, payments, messages
and ratings happen on the portal page the chip opens.
"""

from __future__ import annotations

import logging
from typing import Any

from django.utils import timezone

from iic_booking.research_copilot.services.assistant import bookings as B
from iic_booking.research_copilot.services.assistant import cards as C
from iic_booking.research_copilot.services.assistant.intents import Detected

logger = logging.getLogger(__name__)

STAFF_TYPES = {"manager", "operator", "admin", "dept_admin"}
STUDENT_TYPES = {"student", "individual_student"}
EXTERNAL_TYPES = {"external", "rnd", "industry", "startup_incubated_iitr", "external_startup_msme", "other"}


def user_type(user) -> str:
    raw = getattr(user, "user_type", "") or ""
    return str(getattr(raw, "value", raw)).strip().lower()


def is_staff(user) -> bool:
    return user_type(user) in STAFF_TYPES or bool(getattr(user, "is_superuser", False))


def _money(value) -> str:
    try:
        return f"₹{float(value):,.2f}"
    except (TypeError, ValueError):
        return "₹0.00"


def _local(dt, fmt: str = "%d %b %Y") -> str:
    return timezone.localtime(dt).strftime(fmt) if dt else ""


# =============================================================================== routing

def handle(user, conversation, det: Detected, text: str) -> dict[str, Any] | None:
    handler = _HANDLERS.get(det.intent)
    if handler is None:
        return None
    return handler(user, conversation, det.params, text)


# =============================================================================== help / fallback

def help_actions(user) -> list[dict[str, Any]]:
    t = user_type(user)
    if t in {"manager", "operator"}:
        return [
            C.prompt_action("Today's bookings on my equipment", "Today's bookings on my equipment", primary=True),
            C.prompt_action("Pending approvals", "Pending approvals on my equipment"),
            C.prompt_action("Urgent requests", "Urgent requests queue on my equipment"),
            C.prompt_action("Waitlist queue", "Waitlist queue on my equipment"),
        ]
    if t in {"admin", "dept_admin"}:
        return [
            C.prompt_action("Today's bookings", "Today's bookings on my equipment", primary=True),
            C.prompt_action("Pending approvals", "Pending approvals on my equipment"),
            C.prompt_action("My upcoming bookings", "Show my upcoming bookings"),
            C.prompt_action("Reports", "Open reports"),
        ]
    out = [
        C.prompt_action("My upcoming bookings", "Show my upcoming bookings", primary=True),
        C.prompt_action("Wallet balance", "What is my wallet balance?"),
        C.prompt_action("How to recharge", "How do I recharge my wallet?"),
        C.flow_action("Book equipment", "start"),
    ]
    if t == "faculty":
        out.insert(3, C.prompt_action("My students", "Manage my students and spending limits"))
    return out


def help_reply(user, conversation=None, params=None, text: str = "") -> dict[str, Any]:
    t = user_type(user)
    lines = ["I can help with your bookings, wallet and lab requests. For example:", ""]
    if t in {"manager", "operator", "admin", "dept_admin"}:
        lines += [
            "- \"Today's bookings on my equipment\" · \"Pending approvals\" · \"Urgent requests queue\"",
            "- \"Show my upcoming bookings\" · \"Status of booking IICXRD202600012\"",
            "- \"Charges for XRD\" · \"Is FESEM free tomorrow?\"",
        ]
    else:
        lines += [
            "- \"Show my upcoming bookings\" — then cancel, reschedule or edit with one tap",
            "- \"Wallet balance\" · \"How do I recharge my wallet?\" · \"My transactions\"",
            "- \"Is FESEM free tomorrow?\" · \"Charges for XRD\" · \"Results of my last booking\"",
        ]
        if t == "faculty":
            lines.append("- \"Manage my students\" · \"Set a spending limit for a student\"")
    return C.reply("\n".join(lines), actions=help_actions(user), intent="help", title_hint="Booking Assistant help")


def fallback_reply(user, *, reason: str = "") -> dict[str, Any]:
    msg = reason or "I'm not sure I understood that."
    return C.reply(
        f"{msg} Here are some things I can do right away — or rephrase your question.",
        actions=help_actions(user) + [C.link("Raise a support ticket", "/tickets")],
        intent="fallback",
        kind="CLARIFICATION",
    )


# =============================================================================== bookings

def _target_booking(user, conversation, params: dict[str, Any]):
    """The booking a message points at: an ID, an ordinal from the last list, or "that one"."""
    from iic_booking.research_copilot.services.assistant import state as ba_state

    if params.get("ref"):
        return B.find_owned(user, str(params["ref"]))
    st = ba_state.load(conversation)
    ids = [int(i) for i in (st.get("last_booking_ids") or []) if str(i).isdigit()]
    n = params.get("ordinal")
    if n is not None and ids:
        idx = len(ids) - 1 if n == -1 else n - 1
        if 0 <= idx < len(ids):
            return B.owned(user, ids[idx])
        return None
    if params.get("last") and st.get("focus_booking_id"):
        return B.owned(user, st["focus_booking_id"])
    return None


def bookings_list(user, conversation, params, text):
    return B.list_reply(
        user, conversation, scope=params.get("scope") or "recent", statuses=params.get("statuses"),
        limit=int(params.get("limit") or B.MAX_LIST), text=text,
    )


def booking_details(user, conversation, params, text):
    b = _target_booking(user, conversation, params)
    if b is None and params.get("soft"):
        return None
    if b is None:
        ref = params.get("ref")
        return C.reply(
            (f"I couldn't find booking **{ref}** among your bookings." if ref else "Which booking do you mean?")
            + " Here are your recent bookings.",
            actions=[C.prompt_action("Show my bookings", "Show my recent bookings", primary=True),
                     C.link("Open My Bookings", "/my-bookings")],
            intent="booking_details",
        )
    return B.detail_reply(user, conversation, b)


_OP_VERBS = {"cancel": "cancel", "reschedule": "reschedule", "edit": "edit"}


def _pick(user, conversation, op: str, *, next_only: bool = False) -> dict[str, Any]:
    """No booking named: act on the only eligible one, or list the eligible ones with that op's chip."""
    now = timezone.now()
    qs = B._base_qs(user).filter(status__in=B.ACTIVE_STATUSES if op != "edit" else ("BOOKED",))
    rows = []
    for b in qs:
        elig = B.eligibility(b, now=now)
        if elig["future"]:
            rows.append((b, elig))
    rows.sort(key=lambda be: B._row(be[0])["start"] or "")
    verb = _OP_VERBS[op]
    allowed = [(b, e) for b, e in rows if e.get(op)]
    if next_only and rows:
        return booking_op(user, conversation, rows[0][0], op)
    if not rows:
        return C.reply(
            f"You have no upcoming bookings to {verb}.",
            actions=[C.prompt_action("Past bookings", "Show my past bookings"), C.flow_action("Book equipment", "start"),
                     C.link("Open My Bookings", "/my-bookings")],
            intent=op,
        )
    if not allowed:
        return booking_op(user, conversation, rows[0][0], op)
    if len(allowed) == 1:
        return booking_op(user, conversation, allowed[0][0], op)
    items = []
    for b, e in allowed[: B.MAX_LIST]:
        r = B._row(b, e)
        r["actions"] = [B.chip(b, op, primary=True), B.chip(b, "details")]
        items.append(r)
    B._remember(conversation, items)
    return C.reply(
        f"Which booking would you like to {verb}? Pick one below, or say \"the first one\" / \"the second one\".",
        cards=[{"type": "ba_bookings", "title": f"Choose a booking to {verb}", "items": items}],
        actions=[C.link("Open My Bookings", "/my-bookings")],
        intent=f"{op}_choose",
        title_hint=f"{verb.title()} booking",
    )


def _change(op: str):
    def run(user, conversation, params, text):
        b = _target_booking(user, conversation, params)
        if params.get("ref") and b is None:
            return C.reply(
                f"I couldn't find booking **{params['ref']}** among your bookings.",
                actions=[C.prompt_action("Show my bookings", "Show my upcoming bookings", primary=True)],
                intent=op,
            )
        if b is None:
            return _pick(user, conversation, op, next_only=bool(params.get("next")))
        return booking_op(user, conversation, b, op)

    return run


def _intel_turn(user, conversation, text: str, intent: str):
    from iic_booking.research_copilot.services.intelligence import entities as entity_svc
    from iic_booking.research_copilot.services.intelligence import state as intel_state
    from iic_booking.research_copilot.services.intelligence.flows import Turn

    return Turn(user=user, text=text, conversation=conversation, state=intel_state.load(conversation),
                ents=entity_svc.extract(text), intent=intent)


def _run_change(user, conversation, b, op: str) -> dict[str, Any]:
    """Hand the booking to the existing cancel / reschedule flow (proposal + explicit Confirm)."""
    from iic_booking.research_copilot.services.intelligence import booking_changes as changes
    from iic_booking.research_copilot.services.intelligence import state as intel_state

    active = changes._owned(user, b.pk)
    if active is None:
        return _not_changeable(b, op)
    turn = _intel_turn(user, conversation, f"{op} booking", "CANCELLATION_REQUEST" if op == "cancel" else "RESCHEDULING_REQUEST")
    intel_state.restart(turn.state, op)
    out = changes.cancel_selected(turn, active) if op == "cancel" else changes.reschedule_selected(turn, active)
    turn.state["last_intent"] = turn.intent
    intel_state.save(conversation, turn.state)
    meta = out.setdefault("metadata", {})
    meta.setdefault("booking_assistant", True)
    meta.setdefault("deterministic", True)
    meta.setdefault("llm_used", False)
    meta.setdefault("title_hint", f"{op.title()} booking")
    return out


def _not_changeable(b, op: str) -> dict[str, Any]:
    from iic_booking.research_copilot.services.booking_refs import display_ref

    elig = B.eligibility(b)
    ref = display_ref(b)
    verb = {"cancel": "cancelled", "reschedule": "rescheduled", "edit": "edited"}[op]
    if op == "reschedule" and elig.get("reschedule_locked"):
        from iic_booking.equipment.reschedule_lock import RESCHEDULE_LOCKED_SAMPLE_ACCEPTED_MESSAGE

        return C.reply(
            f"Booking **{ref}**: {RESCHEDULE_LOCKED_SAMPLE_ACCEPTED_MESSAGE}",
            actions=[B.chip(b, "message", primary=True), C.link("Open booking", f"/my-bookings?booking={b.pk}")],
            intent=op,
        )
    if elig["active"] and not elig["self_service_open"]:
        why = f"the self-service window closed on {elig['cutoff']}" if elig.get("cutoff") else "the self-service window has closed"
        from iic_booking.research_copilot.services.intelligence import messages as M

        return C.reply(
            f"Booking **{ref}** can't be {verb} here because {why}. The lab or an admin can still help.",
            actions=[B.chip(b, "message", primary=True), M.ticket_action("user_requested", "Ask the admin (support ticket)"),
                     C.link("Open booking", f"/my-bookings?booking={b.pk}")],
            intent=op,
        )
    label = b.get_status_display() if hasattr(b, "get_status_display") else b.status
    if op == "edit":
        reason = ("only **Booked** bookings can be edited, until the analysis is completed"
                  if str(b.status) != "BOOKED" else "it is a repeat booking created by the lab")
    elif str(b.status) == "WAITLISTED":
        reason = "it is a waitlist entry — leave the waitlist from Equipment Waitlist instead"
    elif not elig["future"]:
        reason = "its slots have already started or passed"
    else:
        reason = f"its status is **{label}**"
    return C.reply(
        f"Booking **{ref}** can't be {verb} because {reason}.",
        actions=[a for a in B.chips_for(b, elig, include_details=True, limit=4)] + [C.link("Open booking", f"/my-bookings?booking={b.pk}")],
        intent=op,
    )


def _edit_refund_timing(b) -> str:
    """How a lower charge from editing `b` now is refunded, with the actual cancellation deadline."""
    from iic_booking.equipment.input_edit_refund_window import instant_refund_window

    try:
        is_open, cutoff = instant_refund_window(b)
    except Exception:
        logger.exception("Could not work out the edit refund window for booking %s", getattr(b, "pk", None))
        is_open, cutoff = False, None
    approval = "the refund needs the Officer In Charge's approval."
    if is_open and cutoff:
        return (f"the difference is refunded to your wallet straight away if you save before the cancellation "
                f"deadline ({_local(cutoff, '%a %d %b %Y, %H:%M')}); after that, {approval}")
    if is_open:
        return "the difference is refunded to your wallet straight away."
    if cutoff:
        return f"the cancellation deadline ({_local(cutoff, '%a %d %b %Y, %H:%M')}) has passed, so {approval}"
    return approval


def booking_op(user, conversation, b, op: str) -> dict[str, Any]:
    """One per-booking action. `b` is already owned by `user`; eligibility is re-checked here."""
    from iic_booking.research_copilot.services.booking_refs import display_ref

    if op == "details":
        return B.detail_reply(user, conversation, b)
    elig = B.eligibility(b)
    ref = display_ref(b)
    href = f"/my-bookings?booking={b.pk}"
    if op in ("cancel", "reschedule"):
        if not elig[op]:
            return _not_changeable(b, op)
        return _run_change(user, conversation, b, op)
    if op == "edit":
        if not elig["edit"]:
            return _not_changeable(b, "edit")
        return C.reply(
            f"**Edit parameters — {ref}** ({b.equipment.name})\n\n"
            "1. Press **Edit parameters** below; the booking opens with the edit form.\n"
            "2. Change the inputs or sample sets and review the new charge the portal shows.\n"
            "3. Save. If the charge goes up you have 1 minute to pay the difference, otherwise the edit is undone. "
            f"If the new charge is lower, {_edit_refund_timing(b)}\n\n"
            "Nothing changes until you save on that page.",
            actions=[C.link("Edit parameters", f"{href}&edit_inputs=1", primary=True), B.chip(b, "details")],
            intent="edit",
            title_hint=f"Edit {ref}",
        )
    if op == "message":
        if not elig["message"]:
            from iic_booking.research_copilot.services.intelligence import messages as M

            reason = elig.get("message_reason") or "Messages are not available for this booking."
            return C.reply(reason, actions=[M.ticket_action("user_requested", "Raise a support ticket"),
                                            C.link("Open booking", href)], intent="message_lab")
        return C.reply(
            f"Open booking **{ref}** and use **Message the lab** at the bottom of the details. "
            "The lab staff reply in the same thread and you get a notification.",
            actions=[C.link("Message the lab", href, primary=True), B.chip(b, "details")],
            intent="message_lab",
            title_hint=f"Message lab — {ref}",
        )
    if op == "results":
        if elig["results"] and elig["results_blocked"] == "rating":
            return C.reply(
                f"Results for **{ref}** ({b.equipment.name}) are uploaded. Submit your rating for this booking first; "
                "the download unlocks right after.",
                actions=[*(([B.chip(b, "rate", primary=True)]) if elig["rate"] else []), C.link("Open booking", href)],
                intent="results",
            )
        if elig["results"] and elig["results_blocked"] == "istem_fbr":
            return C.reply(
                f"Results for **{ref}** are uploaded but locked until your I-STEM FBR number is verified. Enter the FBR "
                "number on the booking; the Officer In Charge verifies it and the download unlocks.",
                actions=[C.link("Open booking", href, primary=True)],
                intent="results",
            )
        if elig["results"]:
            return C.reply(
                f"Results for **{ref}** ({b.equipment.name}) are available. Open the booking to view and download them.",
                actions=[C.link("View results", href, primary=True), C.link("All my results", "/my-results"),
                         *(([B.chip(b, "rate")]) if elig["rate"] else [])],
                intent="results",
            )
        return C.reply(
            f"No results have been uploaded for **{ref}** yet (status: {b.get_status_display()}). "
            "You'll get an email and a notification when they are published.",
            actions=[*(([B.chip(b, "message")]) if elig["message"] else []), C.link("Open booking", href)],
            intent="results",
        )
    if op == "invoice":
        if not elig["invoice"]:
            return C.reply(
                f"The invoice for **{ref}** becomes available once the analysis is completed. "
                "For a quote before booking or payment, use **Proforma Invoice**.",
                actions=[C.link("Proforma invoice", "/proforma-invoice"), C.link("Open booking", href)],
                intent="invoice",
            )
        return C.reply(
            f"Open booking **{ref}** and press **Invoice (PDF)** in the documents row to download it.",
            actions=[C.link("Download invoice", href, primary=True), C.link("Proforma invoice", "/proforma-invoice")],
            intent="invoice",
        )
    if op == "rate":
        if not elig["rate"]:
            return C.reply(f"Booking **{ref}** can't be rated (only completed bookings that haven't been rated yet, on "
                           "equipment with ratings switched on).", actions=[C.link("Open booking", href)], intent="rate")
        return C.reply(
            f"Open booking **{ref}** and fill **Rate this booking** (overall rating plus a few yes/no questions).",
            actions=[C.link("Rate experience", href, primary=True)],
            intent="rate",
        )
    if op == "rebook":
        from iic_booking.research_copilot.services.assistant import guided
        from iic_booking.research_copilot.services.assistant.engine import _visible

        if _visible(user, b.equipment_id) is None:
            return C.reply(f"{b.equipment.name} can't be booked from your account right now.",
                           actions=[C.link("Browse equipment", "/equipments")], intent="rebook")
        return guided.handle(user, conversation, {"step": "equipment", "equipment_id": int(b.equipment_id)})
    if op == "template":
        return C.reply(
            f"To reuse the inputs of **{ref}**: open **Booking Templates** → **New template**, choose "
            f"**{b.equipment.name}**, fill the same inputs and press **Save template**. Next time pick it under "
            "Booking template on the booking page. (After any booking attempt you can also use "
            "**Save these parameters as a template**.)",
            actions=[C.link("Booking Templates", "/booking-templates", primary=True)],
            intent="template",
        )
    return C.reply("That option is not available.", intent="invalid")


def dispatch_booking_action(user, conversation, payload: dict[str, Any]) -> dict[str, Any]:
    b = B.owned(user, payload.get("booking_id"))
    if b is None:
        return C.reply("I couldn't find that booking among yours.",
                       actions=[C.prompt_action("Show my bookings", "Show my recent bookings", primary=True)],
                       intent="booking_gone")
    return booking_op(user, conversation, b, str(payload.get("op") or "details"))


def message_lab(user, conversation, params, text):
    b = _target_booking(user, conversation, params)
    if b is not None:
        return booking_op(user, conversation, b, "message")
    return C.reply(
        "Every booking has its own **Message the lab** thread: open the booking in My Bookings and write at the "
        "bottom of the details. For questions before booking, ask me \"who is the OIC of <equipment>\" for "
        "contacts, or raise a support ticket.",
        actions=[C.prompt_action("Pick a booking", "Show my upcoming bookings", primary=True),
                 C.link("Open My Bookings", "/my-bookings"), C.link("Support tickets", "/tickets")],
        intent="message_lab",
    )


def howto_edit(user, conversation, params, text):
    return C.reply(
        "**Edit booking parameters**\n\n"
        "1. Open the booking in My Bookings (or ask me \"show my upcoming bookings\" and press **Edit parameters**).\n"
        "2. Choose **Edit User Inputs**, change the values or sample sets and save.\n"
        "3. A higher charge must be paid within 1 minute or the edit is undone. If the new charge is lower, the "
        "difference is refunded to your wallet straight away when you edit before the cancellation deadline; after "
        "that deadline the refund needs the Officer In Charge's approval.\n\n"
        "You can edit **Booked** bookings until the analysis is completed.",
        actions=[C.prompt_action("Pick a booking to edit", "Edit my booking", primary=True), C.link("Open My Bookings", "/my-bookings")],
        intent="howto_edit",
    )


# =============================================================================== wallet

def _caps(user):
    from iic_booking.research_copilot.services.intelligence.capabilities import Capabilities

    return Capabilities(user)


def wallet_balance(user, conversation, params, text):
    from iic_booking.research_copilot.services import tools as tools_svc

    caps = _caps(user)
    if not caps.has_wallet:
        return C.reply(
            "Your account doesn't have a wallet. Bookings are paid as shown on the booking page"
            + (" (wallet partial + online / offline payment for external users)." if user_type(user) in EXTERNAL_TYPES else "."),
            actions=[C.link("Open Wallet", "/wallet")],
            intent="balance",
        )
    data = (tools_svc._get_wallet(arguments={}, user=user) or {}).get("data") or {}
    bal = data.get("balance")
    if bal is None:
        return C.reply("Your wallet hasn't been set up yet. It is created when you first open the Wallet page.",
                       actions=[C.link("Open Wallet", "/wallet", primary=True)], intent="balance")
    lines = [f"Your wallet balance is **{_money(bal)}**."]
    subs = data.get("sub_wallets") or []
    if len(subs) > 1:
        lines += ["", "By department:"] + [f"- {s.get('department') or 'Department'}: {_money(s.get('balance'))}" for s in subs]
    if caps.wallet_is_shared:
        owner = f" ({caps.wallet_owner_name})" if caps.wallet_owner_name else ""
        lines += ["", f"This is your faculty's wallet{owner}; your bookings are charged to it."]
    lines += ["", B.NEXT_PROMPT]
    actions = []
    if caps.can_recharge:
        actions.append(C.prompt_action("Recharge", "How do I recharge my wallet?", primary=True))
    actions.append(C.prompt_action("View transactions", "Show my wallet transactions"))
    actions.append(C.link("Open Wallet", "/wallet"))
    return C.reply("\n".join(lines), cards=[{"type": "wallet", "balance": bal, "currency": "INR", "sub_wallets": subs}],
                   actions=actions, intent="balance", title_hint="Wallet balance")


def recharge_methods(user) -> list[tuple[str, bool, str]]:
    """(label, enabled, how) for each recharge method, from the Main Administrator's live switches."""
    try:
        from iic_booking.users.models.wallet_sric_settings import wallet_mode_flags

        flags = wallet_mode_flags(user)
    except Exception:  # noqa: BLE001
        flags = {"direct_cash_recharge_enabled": True}
    out: list[tuple[str, bool, str]] = []
    if user_type(user) == "faculty":
        out.append(("Project Grant", bool(flags.get("project_grant_recharge_enabled")),
                    "pick or add your sponsored project; the SRIC Office approves it"))
    out.append(("Direct Cash Deposit / Bank Transfer", bool(flags.get("direct_cash_recharge_enabled")),
                "deposit or transfer at the SRIC Bill Section and share the transaction number with them"))
    out.append(("Pay online", bool(flags.get("online_gateway_recharge_enabled")), "pay by card / net banking on the gateway"))
    return out


def recharge_steps_text(user, *, shared_owner: str | None = None) -> str:
    methods = recharge_methods(user)
    lines = []
    for label, enabled, how in methods:
        lines.append(f"   - **{label}** — {how}." if enabled else f"   - **{label}** — Awaiting Competent Authority Approval (not available yet).")
    body = [
        "1. Open **Wallet** and press **Recharge Wallet**.",
        "2. Choose the method:",
        *lines,
        "3. Pick the department sub-wallet under **Credit to** and enter the **Amount** (minimum ₹100).",
        "4. Accept the undertaking and enter the OTP sent to your email (valid 10 minutes).",
        "5. Note the Transaction ID. The amount is credited after approval and you get an email.",
    ]
    if shared_owner is not None:
        body.append(f"\nOnce approved, the funds go to your supervisor's wallet{f' ({shared_owner})' if shared_owner else ''}.")
    body.append("\nReceipt upload is no longer used — you don't need to upload a payment receipt.")
    return "\n".join(body)


def recharge(user, conversation, params, text):
    caps = _caps(user)
    if not caps.has_wallet:
        return C.reply("Your account doesn't have a wallet to recharge. Payment options are shown on the booking page.",
                       actions=[C.link("Open Wallet", "/wallet")], intent="recharge")
    if not caps.can_recharge:
        owner = caps.wallet_owner_name or "your faculty supervisor"
        return C.reply(
            f"You book against **{owner}**'s wallet, so the wallet owner recharges it. Ask them to recharge from "
            "**Wallet → Recharge Wallet**. Your own bookings and spending limit stay visible on the booking page.",
            actions=[C.prompt_action("Wallet balance", "What is my wallet balance?", primary=True),
                     C.prompt_action("My transactions", "Show my wallet transactions")],
            intent="recharge",
            title_hint="Wallet recharge",
        )
    from iic_booking.research_copilot.services.v2.mutations.wallet import parse_inr_amount, recharge_href

    amount = None
    try:
        amount = parse_inr_amount(text, None)
    except Exception:  # noqa: BLE001
        amount = None
    if amount is not None and (amount <= 0 or amount > 10_000_000):
        amount = None
    href = recharge_href(department_id=None, amount=amount)
    shared = caps.wallet_owner_name if caps.wallet_is_shared else None
    content = "**How to recharge your wallet**\n\n" + recharge_steps_text(user, shared_owner=shared)
    if amount is not None:
        content += f"\n\nThe recharge form will open with **₹{amount}** filled in."
    return C.reply(
        content,
        actions=[C.link("Open recharge form", href, primary=True),
                 C.prompt_action("Wallet balance", "What is my wallet balance?"),
                 C.prompt_action("View transactions", "Show my wallet transactions")],
        intent="recharge",
        title_hint="Wallet recharge",
    )


def howto_wallet(user, conversation, params, text):
    return C.reply(
        "**Wallet basics**\n\n"
        "- Your bookings are charged to your wallet (students: to the supervisor's wallet after the faculty approves "
        "your join request).\n"
        "- Wallet shows one sub-wallet per department, all transactions, and the **Recharge Wallet**, **Transfer** "
        "(faculty) and **Credit Facility** buttons.\n"
        "- Faculty can cap each student's spending under Student management.",
        actions=[C.prompt_action("Wallet balance", "What is my wallet balance?", primary=True),
                 C.prompt_action("How to recharge", "How do I recharge my wallet?"),
                 C.link("Open Wallet", "/wallet")],
        intent="howto_wallet",
    )


def transactions(user, conversation, params, text):
    from django.db.models import Q

    from iic_booking.users.models import SubWalletTransaction

    caps = _caps(user)
    if caps.wallet is None:
        return C.reply("Your wallet has no transactions yet.", actions=[C.link("Open Wallet", "/wallet")], intent="transactions")
    qs = SubWalletTransaction.objects.filter(sub_wallet__wallet=caps.wallet).order_by("-created_at")
    if caps.wallet_is_shared:
        # Same visibility as the Wallet page: on a shared wallet a student sees only their own debits plus credits.
        qs = qs.filter(Q(related_user_id=user.pk) | Q(transaction_type=SubWalletTransaction.TransactionType.CREDIT))
    rows = list(qs[:8])
    if not rows:
        lines = ["No wallet transactions yet."]
    else:
        lines = ["**Recent wallet transactions**", ""]
        for t in rows:
            kind = "Credit" if t.transaction_type == SubWalletTransaction.TransactionType.CREDIT else "Debit"
            desc = " ".join(str(t.description or "").split())[:80]
            lines.append(f"- {_local(t.created_at)}: {kind} {_money(t.amount)}" + (f" — {desc}" if desc else ""))
    items = [{"date": t.created_at.isoformat() if t.created_at else None, "type": t.transaction_type,
              "amount": str(t.amount), "description": str(t.description or "")[:200]} for t in rows]
    actions = [C.prompt_action("Wallet balance", "What is my wallet balance?")]
    if caps.can_recharge:
        actions.append(C.prompt_action("Recharge", "How do I recharge my wallet?"))
    actions.append(C.link("Full statement", "/wallet"))
    return C.reply("\n".join(lines), cards=[{"type": "transactions", "items": items}], actions=actions,
                   intent="transactions", title_hint="Wallet transactions")


def invoices(user, conversation, params, text):
    b = _target_booking(user, conversation, params)
    if b is not None:
        return booking_op(user, conversation, b, "invoice")
    done = list(B._base_qs(user).filter(status="COMPLETED").order_by("-pk")[:3])
    lines = [
        "**Invoices**",
        "",
        "- **Booking invoice**: open a completed booking and press **Invoice (PDF)**.",
        "- **Proforma invoice** (a quote before payment, e.g. for project or external funding): use the Proforma "
        "Invoice page.",
    ]
    from iic_booking.research_copilot.services.booking_refs import display_ref

    actions = []
    for x in done:
        a = B.chip(x, "invoice")
        a["label"] = f"Invoice {display_ref(x)}"
        actions.append(a)
    actions += [C.link("Proforma invoice", "/proforma-invoice"), C.link("Open My Bookings", "/my-bookings")]
    return C.reply("\n".join(lines), actions=actions, intent="invoices", title_hint="Invoices")


# =============================================================================== waitlist / urgent / templates

def waitlist(user, conversation, params, text):
    from iic_booking.equipment.models import WaitlistEntry

    if is_staff(user) and not params.get("join") and not params.get("leave"):
        return staff_waitlist(user, conversation, params, text)
    mine = list(WaitlistEntry.objects.filter(user=user, status="ACTIVE").select_related("equipment").order_by("created_at")[:6])
    lines = []
    if mine:
        lines += ["**Your waitlist entries**", ""]
        for e in mine:
            pos = WaitlistEntry.objects.filter(equipment_id=e.equipment_id, status="ACTIVE", created_at__lt=e.created_at).count() + 1
            lines.append(f"- {e.equipment.name}: position **WL{pos}** (joined {_local(e.created_at)})")
        lines.append("")
    elif not params.get("join"):
        lines += ["You are not on any waitlist.", ""]
    if params.get("leave"):
        lines.append("To leave a waitlist, open the entry in **My Bookings** and press **Leave Waitlist**. Everyone behind you moves up.")
    else:
        lines += [
            "**How the waitlist works**",
            "1. When no slot is free, tick **Add to the waitlist if the booking cannot be completed** on the booking page "
            "(only on equipment with a waitlist).",
            "2. You are not charged while waiting; when a slot opens you are booked first come, first served and the "
            "wallet is charged then.",
            "3. You may submit your sample while waitlisted so the lab already has it.",
        ]
    return C.reply("\n".join(lines),
                   actions=[C.link("My waitlist entries", "/my-bookings", primary=True), C.flow_action("Book equipment", "start")],
                   intent="waitlist", title_hint="Waitlist")


def urgent(user, conversation, params, text):
    from iic_booking.equipment.models import UrgentBookingRequest

    if is_staff(user) and not params.get("howto"):
        return staff_urgent(user, conversation, params, text)
    mine = list(UrgentBookingRequest.objects.filter(user=user).select_related("equipment").order_by("-requested_at")[:5])
    lines = []
    if mine:
        lines += ["**Your urgent requests**", ""] + [
            f"- {r.equipment.name} — {r.get_status_display()} (requested {_local(r.requested_at)})" for r in mine
        ] + [""]
    lines += [
        "**Request an urgent booking**",
        "1. On the booking page press **Request urgent booking** (students can also use Urgent booking request on the dashboard).",
        "2. **Type A — Rush relief** (no surcharge): after at least 2 failed peak-window attempts in 14 days.",
        "3. **Type B — Urgent with reason** (50% surcharge): pick slots, give the reason and accept the surcharge. "
        "Students need their supervisor's approval first; the Officer In Charge gives final approval.",
        "The wallet is charged only after final approval.",
    ]
    return C.reply("\n".join(lines),
                   actions=[C.link("My urgent requests", "/my-urgent-requests", primary=True), C.flow_action("Book equipment", "start")],
                   intent="urgent", title_hint="Urgent booking")


def templates(user, conversation, params, text):
    from iic_booking.equipment.models import BookingInputTemplate

    rows = list(BookingInputTemplate.objects.filter(user=user).select_related("equipment").order_by("-pk")[:6])
    lines = []
    if rows:
        lines += [f"You have {len(rows)}{'+' if len(rows) == 6 else ''} saved template(s):", ""]
        lines += [f"- **{t.name}** — {t.equipment.name}" for t in rows] + [""]
    lines += [
        "**Booking templates** save the booking form for one equipment (inputs, sample sets, options and an optional "
        "preferred slot). They never book by themselves — you still press Book.",
        "- Create: **Booking Templates → New template**, or **Save these parameters as a template** after a booking attempt.",
        "- Use: pick it under **Booking template** on the booking page, or press **Book now** on the template.",
    ]
    return C.reply("\n".join(lines),
                   actions=[C.link("Booking Templates", "/booking-templates", primary=True), C.flow_action("Book equipment", "start")],
                   intent="templates", title_hint="Booking templates")


def rate(user, conversation, params, text):
    b = _target_booking(user, conversation, params)
    if b is not None:
        return booking_op(user, conversation, b, "rate")
    pending = [x for x in B._base_qs(user).filter(status="COMPLETED", rating__isnull=True).order_by("-pk")[:12]
               if getattr(x.equipment, "user_rating_enabled", False)][:4]
    if not pending:
        return C.reply("You have no completed bookings waiting for a rating. Thanks for your feedback!",
                       actions=[C.prompt_action("Recent bookings", "Show my recent bookings")], intent="rate")
    from iic_booking.research_copilot.services.booking_refs import display_ref

    actions = []
    for x in pending:
        a = B.chip(x, "rate")
        a["label"] = f"Rate {display_ref(x)}"
        actions.append(a)
    actions.append(C.link("All bookings to rate", "/my-bookings?pending_rating=1"))
    return C.reply(f"{len(pending)} completed booking(s) are waiting for your rating. Pick one:", actions=actions,
                   intent="rate", title_hint="Rate your experience")


def results(user, conversation, params, text):
    b = _target_booking(user, conversation, params)
    if b is not None:
        return booking_op(user, conversation, b, "results")
    recent = list(B._base_qs(user).filter(status__in=("PROCESSING", "COMPLETED")).order_by("-pk")[:6])
    if not recent:
        return C.reply("You have no completed bookings with results yet.",
                       actions=[C.prompt_action("Upcoming bookings", "Show my upcoming bookings"), C.link("My results", "/my-results")],
                       intent="results")
    from iic_booking.research_copilot.services.booking_refs import display_ref

    lines, actions = ["**Results for your recent bookings**", ""], []
    for x in recent:
        ready = B.eligibility(x)["results"]
        lines.append(f"- {display_ref(x)} {x.equipment.name}: " + ("results available" if ready else "not uploaded yet"))
        if ready and len(actions) < 3:
            a = B.chip(x, "results")
            a["label"] = f"Results {display_ref(x)}"
            actions.append(a)
    actions.append(C.link("All my results", "/my-results"))
    return C.reply("\n".join(lines), actions=actions, intent="results", title_hint="Results")


# =============================================================================== support / research / faculty

def tickets(user, conversation, params, text):
    from iic_booking.support.models import Ticket

    from iic_booking.research_copilot.services.intelligence import messages as M

    rows = list(Ticket.objects.filter(user=user).order_by("-created_at")[:5])
    lines = ["**Your recent support tickets**", ""] + [f"- #{t.ticket_id} {t.subject[:80]} ({t.get_status_display()})" for t in rows] \
        if rows else ["You have no support tickets."]
    actions = [C.link(f"Ticket #{t.ticket_id}", f"/tickets?ticket={t.ticket_id}") for t in rows[:3]]
    actions.append(M.ticket_action("user_requested", "Raise a support ticket"))
    return C.reply("\n".join(lines), actions=actions, intent="tickets", title_hint="Support tickets")


def ticket_create(user, conversation, params, text):
    from iic_booking.research_copilot.services.intelligence import messages as M

    return C.reply(
        "I can raise a support ticket for the IIC team with this conversation attached. Describe the problem in a "
        "message first if you haven't, then press **Raise a support ticket**. Nothing is sent until you do.",
        actions=[M.ticket_action("user_requested", "Raise a support ticket"), C.link("My tickets", "/tickets")],
        intent="ticket_create",
        title_hint="Support request",
        escalate=True,
    )


def students(user, conversation, params, text):
    from iic_booking.users.models.wallet import WalletJoinRequest

    t = user_type(user)
    if t == "faculty":
        linked = WalletJoinRequest.objects.filter(faculty=user, status="APPROVED")
        pending = WalletJoinRequest.objects.filter(faculty=user, status="PENDING").count()
        limited = linked.filter(spending_limit_enabled=True).count()
        lines = [
            f"You have **{linked.count()}** linked student(s)"
            + (f", **{pending}** join request(s) waiting for you" if pending else "")
            + (f"; {limited} with a spending limit." if limited else "."),
            "",
            "**Student management**",
            "- Approve join requests to link a student to your wallet.",
            "- **Spending limit**: set a Weekly and/or Monthly limit (₹) per student and Save; leave empty for no limit. "
            "A booking that would exceed it is blocked.",
            "- Turn off **Linked** to delink a student.",
        ]
        return C.reply("\n".join(lines), actions=[C.link("Student management", "/student-management", primary=True),
                                                  C.prompt_action("Wallet balance", "What is my wallet balance?")],
                       intent="students", title_hint="My students")
    if t in STUDENT_TYPES:
        req = WalletJoinRequest.objects.filter(student=user).select_related("faculty").order_by("-created_at").first()
        if req and req.status == "APPROVED":
            limit = ""
            if req.spending_limit_enabled and (req.weekly_limit_inr or req.monthly_limit_inr):
                parts = [f"weekly {_money(req.weekly_limit_inr)}" if req.weekly_limit_inr else "",
                         f"monthly {_money(req.monthly_limit_inr)}" if req.monthly_limit_inr else ""]
                limit = " Your supervisor's spending limit: " + ", ".join(p for p in parts if p) + "."
            head = f"You are linked to **{getattr(req.faculty, 'name', '') or 'your supervisor'}**'s wallet.{limit}"
        elif req and req.status == "PENDING":
            head = f"Your request to join **{getattr(req.faculty, 'name', '') or 'the faculty'}**'s wallet is waiting for approval."
        else:
            head = "You are not linked to a faculty wallet yet."
        lines = [head, "", "**Link to your supervisor**",
                 "1. Open **Wallet** → **Request to Join Wallet**.",
                 "2. Find your faculty and press **Send Request**.",
                 "3. Once they approve, your bookings are charged to their wallet.",
                 "",
                 "Can't find your supervisor's name? Faculty appear in the list only after they have signed in to "
                 "the portal once. Use **Invite your supervisor** on the Wallet page to email them "
                 "an invitation — when they sign in it becomes a normal link request for them to approve."]
        return C.reply("\n".join(lines), actions=[C.link("Link my supervisor's wallet", "/wallet", primary=True),
                                                  C.prompt_action("Wallet balance", "What is my wallet balance?")],
                       intent="students", title_hint="Faculty wallet")
    return C.reply("Student management is for faculty accounts; students link to a faculty wallet from the Wallet page.",
                   actions=[C.link("Open Wallet", "/wallet")], intent="students")


def my_research(user, conversation, params, text):
    caps = _caps(user)
    if not caps.my_research_available:
        return C.reply("My Research isn't available for your account.", actions=help_actions(user)[:3], intent="my_research")
    lines = [
        "**My Research** keeps your bookings, results and notes together in workspaces.",
        "- Create a workspace with **New Workspace**, link bookings and share it with your group.",
        "- Results of linked bookings appear in the workspace automatically.",
    ]
    if caps.groups_available:
        lines.append("- **Research groups** let a faculty share workspaces with students.")
    return C.reply("\n".join(lines), actions=[C.link("Open My Research", "/my-research", primary=True),
                                              C.link("My results", "/my-results")],
                   intent="my_research", title_hint="My Research")


def reports(user, conversation, params, text):
    t = user_type(user)
    if t in {"admin", "dept_admin", "manager", "finance", "org_admin"} or getattr(user, "is_superuser", False):
        return C.reply(
            "Usage, revenue and booking reports are on the **Reports** page; the bookings list report can be filtered "
            "and exported.",
            actions=[C.link("Open Reports", "/reports", primary=True), C.link("Bookings report", "/reports/bookings")],
            intent="reports", title_hint="Reports",
        )
    return C.reply(
        "Portal reports are for lab and admin staff. Your own history is in My Bookings and Wallet transactions.",
        actions=[C.prompt_action("Past bookings", "Show my past bookings", primary=True),
                 C.prompt_action("My transactions", "Show my wallet transactions")],
        intent="reports",
    )


def charges_generic(user, conversation, params, text):
    from iic_booking.research_copilot.services.assistant import engine
    from iic_booking.research_copilot.services.assistant import state as ba_state

    eq = engine._context_equipment(user, conversation, ba_state.load(conversation)) if conversation is not None else None
    if eq is not None:
        return engine._for_equipment(user, conversation, eq, "info", None, "charges")
    return C.reply(
        "Charges depend on the equipment and your user type. Tell me the equipment (for example \"charges for XRD\"), "
        "or open the Analysis Charges list.",
        actions=[C.link("Analysis charges", "/analysis-charges", primary=True), C.link("Browse equipment", "/equipments")],
        intent="charges",
    )


# =============================================================================== staff queues

def staff_equipment_ids(user) -> list[int] | None:
    """Equipment the staff member handles; None means all equipment (admin)."""
    t = user_type(user)
    if t == "admin" or getattr(user, "is_superuser", False):
        return None
    if t == "dept_admin":
        from iic_booking.equipment.models import Equipment

        dept = getattr(user, "department_id", None)
        return list(Equipment.objects.filter(internal_department_id=dept).values_list("pk", flat=True)) if dept else []
    from iic_booking.support.ticket_service import handled_equipment_ids_for

    return handled_equipment_ids_for(user)


def _not_staff(user) -> dict[str, Any]:
    return C.reply("That view is for lab staff (OIC / Lab Operator / admins). Here are your own bookings instead.",
                   actions=[C.prompt_action("My upcoming bookings", "Show my upcoming bookings", primary=True)],
                   intent="staff_denied")


def _scope(qs, ids, field: str = "equipment_id"):
    return qs if ids is None else qs.filter(**{f"{field}__in": ids})


def staff_today(user, conversation, params, text):
    from iic_booking.equipment.models import DailySlot

    if not is_staff(user):
        return _not_staff(user)
    ids = staff_equipment_ids(user)
    today = timezone.localdate()
    slots = _scope(
        DailySlot.objects.filter(date=today, booking__isnull=False)
        .exclude(booking__status__in=("CANCELLED", "REFUNDED"))
        .select_related("booking__equipment", "booking__user"),
        ids, "booking__equipment_id",
    ).order_by("start_datetime")[:200]
    seen: dict[int, dict[str, Any]] = {}
    for s in slots:
        b = s.booking
        row = seen.get(b.pk)
        if row is None:
            from iic_booking.research_copilot.services.booking_refs import display_ref

            seen[b.pk] = row = {"booking_id": int(b.pk), "reference": display_ref(b), "equipment": b.equipment.name,
                                "status": b.status, "status_label": b.get_status_display(), "start": s.start_datetime,
                                "end": s.end_datetime, "user": getattr(b.user, "name", "") or "",
                                "href": f"/booking-management?expand={b.pk}"}
        row["end"] = max(row["end"], s.end_datetime) if row["end"] and s.end_datetime else row["end"]
    rows = list(seen.values())
    if not rows:
        return C.reply(f"No bookings on your equipment today ({today:%a %d %b}).",
                       actions=[C.prompt_action("Pending approvals", "Pending approvals on my equipment"),
                                C.link("View Booking", "/booking-management")], intent="staff_today")
    items = []
    for r in rows[:12]:
        when = f"{_local(r['start'], '%H:%M')}–{_local(r['end'], '%H:%M')}" if r["start"] else ""
        items.append({**r, "start": r["start"].isoformat() if r["start"] else None, "end": None,
                      "when": f"Today {when}" + (f" · {r['user']}" if r["user"] else ""), "charge": None})
    return C.reply(
        f"**{len(rows)} booking(s) on your equipment today** ({today:%a %d %b}).",
        cards=[{"type": "ba_bookings", "title": "Today's bookings", "items": items}],
        actions=[C.link("View Booking", "/booking-management", primary=True),
                 C.prompt_action("Pending approvals", "Pending approvals on my equipment"),
                 C.prompt_action("Urgent requests", "Urgent requests queue on my equipment")],
        intent="staff_today", title_hint="Today's bookings",
    )


def staff_approvals(user, conversation, params, text):
    from iic_booking.equipment.models import Booking

    if not is_staff(user):
        return _not_staff(user)
    ids = staff_equipment_ids(user)
    qs = _scope(Booking.objects.filter(status="PENDING"), ids).select_related("equipment").order_by("pk")
    count = qs.count()
    if not count:
        return C.reply("No bookings are waiting for approval on your equipment.",
                       actions=[C.prompt_action("Today's bookings", "Today's bookings on my equipment"),
                                C.link("View Booking", "/booking-management")], intent="staff_approvals")
    from iic_booking.research_copilot.services.booking_refs import display_ref

    lines = [f"**{count} pending booking(s)** on your equipment:", ""] + [
        f"- {display_ref(b)} — {b.equipment.name}" for b in qs[:8]
    ]
    return C.reply("\n".join(lines), actions=[C.link("Review in View Booking", "/booking-management", primary=True),
                                              C.prompt_action("Urgent requests", "Urgent requests queue on my equipment")],
                   intent="staff_approvals", title_hint="Pending approvals")


def staff_waitlist(user, conversation, params, text):
    from django.db.models import Count

    from iic_booking.equipment.models import WaitlistEntry

    if not is_staff(user):
        return waitlist(user, conversation, {"join": False}, text)
    ids = staff_equipment_ids(user)
    rows = list(_scope(WaitlistEntry.objects.filter(status="ACTIVE"), ids).values("equipment__name")
                .annotate(n=Count("id")).order_by("-n")[:8])
    if not rows:
        return C.reply("The waitlist on your equipment is empty.", actions=[C.link("Equipment Waitlist", "/equipment-waitlist")],
                       intent="staff_waitlist")
    lines = ["**Waitlist on your equipment**", ""] + [f"- {r['equipment__name']}: {r['n']} waiting" for r in rows]
    lines += ["", "Use **Confirm manually** on Equipment Waitlist to place a waitlisted booking into a free slot."]
    return C.reply("\n".join(lines), actions=[C.link("Equipment Waitlist", "/equipment-waitlist", primary=True)],
                   intent="staff_waitlist", title_hint="Waitlist queue")


def staff_urgent(user, conversation, params, text):
    from iic_booking.equipment.models import UrgentBookingRequest

    if not is_staff(user):
        return urgent(user, conversation, {"howto": True}, text)
    ids = staff_equipment_ids(user)
    qs = _scope(UrgentBookingRequest.objects.filter(status="PENDING"), ids).select_related("equipment").order_by("requested_at")
    count = qs.count()
    if not count:
        return C.reply("No urgent requests are waiting on your equipment.", actions=[C.link("Urgent booking", "/urgent-requests")],
                       intent="staff_urgent")
    lines = [f"**{count} urgent request(s)** waiting:", ""] + [
        f"- {r.equipment.name} — {r.get_request_type_display()} (requested {_local(r.requested_at)})" for r in qs[:8]
    ]
    return C.reply("\n".join(lines), actions=[C.link("Review urgent requests", "/urgent-requests", primary=True)],
                   intent="staff_urgent", title_hint="Urgent requests")


_HANDLERS = {
    "help": help_reply,
    "bookings": bookings_list,
    "booking_details": booking_details,
    "cancel": _change("cancel"),
    "reschedule": _change("reschedule"),
    "edit": _change("edit"),
    "howto_edit": howto_edit,
    "message_lab": message_lab,
    "balance": wallet_balance,
    "recharge": recharge,
    "howto_wallet": howto_wallet,
    "transactions": transactions,
    "invoices": invoices,
    "waitlist": waitlist,
    "urgent": urgent,
    "templates": templates,
    "rate": rate,
    "results": results,
    "tickets": tickets,
    "ticket_create": ticket_create,
    "students": students,
    "my_research": my_research,
    "reports": reports,
    "charges_generic": charges_generic,
    "staff_today": staff_today,
    "staff_approvals": staff_approvals,
    "staff_waitlist": staff_waitlist,
    "staff_urgent": staff_urgent,
}

from iic_booking.research_copilot.services.assistant import answers as _answers  # noqa: E402

_HANDLERS.update(_answers.HANDLERS)

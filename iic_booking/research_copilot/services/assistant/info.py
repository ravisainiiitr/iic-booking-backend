"""
Equipment questions answered from portal data: overview, location, contacts, charges for the user's
category, instructions for the user's type, booking-form inputs and booking rules. Also "which
equipment can do X" and portal policy questions.

Every equipment lookup goes through the user's visible queryset; charges come from the portal charge
engine for the user's own category (internal rates stay hidden from viewers who may not see them).
"""

from __future__ import annotations

import re
from typing import Any

from django.utils.html import strip_tags

from iic_booking.research_copilot.services.assistant import cards as C
from iic_booking.research_copilot.services.assistant import matching
from iic_booking.users.display import get_user_display_name

TOPICS = ("overview", "location", "contacts", "charges", "instructions", "inputs", "rules")
TOPIC_LABELS = {
    "overview": "Overview",
    "location": "Location",
    "contacts": "Contacts",
    "charges": "Charges",
    "instructions": "Sample instructions",
    "inputs": "Booking inputs",
    "rules": "Booking rules",
}


def _clean(text: Any, limit: int = 700) -> str:
    plain = " ".join(strip_tags(str(text or "")).split())
    return plain if len(plain) <= limit else plain[: limit - 1].rsplit(" ", 1)[0] + "…"


def _user_type_label(user) -> str:
    getter = getattr(user, "get_user_type_display", None)
    try:
        return str(getter()) if callable(getter) else str(getattr(user, "user_type", "") or "")
    except Exception:  # noqa: BLE001
        return str(getattr(user, "user_type", "") or "")


def contacts(eq) -> list[dict[str, Any]]:
    from iic_booking.users.display import name_with_honorific

    out: list[dict[str, Any]] = []
    try:
        for m in eq.equipment_managers.select_related("manager").all()[:4]:
            u = m.manager
            out.append({
                "role": "Officer in Charge",
                "name": name_with_honorific(u, m.honorific, default=get_user_display_name(u)),
                "email": getattr(u, "email", "") or "",
                "phone": " / ".join(p for p in (str(getattr(u, "phone_number", "") or "").strip(), (m.alternate_phone_number or "").strip()) if p),
                "office": (m.office_address or "").strip(),
            })
    except Exception:  # noqa: BLE001
        pass
    try:
        for o in eq.equipment_operators.select_related("operator").order_by("role").all()[:4]:
            u = o.operator
            out.append({
                "role": o.get_role_display() if hasattr(o, "get_role_display") else "Lab operator",
                "name": name_with_honorific(u, o.honorific, default=get_user_display_name(u)),
                "email": getattr(u, "email", "") or "",
                "phone": " / ".join(p for p in (str(getattr(u, "phone_number", "") or "").strip(), (o.alternate_phone_number or "").strip()) if p),
                "office": (o.office_address or "").strip(),
            })
    except Exception:  # noqa: BLE001
        pass
    return out


def input_fields(user, eq) -> list[dict[str, Any]]:
    from iic_booking.research_copilot.services.v2.mutations.booking import _field_descriptor
    from iic_booking.equipment.equipment_group_service import _effective_input_fields

    try:
        fields = _effective_input_fields(eq, getattr(user, "user_type", "") or "")
    except Exception:  # noqa: BLE001
        return []
    out = []
    for f in fields:
        d = _field_descriptor(f)
        out.append({
            "key": d["key"],
            "label": d["label"],
            "type": d["type"],
            "required": d["required"],
            "options": [o["label"] for o in d["options"][:8]],
        })
    return out


def instructions(user, eq, limit: int = 900) -> str:
    from iic_booking.equipment.rich_text import resolve_important_instruction, rich_text_to_plain

    text = resolve_important_instruction(eq, str(getattr(user, "user_type", "") or ""))
    plain = rich_text_to_plain(text)
    return plain if len(plain) <= limit else plain[: limit - 1].rsplit(" ", 1)[0] + "…"


def rules(eq) -> list[str]:
    out = []
    minutes = int(getattr(eq, "slot_duration_minutes", 0) or 0)
    if minutes:
        out.append(f"Each slot is {minutes} minutes; longer runs book back-to-back slots.")
    hours = int(getattr(eq, "reschedule_hours_threshold", None) or 48)
    out.append(f"You can cancel or reschedule yourself up to {hours} hours before the slot starts; after that only an admin can.")
    lead = getattr(eq, "sample_submission_lead_hours", None)
    if lead:
        out.append(
            f"Submit your sample at least {int(lead)} hours before the slot starts "
            "(if that falls on a weekend or institute holiday, the previous working day)."
        )
    elif lead == 0:
        out.append("There is no sample submission deadline for this instrument.")
    collect = int(getattr(eq, "sample_collect_deadline_hours", 0) or 0)
    if collect:
        out.append(f"Collect your sample within {collect} hours after the booking is completed.")
    if int(getattr(eq, "waitlist_queue_depth", 0) or 0) > 0:
        out.append("A waitlist is available when the slots you want are full.")
    return out


def charges(user, eq) -> dict[str, Any]:
    from iic_booking.research_copilot.services.assistant.availability import estimate_one_sample

    est = estimate_one_sample(user, eq)
    return {**est, "user_category": _user_type_label(user), "basis": "1 sample with default inputs"}


def _money(v: Any) -> str:
    try:
        return f"₹{float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"


def equipment_info_reply(user, eq, topic: str = "overview") -> dict[str, Any]:
    topic = topic if topic in TOPICS else "overview"
    dept = getattr(eq, "internal_department", None)
    status_label = eq.get_status_display() if eq.status else "Unknown"
    bookable = (eq.status or "").strip() == "ACTIVE"
    card: dict[str, Any] = {
        "type": "ba_equipment_info",
        "focus": topic,
        "equipment_id": int(eq.pk),
        "name": eq.name,
        "code": eq.code or "",
        "department": getattr(dept, "name", "") or "",
        "category": getattr(getattr(eq, "category", None), "name", "") or "",
        "status_label": status_label,
        "bookable": bookable,
        "location": _clean(eq.location, 240),
        "office_address": _clean(getattr(eq, "office_address", ""), 240),
        "maps_url": getattr(eq, "google_maps_url", None) or None,
        "description": _clean(eq.description, 600),
        "make": _clean(getattr(eq, "make", ""), 120),
        "model": _clean(getattr(eq, "model_information", ""), 120),
        "href": f"/equipment/{eq.pk}",
    }
    lines: list[str] = []
    name = f"**{eq.name}**"
    if topic in ("overview", "contacts"):
        card["contacts"] = contacts(eq)
    if topic in ("overview", "charges"):
        card["charges"] = charges(user, eq)
    if topic in ("overview", "instructions"):
        card["instructions"] = instructions(user, eq)
    if topic in ("overview", "inputs"):
        card["inputs"] = input_fields(user, eq)
    if topic in ("overview", "rules"):
        card["rules"] = rules(eq)

    if topic == "location":
        if card["location"]:
            lines.append(f"{name} is at **{card['location']}**" + (f" ({card['department']})." if card["department"] else "."))
        else:
            lines.append(f"The portal has no location recorded for {name}." + (f" It belongs to {card['department']}." if card["department"] else ""))
        if card["office_address"]:
            lines.append(f"Enquiries office: {card['office_address']}.")
    elif topic == "contacts":
        people = card["contacts"]
        if people:
            lines.append(f"Contacts for {name}:")
            for p in people:
                bits = [p["name"]] + [b for b in (p["email"], p["phone"]) if b]
                lines.append(f"- {p['role']}: " + ", ".join(bits))
        else:
            lines.append(f"No Officer in Charge or operator is listed for {name} yet. Raise a support ticket and the IIC team will route it.")
    elif topic == "charges":
        ch = card["charges"]
        if ch.get("charge") is not None:
            text = f"For your category (**{ch['user_category']}**), 1 sample on {name} with default inputs is estimated at **{_money(ch['charge'])}**"
            if ch.get("gst_amount"):
                text += f" + GST {ch['gst_percent']:g}% = **{_money(ch['total'])}**"
            lines.append(text + ".")
            if (ch.get("slots_needed") or 1) > 1:
                lines.append(f"That takes about {ch['slots_needed']} back-to-back slots.")
            lines.append("The booking page recalculates the exact charge from your actual inputs before you book.")
        else:
            lines.append(f"There is no active charge profile for your category on {name}. Open the booking page for the authoritative charge.")
    elif topic == "instructions":
        lines.append(card["instructions"] and f"Instructions for {name}:\n\n{card['instructions']}" or
                     f"{name} has no special sample instructions on the portal. Check the equipment page or ask the operator.")
    elif topic == "inputs":
        fields = card["inputs"]
        if fields:
            lines.append(f"Booking {name} asks for:")
            for f in fields:
                opt = f" ({', '.join(f['options'][:5])})" if f["options"] else ""
                lines.append(f"- {f['label']}{' (required)' if f['required'] else ''}{opt}")
        else:
            lines.append(f"Booking {name} only needs the slot and the number of samples.")
    elif topic == "rules":
        lines.append(f"Booking rules for {name}:")
        lines.extend(f"- {r}" for r in card["rules"])
    else:
        summary = f"{name}"
        if card["code"]:
            summary += f" ({card['code']})"
        summary += f" — {status_label}"
        if card["department"]:
            summary += f", {card['department']}"
        lines.append(summary + ".")
        if card["description"]:
            lines.append(card["description"])
        if card["location"]:
            lines.append(f"Location: {card['location']}.")

    actions = []
    if bookable:
        actions.append(C.assistant_action(
            "Check availability", "ba_availability", {"equipment_id": int(eq.pk), "when": None}, primary=True,
            utterance=f"Check {eq.name} availability",
        ))
    for t in ("charges", "contacts", "rules", "instructions"):
        if t != topic and len(actions) < 4:
            actions.append(C.assistant_action(TOPIC_LABELS[t], "ba_info", {"equipment_id": int(eq.pk), "topic": t},
                                              utterance=f"{TOPIC_LABELS[t]} for {eq.name}"))
    actions.append(C.link("Equipment page", card["href"]))
    return C.reply(
        "\n\n".join(lines) if topic != "contacts" else "\n".join(lines),
        cards=[card],
        actions=actions,
        intent=f"info_{topic}",
        title_hint=f"{eq.name} {TOPIC_LABELS[topic].lower()}" if topic != "overview" else eq.name,
        extra={"equipment_id": int(eq.pk)},
    )


def capability_reply(user, text: str) -> dict[str, Any] | None:
    cands, labels = matching.capability_search(user, text)
    if not cands:
        return None
    rows = [matching.option_row(c) for c in cands]
    what = labels[0] if labels else "this"
    return C.reply(
        f"These instruments on the portal can help with {what}. Pick one to see details, charges or free slots.",
        cards=[C.equipment_options_card(rows, title="Matching equipment", intent="info", query=text)],
        intent="capability",
        title_hint=f"Equipment for {labels[0].split(' (')[0]}" if labels else "Find equipment",
    )


# ------------------------------------------------------------------------------------------- policy

POLICY_TOPICS = ("cancel", "refund", "edit", "recharge", "sample_submission", "booking_rules", "waitlist")
_POLICY_LINKS = {
    "cancel": ("My Bookings", "/my-bookings"),
    "refund": ("My Bookings", "/my-bookings"),
    "edit": ("My Bookings", "/my-bookings"),
    "recharge": ("Open Wallet", "/wallet"),
    "sample_submission": ("My Bookings", "/my-bookings"),
    "booking_rules": ("Book equipment", "/book-equipment"),
    "waitlist": ("Book equipment", "/book-equipment"),
}


def _builtin_policy(topic: str, eq=None) -> list[str]:
    hours = int(getattr(eq, "reschedule_hours_threshold", None) or 48) if eq is not None else None
    cutoff = f"{hours} hours" if hours else "the instrument's cutoff (48 hours unless the lab set another value)"
    if topic in ("cancel", "edit"):
        lines = [
            f"You can cancel or reschedule a booking yourself from **My Bookings** until {cutoff} before the slot starts.",
            "Inside that window only an admin can change it; raise a support ticket if you need help.",
            "You can also release part of a multi-slot booking (partial cancellation) or move it to another free slot (reschedule).",
            "I can start a cancellation or reschedule for you here: just say \"cancel my next booking\" or \"reschedule my next booking\".",
        ]
        if topic == "edit":
            lines.append(
                "To change the booking inputs, open the booking and choose **Edit User Inputs** (until the booking is "
                "completed). If the charge goes up, pay the difference within 1 minute or the edit is cancelled. If the "
                "new charge is lower, the difference is refunded to your wallet straight away when you edit before "
                f"the cancellation deadline ({cutoff} before the slot starts); after that deadline the refund needs "
                "the Officer In Charge's approval."
            )
        return lines
    if topic == "refund":
        return [
            "Refunds are calculated by the portal when you cancel, under the cancellation policy, and the amount is shown "
            "before you confirm the cancellation.",
            f"Self-service cancellation is open until {cutoff} before the slot starts.",
        ]
    if topic == "recharge":
        return [
            "Open **Wallet** and click **Recharge Wallet**. Faculty can choose **Project Grant** (approved by the SRIC "
            "Office) or **Direct Cash Deposit / Bank Transfer** (deposit at the SRIC Bill Section and share the "
            "transaction number); confirm with the OTP sent to your email. The minimum is ₹100.",
            "Students book from their supervisor's wallet once the supervisor approves the wallet join request.",
            "If you need credit instead, click **Credit Facility** on the Wallet page and submit a credit request "
            "(Main Administrator approval). Faculty can also use **Transfer** to move balance to another user under "
            "the same department grant.",
        ]
    if topic == "sample_submission":
        lead = getattr(eq, "sample_submission_lead_hours", None) if eq is not None else None
        if eq is not None and lead == 0:
            return [f"**{eq.name}** has no sample submission deadline."]
        when = f"{int(lead)} hours" if lead else "the instrument's lead time (24 hours unless the lab set another value)"
        return [
            f"Submit your sample at least {when} before your slot starts.",
            "If that deadline falls on a weekend or institute holiday, it moves to the previous working day at the same time.",
            "My Bookings shows the countdown and the exact deadline for each booking.",
        ]
    if topic == "waitlist":
        return [
            "When the slots you want are full, the booking page can add you to the instrument's waitlist (if the lab enabled one).",
            "You are notified of your position and when a slot frees up.",
        ]
    return [
        "Each account type has its own booking window (how far ahead you can book); the booking page only shows slots inside it.",
        "Weekends, institute holidays and maintenance days are blocked automatically.",
        f"Cancellation/reschedule is self-service until {cutoff} before the slot.",
        "Your wallet (or your supervisor's wallet) is charged when the booking is created; spending limits and quotas are checked at that moment.",
    ]


def policy_reply(user, topic: str, text: str, eq=None) -> dict[str, Any]:
    from iic_booking.research_copilot.services.intelligence import knowledge

    lines: list[str] = []
    source = "Portal rules"
    try:
        confidence, hits = knowledge.best(text=text, user=user)
    except Exception:  # noqa: BLE001
        confidence, hits = knowledge.NO_VERIFIED_ANSWER, []
    if hits and confidence == knowledge.HIGH:
        art = hits[0].article
        lines.append(_clean(art.answer, 1200))
        source = "Verified IIC answer"
        try:
            knowledge.record_usage(art)
        except Exception:  # noqa: BLE001
            pass
    else:
        lines.extend(_builtin_policy(topic, eq))
    label, href = _POLICY_LINKS.get(topic, ("Book equipment", "/book-equipment"))
    reply_ = C.reply(
        "\n".join(f"- {l}" if not l.startswith("- ") and len(lines) > 1 else l for l in lines),
        actions=[C.link(label, href)],
        intent=f"policy_{topic}",
        kind="ANSWER",
        title_hint={"recharge": "Wallet recharge", "sample_submission": "Sample submission"}.get(topic, f"{topic.replace('_', ' ').capitalize()} policy"),
    )
    reply_["metadata"]["source_label"] = source
    return reply_


def detect_policy_topic(lower: str) -> str | None:
    if re.search(r"\b(recharge|top ?up|add (money|funds|balance)|load (money|wallet))\b", lower):
        return "recharge"
    if re.search(r"\brefund", lower):
        return "refund"
    if re.search(r"\b(sample)s?\b.*\b(submi\w*|deliver\w*|drop|hand ?over|bring)\b|\b(submi\w*|deliver\w*|drop)\b.*\bsamples?\b", lower):
        return "sample_submission"
    if re.search(r"\bwait ?list", lower):
        return "waitlist"
    if re.search(r"\b(cancel\w*|reschedul\w*)\b", lower):
        return "cancel"
    if re.search(r"\b(edit|modify|change|update)\b.*\bbooking", lower):
        return "edit"
    if re.search(r"\b(booking (rules|policy|window|limits?)|how (far|early) (in advance|ahead)|advance booking|rules for booking)\b", lower):
        return "booking_rules"
    return None

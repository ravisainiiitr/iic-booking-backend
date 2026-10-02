"""
"Need help?" after a failed booking or charge calculation on the booking page.

The page posts a `ba_help` action with the failure code (and the equipment, the error text the user saw,
missing field labels, the date). The reply is deterministic: free slots and the waitlist when a slot was
taken, used / remaining quota and the reset date when a quota blocked it, wallet-linking steps when there
is no wallet, and what to fix when the charge could not be calculated. Nothing is booked or changed.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from django.utils import timezone

from iic_booking.research_copilot.services.assistant import cards as C

CODES = ("slot_taken", "no_slots", "quota_exceeded", "no_wallet", "charge_error", "booking_failed")

_QUOTA_MIN_RE = re.compile(
    r"(?P<scope>[A-Za-z ]+?)\s+(?P<period>weekly|monthly)\s+quota exceeded:\s*current usage (?P<used>\d+) min"
    r"\s*\+\s*requested (?P<req>\d+) min.*?limit (?P<limit>\d+) min.*?remaining before this request (?P<left>\d+) min",
    re.IGNORECASE | re.DOTALL,
)
_QUOTA_COUNT_RE = re.compile(r"(?P<period>weekly|monthly) booking-count quota exceeded:\s*(?P<total>\d+) bookings vs limit (?P<limit>\d+)", re.I)
_QUOTA_CHARGE_RE = re.compile(r"(?P<period>weekly|monthly) charge quota exceeded:\s*₹?(?P<total>[\d.,]+) vs limit ₹?(?P<limit>[\d.,]+)", re.I)


def classify(message: str) -> str:
    """Failure code for an error message from the booking API (mirrors the frontend classifier)."""
    m = (message or "").lower()
    if "quota" in m and ("exceed" in m or "limit" in m):
        return "quota_exceeded"
    if "wallet" in m and ("access" in m or "don't have" in m or "do not have" in m or "no wallet" in m or "not linked" in m):
        return "no_wallet"
    if "no longer available" in m or "already booked" in m or "slot is taken" in m or ("not available" in m and "slot" in m):
        return "slot_taken"
    if "no slots" in m or "no available slots" in m or "fully booked" in m:
        return "no_slots"
    if "charge" in m and ("calculat" in m or "estimate" in m):
        return "charge_error"
    return "booking_failed"


def _hours(minutes: int) -> str:
    h, mm = divmod(max(0, int(minutes)), 60)
    if h and mm:
        return f"{h} h {mm} min"
    return f"{h} h" if h else f"{mm} min"


def _ref_date(payload: dict[str, Any]) -> date:
    today = timezone.localdate()
    try:
        d = date.fromisoformat(str(payload.get("date") or ""))
    except ValueError:
        return today
    return d if d >= today else today


def reset_date(period: str, ref: date) -> date:
    if period == "monthly":
        return date(ref.year + (ref.month == 12), 1 if ref.month == 12 else ref.month + 1, 1)
    return ref + timedelta(days=7 - ref.weekday())


def _base_actions(eq) -> list[dict[str, Any]]:
    from iic_booking.research_copilot.services.intelligence import messages as M

    out = []
    if eq is not None:
        from iic_booking.research_copilot.services.assistant.availability import booking_href

        out.append(C.link("Back to booking form", booking_href(eq.pk)))
    out.append(M.ticket_action("user_requested", "Raise a support ticket"))
    return out


def _slots(user, conversation, eq, payload: dict[str, Any], code: str) -> dict[str, Any]:
    from iic_booking.research_copilot.services.assistant import availability
    from iic_booking.research_copilot.services.assistant import state as ba_state
    from iic_booking.research_copilot.services.assistant.dates import when_from_payload

    start = _ref_date(payload)
    when = when_from_payload({"start": start.isoformat(), "end": (start + timedelta(days=6)).isoformat()})
    out = availability.availability_reply(user, eq, when, booking_intent=True)
    ba_state.remember_equipment(conversation, eq, when, "availability")
    lead = {
        "slot_taken": f"Someone else booked that **{eq.name}** slot just before you. Here are the next free times — pick one to book it.",
        "no_slots": f"There were no free **{eq.name}** slots on that day. Here are the next free times.",
    }.get(code, f"That **{eq.name}** booking didn't go through. Here are the next free times.")
    out["content"] = f"{lead}\n\n{out.get('content') or ''}".strip()
    actions = list(out.get("suggested_actions") or [])
    if int(getattr(eq, "waitlist_queue_depth", 0) or 0) > 0:
        actions.insert(0, C.link("Join the waitlist", availability.booking_href(eq.pk, start), primary=False))
    out["suggested_actions"] = actions[:4]
    return out


def _quota(user, conversation, eq, payload: dict[str, Any]) -> dict[str, Any]:
    from iic_booking.research_copilot.services.assistant.availability import _next_week

    message = str(payload.get("message") or "")
    ref = _ref_date(payload)
    lines: list[str] = []
    period = "weekly"
    m = _QUOTA_MIN_RE.search(message)
    if m:
        period = m.group("period").lower()
        scope = m.group("scope").strip().lower()
        used, req, limit, left = (int(m.group(k)) for k in ("used", "req", "limit", "left"))
        whose = {
            "faculty": "everyone booking from your supervisor's wallet",
            "external": "external users on this instrument",
        }.get(scope.split()[-1] if scope else "", "your account category")
        lines.append(f"This booking needs **{_hours(req)}**, but only **{_hours(left)}** of the {period} quota for "
                     f"{whose} is left (**{_hours(used)}** of **{_hours(limit)}** already used).")
    elif (c := _QUOTA_COUNT_RE.search(message)):
        period = c.group("period").lower()
        lines.append(f"The {period} limit is **{c.group('limit')} bookings** on this instrument and this one would make "
                     f"{c.group('total')}.")
    elif (c := _QUOTA_CHARGE_RE.search(message)):
        period = c.group("period").lower()
        lines.append(f"The {period} spending quota is **₹{c.group('limit')}** and this booking would take it to ₹{c.group('total')}.")
    else:
        if "month" in message.lower():
            period = "monthly"
        lines.append(f"This booking would go over the {period} booking quota for {eq.name if eq is not None else 'this instrument'}.")
    reset = reset_date(period, ref)
    lines.append(f"The quota resets on **{reset:%A %d %b %Y}** ({'every Monday' if period == 'weekly' else 'on the 1st of each month'}; "
                 "a booking counts in the week or month of its slot).")
    lines += [
        "",
        "**What you can do**",
        "- Book fewer slots now, or pick slots after the reset date.",
        "- Cancel a booking you no longer need to free up quota.",
        "- For genuinely urgent work, submit an urgent request (reviewed by the lab).",
    ]
    actions: list[dict[str, Any]] = []
    if eq is not None:
        actions.append(C.assistant_action(
            "Free slots after the reset", "ba_availability",
            {"equipment_id": int(eq.pk), "when": {"start": reset.isoformat(), "end": (reset + timedelta(days=6)).isoformat()}}
            if (reset - timezone.localdate()).days <= 13 else {"equipment_id": int(eq.pk), "when": _next_week(timezone.localdate())},
            primary=True, utterance=f"Free {eq.name} slots after the quota resets",
        ))
    actions += [C.prompt_action("My upcoming bookings", "Show my upcoming bookings"),
                C.prompt_action("Urgent booking", "How do I make an urgent booking request?")]
    return C.reply("\n".join(lines), actions=actions[:4], intent="help_quota_exceeded", kind="ANSWER", title_hint="Booking quota")


def _no_wallet(user, conversation, eq, payload: dict[str, Any]) -> dict[str, Any]:
    from iic_booking.research_copilot.services.assistant import daily

    t = daily.user_type(user)
    if t in daily.STUDENT_TYPES:
        from iic_booking.users.models.wallet import WalletJoinRequest

        req = WalletJoinRequest.objects.filter(student=user).select_related("faculty").order_by("-created_at").first()
        faculty = getattr(getattr(req, "faculty", None), "name", "") or "your supervisor"
        if req is not None and req.status == "PENDING":
            head = (f"Your request to join **{faculty}**'s wallet is still waiting for approval, so you can't book yet. "
                    "Ask them to approve it from **Student management** on their dashboard.")
        elif req is not None and req.status == "APPROVED":
            head = ("You are linked to a supervisor's wallet, but the portal couldn't use it for this booking. "
                    "Open Wallet to check it, or raise a support ticket.")
        else:
            head = "Bookings are paid from your supervisor's wallet, and your account isn't linked to one yet."
        lines = [
            head, "",
            "**Link your supervisor's wallet**",
            "1. Open **Wallet** → **Request to Join Wallet**.",
            "2. Search for your supervisor and press **Send Request**.",
            "3. When they approve it, come back and book — your booking is charged to their wallet.",
            "",
            "Can't find your supervisor? Faculty appear in the list only after they have signed in to the portal once "
            "(via Channel I), so ask them to sign in, then search again.",
        ]
    elif t == "faculty":
        lines = ["Your wallet isn't set up yet. Open **Wallet** once to create it, then recharge it to book.",
                 "", daily.recharge_steps_text(user)]
    else:
        lines = ["Your account has no wallet the portal can charge for this booking. Open **Wallet** to check it; "
                 "external users can also pay as shown on the booking page."]
    actions = [C.link("Link my supervisor's wallet" if t in daily.STUDENT_TYPES else "Open Wallet", "/wallet", primary=True),
               C.prompt_action("Wallet balance", "What is my wallet balance?")] + _base_actions(eq)
    return C.reply("\n".join(lines), actions=actions[:4], intent="help_no_wallet", kind="ANSWER", title_hint="Wallet needed to book")


def _charge_error(user, conversation, eq, payload: dict[str, Any]) -> dict[str, Any]:
    from iic_booking.research_copilot.services.assistant import info

    missing = [str(x)[:80] for x in (payload.get("missing_fields") or [])][:12]
    lines = ["The booking page couldn't calculate the charge, so the booking can't be submitted yet."]
    if missing:
        lines += ["", "**Fill in these fields first:** " + ", ".join(missing) + "."]
    no_profile = False
    if eq is not None:
        est = info.charges(user, eq)
        if est.get("charge") is None:
            no_profile = True
            lines += ["", f"There is no active charge profile for your category (**{est.get('user_category') or 'your account type'}**) "
                          f"on **{eq.name}**, so the portal can't price it for your account. Contact the Officer in Charge."]
        else:
            required = [f["label"] for f in info.input_fields(user, eq) if f["required"]][:8]
            if required and not missing:
                lines += ["", f"**{eq.name}** needs: " + ", ".join(required) + "."]
    if not no_profile:
        lines += [
            "",
            "**Common causes**",
            "- A required booking input is empty, or a number is outside the allowed range (for example more samples "
            "than the maximum).",
            "- An option was changed after the slots were picked — re-select the slots and try again.",
            "- A brief connection problem — wait a moment and press **Calculate** again.",
        ]
    actions = []
    if eq is not None:
        actions += [
            C.assistant_action("Required inputs", "ba_info", {"equipment_id": int(eq.pk), "topic": "inputs"}, utterance=f"Booking inputs for {eq.name}"),
            C.assistant_action("Contacts", "ba_info", {"equipment_id": int(eq.pk), "topic": "contacts"}, utterance=f"Contacts for {eq.name}"),
        ]
    actions += _base_actions(eq)
    return C.reply("\n".join(lines), actions=actions[:4], intent="help_charge_error", kind="ANSWER", title_hint="Charge calculation")


def _failed(user, conversation, eq, payload: dict[str, Any]) -> dict[str, Any]:
    message = " ".join(str(payload.get("message") or "").split())[:300]
    lines = ["That booking didn't go through."]
    if message:
        lines.append(f"The portal said: \"{message}\"")
    lines += ["", "Try again from the booking form; if it keeps failing, raise a support ticket and the IIC team will check it."]
    actions = []
    if eq is not None:
        from iic_booking.research_copilot.services.assistant.answers import _eq_chips

        actions += _eq_chips(eq, skip=("book", "charges"))
    actions += _base_actions(eq)
    return C.reply("\n".join(lines), actions=actions[:4], intent="help_booking_failed", kind="ANSWER", title_hint="Booking problem")


def reply(user, conversation, payload: dict[str, Any]) -> dict[str, Any]:
    from iic_booking.research_copilot.services.assistant.engine import _visible

    code = payload.get("code") or "booking_failed"
    if code == "booking_failed" and payload.get("message"):
        code = classify(str(payload["message"]))
    eq = _visible(user, payload["equipment_id"]) if payload.get("equipment_id") else None
    if code in ("slot_taken", "no_slots") and eq is not None:
        out = _slots(user, conversation, eq, payload, code)
    elif code == "quota_exceeded":
        out = _quota(user, conversation, eq, payload)
    elif code == "no_wallet":
        out = _no_wallet(user, conversation, eq, payload)
    elif code == "charge_error":
        out = _charge_error(user, conversation, eq, payload)
    else:
        out = _failed(user, conversation, eq, payload)
    meta = out.setdefault("metadata", {})
    meta["failure_context"] = {"code": code, "equipment_id": int(eq.pk) if eq is not None else None}
    meta["intent"] = f"assistant:help_{code}"
    return out

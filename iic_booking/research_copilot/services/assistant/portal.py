"""Portal actions in the IITR Assistant: "enable slot exchange", "where do I block slots", "approve recharges".

Uses the same catalogue as the header search (``equipment.portal_actions``). An action about one instrument asks
which one (buttons for the user's own instruments, or type the name); switches are changed only after the user
presses the button, through the equipment form's serializer. Booking, availability, charges and equipment details
stay with the assistant's own answers.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from iic_booking.equipment import portal_actions as P
from iic_booking.research_copilot.services.assistant import cards as C
from iic_booking.research_copilot.services.assistant import state as ba_state
from iic_booking.research_copilot.services.assistant.actions import PORTAL

# Answered better by the assistant itself (live slots, prices, the guided booking flow).
NATIVE = frozenset({"book_equipment", "equipment_details", "availability", "find_instrument", "analysis_charges",
                    "booking_calendar"})
STRONG = 14  # a whole keyword phrase, not just a shared word
CHOICES = 8


def _label(action: P.PortalAction) -> str:
    return action.title[:1].lower() + action.title[1:]


def _state_word(on: bool) -> str:
    return "on" if on else "off"


def picker_card(action: P.PortalAction) -> dict[str, Any]:
    """A search box over the instruments the user may choose for the action (``/api/portal-actions/<key>/equipment/``)."""
    return {"type": "ba_equipment_picker", "action": action.key, "title": "Search for the instrument",
            "placeholder": "Name, code or model"}


def _choose(user, conversation, action: P.PortalAction, text: str, on: bool | None, *, intro: str = "",
            extra: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    rows = P.equipment_choices(user, action, "", limit=CHOICES)
    if not rows:
        return C.reply(f"You have no instruments to {_label(action)} for.", intent="portal:none")
    ba_state.save(conversation, portal_pending=action.key, portal_on=-1 if on is None else int(on))
    buttons = []
    for r in rows:
        state = f" · {_state_word(r['on'])}" if "on" in r else ""
        buttons.append(C.assistant_action(f"{r['name']}{state}", PORTAL, {"action": action.key, "equipment_id": r["id"]}))
    question = intro or f"**{action.title}** — which instrument?"
    more = " Pick one below, or search for another." if len(rows) >= CHOICES else ""
    return C.reply(f"{question}{more}", cards=[picker_card(action)], actions=[*buttons, *(extra or [])],
                   intent=f"portal:{action.key}", title_hint=action.title)


def settings_link(user, eq) -> dict[str, Any] | None:
    """The instrument's own settings form, for the Main Administrator (the only role that edits it there)."""
    return C.link("Open equipment settings", f"/admin/section/equipment?edit={eq.pk}") if P.ADMIN in P.roles_of(user) else None


def _for_equipment(user, action: P.PortalAction, equipment_id: int, on: bool | None) -> dict[str, Any]:
    eq = P.equipment_scope(user, action).filter(pk=equipment_id).first()
    if eq is None:
        return C.reply("That instrument is not one you can change.", intent="portal:gone")
    if not action.switch:
        return C.reply(f"Here is **{action.title}** for **{eq.name}**.",
                       actions=[C.link(f"Open {action.title.lower()}", action.target(eq.pk), primary=True)],
                       intent=f"portal:{action.key}", title_hint=action.title)
    current = bool(getattr(eq, action.switch))
    want = (not current) if on is None or on == current else on
    note = f"{action.title.split(' on or off')[0].replace('Turn ', '')} is **{_state_word(current)}** for **{eq.name}**."
    if on is not None and on == current:
        note += " Nothing to change."
    verb = "Turn on" if want else "Turn off"
    settings = settings_link(user, eq)
    return C.reply(
        f"{note} {action.hint}. Nothing changes until you press the button.",
        actions=[C.assistant_action(f"{verb} for {eq.code or eq.name}", PORTAL,
                                    {"action": action.key, "equipment_id": eq.pk, "on": int(want)}, primary=True),
                 *([settings] if settings else [])],
        intent=f"portal:{action.key}", title_hint=action.title,
    )


def _run(user, action: P.PortalAction, equipment_id: int, on: bool) -> dict[str, Any]:
    try:
        done = P.run_switch(SimpleNamespace(user=user), action, equipment_id, on)
    except P.ActionRefused as exc:
        return C.reply(exc.message, intent=f"portal:{action.key}:refused")
    name = action.title.split(" on or off")[0].replace("Turn ", "")
    return C.reply(f"Done — {name} is now **{_state_word(done['on'])}** for **{done['name']}**.",
                   actions=[C.assistant_action(f"Turn it {_state_word(not done['on'])} again", PORTAL,
                                               {"action": action.key, "equipment_id": done["id"], "on": int(not done["on"])})],
                   intent=f"portal:{action.key}:done", title_hint=action.title)


def button(user, conversation, payload: dict[str, Any]) -> dict[str, Any]:
    action = P.BY_KEY[payload["action"]]
    if not (action.roles & P.roles_of(user)):
        return C.reply("That option is not available to you.", intent="portal:refused")
    ba_state.save(conversation, portal_pending="")
    if not action.equipment:
        return C.reply(f"Here is **{action.title}**.", actions=[C.link(f"Open {action.title.lower()}", action.path, primary=True)],
                       intent=f"portal:{action.key}")
    if payload.get("equipment_id") is None:
        return _choose(user, conversation, action, "", None)
    if "on" in payload and action.switch:
        return _run(user, action, payload["equipment_id"], bool(payload["on"]))
    st = ba_state.load(conversation)
    on = st.get("portal_on")
    return _for_equipment(user, action, payload["equipment_id"], None if on in (None, -1) else bool(on))


def _subject(action: P.PortalAction) -> str:
    """"Turn Slot Exchange on or off" -> "the Slot Exchange setting"; other titles as they are, lower-cased."""
    if action.switch:
        return f"the {action.title.replace('Turn ', '').replace(' on or off', '')} setting"
    return _label(action)


def clarify(user, conversation, text: str) -> dict[str, Any] | None:
    """A short query that names no action clearly ("exchange", "flash", "amc"): offer what it may mean for this user.

    One likely action about an instrument: say so and ask which instrument (buttons and a search box). Several: ask
    which, as buttons. Always also offer an equipment search for the same words. Nothing is changed here."""
    words = P._words(text)
    if not words or len(words) > 4:
        return None
    options = [a for a in P.loose_match(user, text) if a.key not in NATIVE]
    if not options:
        return None
    q = " ".join(words)
    find = C.prompt_action(f"Find equipment for “{q}”", f"equipment for {q}")
    first = options[0]
    if len(options) == 1 and first.equipment:
        verb = "change" if first.switch else "open"
        return _choose(user, conversation, first, text, None,
                       intro=f"I can help you {verb} **{_subject(first)}**. Which instrument do you want to configure?",
                       extra=[find])
    if len(options) == 1:
        return C.reply(f"I can help with **{first.title}** — {first.hint.lower()}.",
                       actions=[C.link(f"Open {first.title.lower()}", first.path, primary=True), find],
                       intent=f"portal:{first.key}", title_hint=first.title)
    buttons = [C.assistant_action(a.title, PORTAL, {"action": a.key}) for a in options]
    return C.reply(f"I can help with “{q}” in a few ways. Which do you mean?", actions=[*buttons, find],
                   intent="portal:clarify")


def turn(user, conversation, text: str) -> dict[str, Any] | None:
    """A typed message: an equipment name after "which instrument?", or a request that matches the catalogue."""
    st = ba_state.load(conversation)
    pending = P.BY_KEY.get(st.get("portal_pending") or "")
    if pending is not None and pending.roles & P.roles_of(user):
        ba_state.save(conversation, portal_pending="")
        named = P.equipment_named_in(user, pending, text)
        if named is None:
            rows = P.equipment_choices(user, pending, text, limit=2)
            named = {"equipment_id": rows[0]["id"]} if len(rows) == 1 else None
        if named is not None:
            on = st.get("portal_on")
            return _for_equipment(user, pending, named["equipment_id"], None if on in (None, -1) else bool(on))
    hits = [a for a in P.match(user, text, min_score=STRONG) if a.key not in NATIVE]
    if not hits:
        return None
    action = hits[0]
    if not action.equipment:
        others = [C.assistant_action(a.title, PORTAL, {"action": a.key}) for a in hits[1:4]]
        return C.reply(f"**{action.title}** — {action.hint.lower()}.",
                       actions=[C.link(f"Open {action.title.lower()}", action.path, primary=True), *others],
                       intent=f"portal:{action.key}", title_hint=action.title)
    on = P.desired_state(text) if action.switch else None
    named = P.equipment_named_in(user, action, text)
    if named is not None:
        return _for_equipment(user, action, named["equipment_id"], on)
    return _choose(user, conversation, action, text, on)

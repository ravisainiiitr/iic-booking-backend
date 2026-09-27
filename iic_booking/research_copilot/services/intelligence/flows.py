"""
Equipment, availability, cost and booking workflows.

All data comes from existing portal services:
  equipment        -> portal visibility queryset (equipment.search / get_visible)
  slots            -> slot_availability.find_bookable_slots (booking-page rules)
  inputs           -> booking form schema (_effective_input_fields)
  cost             -> tools._estimate_booking_cost (ChargeCalculationEngine), GST as in the portal
  booking proposal -> mutations.booking.prepare_booking_create (cache-only proposal; executes only via
                      the explicit confirmation endpoint, which revalidates the slots)
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from django.utils import timezone

from iic_booking.research_copilot.services.intelligence import equipment as eqsvc
from iic_booking.research_copilot.services.intelligence import messages as M
from iic_booking.research_copilot.services.intelligence import state as st
from iic_booking.research_copilot.services.intelligence import terminology
from iic_booking.research_copilot.services.intelligence.entities import Entities

PAGE = 6
MAX_SLOT_CHOICES = 6
MAX_COMPARE = 4
SAMPLE_QUICK_REPLIES = (1, 2, 3, 4, 5)
_CONTEXT_RE = re.compile(r"\b(it|this|that|same|this one|that one|this equipment|this instrument)\b")


@dataclass
class Turn:
    user: Any
    text: str
    conversation: Any
    state: dict[str, Any]
    ents: Entities
    intent: str = ""
    confidence: str = ""

    def respond(self, **kwargs) -> dict[str, Any]:
        kwargs.setdefault("intent", self.intent)
        kwargs.setdefault("confidence", self.confidence)
        return M.envelope(**kwargs)


# --------------------------------------------------------------------------- helpers


def _fields_for(user, eq) -> list:
    from iic_booking.equipment.equipment_group_service import _effective_input_fields

    try:
        return list(_effective_input_fields(eq, getattr(user, "user_type", "") or ""))
    except Exception:  # noqa: BLE001
        return []


def _field_a_label(user, eq) -> str | None:
    """Label of numeric field A (sample count) or None when the form has no numeric A."""
    for f in _fields_for(user, eq):
        if f.field_key == "A":
            return (f.field_label or "Number of samples") if str(f.field_type) == "NUMERIC" else None
    return "Number of samples"


def gst_percent_for(user) -> Decimal:
    from iic_booking.users.models.user_type import UserType

    if not UserType.is_external_user(str(getattr(user, "user_type", "") or "")):
        return Decimal("0")
    try:
        from iic_booking.equipment.api_views import get_external_gst_percent

        return Decimal(str(get_external_gst_percent() or 0))
    except Exception:  # noqa: BLE001
        return Decimal("18")


def charge_breakdown(user, amount: Any) -> dict[str, Any]:
    """Charge, GST (external users only, as the portal applies it) and total."""
    if amount is None:
        return {"charge": None, "gst_percent": None, "gst_amount": None, "total": None}
    charge = Decimal(str(amount)).quantize(Decimal("0.01"))
    pct = gst_percent_for(user)
    gst = (charge * pct / Decimal("100")).quantize(Decimal("0.01")) if pct > 0 else Decimal("0.00")
    return {
        "charge": float(charge),
        "gst_percent": float(pct) if pct > 0 else 0.0,
        "gst_amount": float(gst),
        "total": float(charge + gst),
    }


def estimate(user, eq, samples: int, provided: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Portal charge-engine estimate for these inputs (same inputs the booking would use)."""
    from iic_booking.research_copilot.services import tools as tools_svc
    from iic_booking.research_copilot.services.v2.mutations import booking as booking_mut

    try:
        values, _missing = booking_mut._copilot_input_values(
            user=user, equipment=eq, samples=samples, provided=provided, validate=False
        )
    except Exception:  # noqa: BLE001
        values = {"A": str(samples)}
    args: dict[str, Any] = {"equipment_id": int(eq.pk)}
    args.update({k: v for k, v in (values or {}).items() if len(k) == 1 and k in "ABCDEFG"})
    for attempt in (args, {"equipment_id": int(eq.pk), "A": samples}):
        try:
            res = tools_svc._estimate_booking_cost(arguments=attempt, user=user)
        except Exception:  # noqa: BLE001
            continue
        if res.get("ok"):
            return res.get("data") or {}
    return None


def required_slot_count(eq, total_time_minutes: Any) -> int:
    minutes = int(getattr(eq, "slot_duration_minutes", 0) or 0)
    try:
        total = float(total_time_minutes or 0)
    except (TypeError, ValueError):
        total = 0.0
    if minutes <= 0 or total <= 0:
        return 1
    return max(1, math.ceil(total / minutes))


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return timezone.localtime(dt) if timezone.is_aware(dt) else dt


def _period_ok(row: dict[str, Any], period: str | None) -> bool:
    if not period:
        return True
    start = _dt(row.get("start"))
    if start is None:
        return True
    hour = start.hour
    return {"morning": hour < 12, "afternoon": 12 <= hour < 17, "evening": hour >= 17}.get(period, True)


def contiguous_runs(rows: list[dict[str, Any]], count: int) -> list[list[dict[str, Any]]]:
    """Every run of `count` back-to-back slots (same day, end == next start)."""
    ordered = sorted(rows, key=lambda r: r.get("start") or "")
    runs = []
    for i in range(len(ordered)):
        run = ordered[i : i + count]
        if len(run) < count:
            break
        if all(a.get("end") and a["end"] == b.get("start") and a.get("date") == b.get("date") for a, b in zip(run, run[1:])):
            runs.append(run)
    return runs


def run_label(run: list[dict[str, Any]]) -> str:
    s, e = _dt(run[0].get("start")), _dt(run[-1].get("end"))
    if s is None:
        return str(run[0].get("date") or "")
    label = f"{s:%a %d %b}, {s:%H:%M}"
    if e is not None:
        label += f"-{e:%H:%M}"
    return label


def run_value(run: list[dict[str, Any]]) -> str:
    return ",".join(str(r["slot_id"]) for r in run)


def _ids(value: str) -> list[int]:
    out = []
    for part in str(value or "").split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out


def lookup_slots(user, eq, *, text: str, period: str | None, limit: int = 200):
    from iic_booking.research_copilot.services.v2.datetime_resolver import resolve_date_window
    from iic_booking.research_copilot.services.v2.slot_availability import find_bookable_slots

    window = resolve_date_window(text or "")
    lookup = find_bookable_slots(
        user=user,
        equipment_id=int(eq.pk),
        start_date=window.start_date,
        end_date=window.end_date,
        after_time=window.after_time,
        limit=limit,
    )
    rows = [r for r in (lookup.rows or []) if _period_ok(r, period)] if lookup.ok else []
    label = window.label
    if lookup.ok and not rows and window.label == "next 7 days":
        wider = find_bookable_slots(
            user=user,
            equipment_id=int(eq.pk),
            start_date=window.start_date,
            end_date=window.start_date + timedelta(days=13),
            limit=limit,
        )
        if wider.ok:
            rows = [r for r in wider.rows if _period_ok(r, period)]
            label = "next 14 days"
    return lookup, rows, label


def _slots_on_day_of(user, eq, slot_id: int) -> list[dict[str, Any]]:
    from iic_booking.equipment.models import DailySlot
    from iic_booking.research_copilot.services.v2.slot_availability import find_bookable_slots

    slot = DailySlot.objects.filter(pk=slot_id, slot_master__equipment_id=eq.pk).only("date").first()
    if slot is None or slot.date is None:
        return []
    lookup = find_bookable_slots(user=user, equipment_id=int(eq.pk), start_date=slot.date, end_date=slot.date, limit=500)
    return lookup.rows if lookup.ok else []


def _equipment_row_actions(r: dict[str, Any]) -> list[dict[str, Any]]:
    acts = [M.choice("equipment_action", f"view:{r['id']}", "View")]
    if r["bookable"]:
        acts += [
            M.choice("equipment_action", f"slots:{r['id']}", "Check slots"),
            M.choice("equipment_action", f"estimate:{r['id']}", "Estimate cost"),
            M.choice("equipment_action", f"book:{r['id']}", "Book"),
        ]
    return acts


def equipment_list(turn: Turn, rows: list[dict[str, Any]], total: int, *, heading: str, offset: int = 0,
                   purpose: str = "view", query: dict[str, Any] | None = None) -> dict[str, Any]:
    """EQUIPMENT_LIST with per-item actions and Show more; picking a name continues `purpose`."""
    options = [{"value": f"pick:{r['id']}", "label": r["name"], "match": r["code"]} for r in rows]
    for r in rows:
        options += [{"value": a["choice"]["value"], "label": a["label"]} for a in _equipment_row_actions(r)]
    actions = []
    shown_to = offset + len(rows)
    if shown_to < total:
        options.append({"value": f"more:{shown_to}", "label": "Show more"})
        actions.append(M.choice("equipment_action", f"more:{shown_to}", f"Show more ({total - shown_to} more)"))
    actions.append(M.choice("equipment_action", "other", "Something else"))
    options.append({"value": "other", "label": "Something else"})
    turn.state["list_purpose"] = purpose
    turn.state["list_query"] = query or {}
    st.set_choice(turn.state, kind="equipment_action", prompt=heading, options=options)
    items = [{**r, "actions": _equipment_row_actions(r)} for r in rows]
    lines = [f"**{heading}**", ""]
    for i, r in enumerate(rows, offset + 1):
        extra = " - ".join(x for x in (r["department"], r["location"]) if x)
        status = "" if r["bookable"] else f" ({r['status_label']})"
        lines.append(f"{i}. **{r['name']}**{status}" + (f" - {extra}" if extra else ""))
    if shown_to < total:
        lines.append("")
        lines.append(f"Showing {offset + 1}-{shown_to} of {total}.")
    return turn.respond(
        message_type=M.EQUIPMENT_LIST,
        content="\n".join(lines),
        cards=[{"type": "equipment_list", "title": heading, "items": items, "total": total, "offset": offset}],
        actions=actions,
        source_label=M.SOURCE_EQUIPMENT,
    )


def resolve_for(turn: Turn, *, text: str | None = None):
    """(equipment, rows, total): one visible instrument, or candidate rows to choose from."""
    user = turn.user
    text = turn.text if text is None else text
    ctx_id = turn.state.get("last_equipment_id") if _CONTEXT_RE.search(terminology.normalize(text)) else None
    res = eqsvc.resolve(user=user, text=text, context_equipment_id=ctx_id)
    if res.confidence in {"EXACT", "ALIAS", "CONTEXTUAL"} and res.equipment_id:
        eq = eqsvc.get_visible(user, res.equipment_id)
        if eq is not None:
            return eq, [], 0
    techniques = list(turn.ents.techniques or [])
    if techniques:
        rows, total = eqsvc.search(user=user, technique_keys=techniques, limit=PAGE)
        if total == 1:
            eq = eqsvc.get_visible(user, rows[0]["id"])
            if eq is not None:
                return eq, [], 0
        if rows:
            return None, rows, total
    if res.confidence == "AMBIGUOUS" and res.candidates:
        rows = []
        for c in res.candidates:
            eq = eqsvc.get_visible(user, c.id)
            if eq is not None:
                rows.append(eqsvc.row(eq))
        return None, rows[:PAGE], len(rows)
    return None, [], 0


def remember_equipment(state: dict[str, Any], eq) -> None:
    state["equipment_id"] = int(eq.pk)
    state["equipment_name"] = eq.name
    state["last_equipment_id"] = int(eq.pk)
    state["last_equipment_name"] = eq.name


def technique_prompt(turn: Turn, *, workflow: str, question: str) -> dict[str, Any]:
    popular = eqsvc.techniques_with_equipment(turn.user, ["xrd", "fesem", "sem", "tem", "xps", "raman", "ftir", "afm"])[:6]
    options = [{"value": k, "label": terminology.TECHNIQUES[k].label.split(" (")[0], "match": k} for k in popular]
    turn.state["workflow"] = workflow
    turn.state["step"] = "choose_technique"
    st.set_choice(turn.state, kind="technique", prompt=question, options=options)
    return turn.respond(
        message_type=M.CHOICE_LIST,
        content=question + " Type the instrument or technique name, or pick one below.",
        cards=[M.choice_card("technique", question, options)],
        actions=[M.choice("technique", o["value"], o["label"]) for o in options],
        source_label=M.SOURCE_EQUIPMENT,
    )


# --------------------------------------------------------------------------- equipment information


def equipment_card(turn: Turn, eq) -> dict[str, Any]:
    r = eqsvc.row(eq)
    remember_equipment(turn.state, eq)
    desc = " ".join(str(getattr(eq, "description", "") or "").split())
    if len(desc) > 600:
        desc = desc[:597] + "..."
    oic = []
    try:
        oic = [m.manager.name or m.manager.email for m in eq.equipment_managers.select_related("manager")[:3] if m.manager]
    except Exception:  # noqa: BLE001
        oic = []
    lines = [f"**{r['name']}**" + (f" ({r['code']})" if r["code"] else ""), ""]
    lines.append(f"- Status: {r['status_label']}")
    if r["department"]:
        lines.append(f"- Department: {r['department']}")
    if r["location"]:
        lines.append(f"- Location: {r['location']}")
    if oic:
        lines.append(f"- Officer in charge: {', '.join(oic)}")
    if desc:
        lines += ["", desc]
    acts = _equipment_row_actions(r)[1:] + [M.link("open_equipment", "Open equipment page", r["href"])]
    st.set_choice(
        turn.state,
        kind="equipment_action",
        prompt=r["name"],
        options=[{"value": a["choice"]["value"], "label": a["label"]} for a in acts if a.get("choice")],
    )
    return turn.respond(
        message_type=M.EQUIPMENT_CARD,
        content="\n".join(lines),
        cards=[{"type": "equipment_card", **r, "description": desc, "oic": oic}],
        actions=acts,
        source_label=M.SOURCE_EQUIPMENT,
        extra={"equipment_id": r["id"], "equipment_name": r["name"]},
    )


def technique_overview(turn: Turn, key: str) -> dict[str, Any]:
    tech = terminology.TECHNIQUES[key]
    rows, total = eqsvc.search(user=turn.user, technique_keys=[key], limit=PAGE)
    lines = [f"**{tech.label}**", "", tech.summary]
    if total:
        lines += ["", f"IIC has {total} matching instrument{'s' if total != 1 else ''} visible to your account."]
        resp = equipment_list(turn, rows, total, heading=f"{tech.label.split(' (')[0]} instruments at IIC",
                              query={"techniques": [key]})
        resp["content"] = "\n".join(lines) + "\n\n" + resp["content"]
        resp["metadata"]["source_label"] = f"{M.SOURCE_TECHNIQUE} + {M.SOURCE_EQUIPMENT}"
        return resp
    lines += ["", "No matching instrument is listed in the IIC catalogue for your account."]
    return turn.respond(message_type=M.TEXT, content="\n".join(lines), source_label=M.SOURCE_TECHNIQUE,
                        actions=[M.ticket_action("user_requested", "Ask the IIC team")])


def search_equipment(turn: Turn) -> dict[str, Any]:
    techniques = list(turn.ents.techniques or turn.ents.purpose_techniques or [])
    rows, total = eqsvc.search(user=turn.user, technique_keys=techniques, text=turn.text, limit=PAGE)
    if not rows:
        if not techniques and not eqsvc.equipment_query(turn.text):
            return technique_prompt(turn, workflow="equipment", question="Which kind of equipment are you looking for?")
        return turn.respond(
            message_type=M.TEXT,
            content="I couldn't find matching equipment in the IIC catalogue for your account. "
            "Try the instrument name or technique (for example FESEM, XRD, Raman).",
            actions=[M.link("browse", "Browse all equipment", "/equipments")],
            source_label=M.SOURCE_EQUIPMENT,
        )
    if total == 1:
        eq = eqsvc.get_visible(turn.user, rows[0]["id"])
        if eq is not None:
            return equipment_card(turn, eq)
    heading = "Matching IIC equipment"
    if techniques:
        heading = ", ".join(terminology.TECHNIQUES[k].label.split(" (")[0] for k in techniques[:3]) + " instruments"
    st.restart(turn.state, "equipment")
    return equipment_list(turn, rows, total, heading=heading,
                          query={"techniques": techniques, "text": "" if techniques else turn.text})


def show_more(turn: Turn, offset: int) -> dict[str, Any]:
    q = turn.state.get("list_query") or {}
    rows, total = eqsvc.search(user=turn.user, technique_keys=list(q.get("techniques") or []),
                               text=q.get("text") or "", limit=PAGE, offset=max(0, offset))
    if not rows:
        return turn.respond(message_type=M.TEXT, content="There are no more matching instruments.",
                            source_label=M.SOURCE_EQUIPMENT)
    return equipment_list(turn, rows, total, heading="More matching equipment", offset=offset,
                          purpose=turn.state.get("list_purpose") or "view", query=q)


def recommend(turn: Turn) -> dict[str, Any]:
    keys = eqsvc.techniques_with_equipment(turn.user, list(turn.ents.purpose_techniques or []))
    if not keys:
        return turn.respond(
            message_type=M.TEXT,
            content="I couldn't match that measurement to an IIC technique. Tell me the property you want to measure "
            "(for example crystal phase, surface morphology, elemental composition) or the technique name.",
            actions=[M.ticket_action("user_requested", "Ask the IIC team")],
            source_label=M.SOURCE_TECHNIQUE,
        )
    options = [
        {"value": k, "label": terminology.TECHNIQUES[k].label, "match": k, "description": terminology.TECHNIQUES[k].summary}
        for k in keys[:6]
    ]
    st.restart(turn.state, "equipment", step="choose_technique")
    st.set_choice(turn.state, kind="technique", prompt="Suitable techniques", options=options)
    lines = ["These IIC techniques fit what you described:", ""]
    lines += [f"- **{o['label']}**: {o['description']}" for o in options]
    lines += ["", "Pick one to see the instruments."]
    return turn.respond(
        message_type=M.CHOICE_LIST,
        content="\n".join(lines),
        cards=[M.choice_card("technique", "Suitable techniques", options)],
        actions=[M.choice("technique", o["value"], o["label"].split(" (")[0]) for o in options],
        source_label=f"{M.SOURCE_TECHNIQUE} + {M.SOURCE_EQUIPMENT}",
    )


def compare(turn: Turn) -> dict[str, Any]:
    keys = list(turn.ents.techniques or [])
    if len(keys) < 2:
        return search_equipment(turn)
    lines = ["**Comparison**", ""]
    cards = []
    for k in keys[:MAX_COMPARE]:
        tech = terminology.TECHNIQUES[k]
        rows, total = eqsvc.search(user=turn.user, technique_keys=[k], limit=3)
        lines.append(f"**{tech.label}**: {tech.summary}")
        lines.append(f"  IIC instruments: {', '.join(r['name'] for r in rows) if rows else 'none listed for your account'}")
        cards.append({"technique": tech.label, "summary": tech.summary, "items": rows, "total": total})
    options = [{"value": k, "label": terminology.TECHNIQUES[k].label.split(" (")[0], "match": k} for k in keys[:MAX_COMPARE]]
    st.restart(turn.state, "equipment", step="choose_technique")
    st.set_choice(turn.state, kind="technique", prompt="Compare", options=options)
    return turn.respond(
        message_type=M.EQUIPMENT_LIST,
        content="\n".join(lines),
        cards=[{"type": "technique_comparison", "items": cards}],
        actions=[M.choice("technique", o["value"], f"Show {o['label']}") for o in options],
        source_label=f"{M.SOURCE_TECHNIQUE} + {M.SOURCE_EQUIPMENT}",
    )


# --------------------------------------------------------------------------- availability


def start_availability(turn: Turn) -> dict[str, Any]:
    st.restart(turn.state, "availability", period=turn.ents.period,
               date_text=turn.text if turn.ents.has_date else None)
    eq, rows, total = resolve_for(turn)
    if eq is not None:
        return show_slots(turn, eq)
    if rows:
        turn.state["step"] = "choose_equipment"
        return equipment_list(turn, rows, total, heading="Which instrument should I check?", purpose="slots",
                              query={"techniques": list(turn.ents.techniques or [])})
    return technique_prompt(turn, workflow="availability", question="Which equipment should I check availability for?")


def show_slots(turn: Turn, eq, *, for_booking: bool = False, required: int = 1) -> dict[str, Any]:
    remember_equipment(turn.state, eq)
    r = eqsvc.row(eq)
    if not r["bookable"]:
        return turn.respond(
            message_type=M.TEXT,
            content=f"**{eq.name}** is currently {r['status_label']} and can't be booked.",
            actions=[M.link("open_equipment", "Open equipment page", r["href"])],
            source_label=M.SOURCE_PORTAL,
        )
    lookup, rows, label = lookup_slots(turn.user, eq, text=turn.state.get("date_text") or "", period=turn.state.get("period"))
    if not lookup.ok:
        return M.error(lookup.message or "Slot availability could not be loaded.", intent=turn.intent,
                       actions=[M.link("calendar", "Open booking calendar", f"/book-equipment?equipment_id={eq.pk}")])
    runs = contiguous_runs(rows, max(1, required))
    if not runs:
        extra = f" for {required} back-to-back slots" if required > 1 else ""
        period = f" in the {turn.state['period']}" if turn.state.get("period") else ""
        return turn.respond(
            message_type=M.SLOT_LIST,
            content=f"No available slots{extra} for **{eq.name}**{period} in the {label}.",
            cards=[{"type": "slot_list", "equipment_id": r["id"], "equipment_name": eq.name, "items": [], "window": label}],
            actions=[
                M.link("calendar", "Open booking calendar", f"/book-equipment?equipment_id={eq.pk}"),
                M.prompt("other_week", "Check next week", f"Check availability for {eq.name} next week"),
            ],
            source_label=M.SOURCE_PORTAL,
        )
    shown = runs[:MAX_SLOT_CHOICES]
    options = [{"value": run_value(run), "label": run_label(run)} for run in shown]
    turn.state["step"] = "choose_slot"
    turn.state["required_slots"] = required
    st.set_choice(turn.state, kind="slot", prompt=f"Slots for {eq.name}", options=options)
    head = "Choose a slot to continue" if for_booking else "Available slots"
    lines = [f"**{eq.name}: {head.lower()} ({label})**", ""]
    lines += [f"{i}. {o['label']}" for i, o in enumerate(options, 1)]
    if required > 1:
        lines += ["", f"Each option is {required} back-to-back slots, the time your inputs need."]
    if len(runs) > len(shown):
        lines += ["", f"{len(runs) - len(shown)} more options are on the booking calendar."]
    items = [
        {"slot_ids": _ids(o["value"]), "label": o["label"], "start": run[0]["start"], "end": run[-1]["end"], "date": run[0]["date"]}
        for o, run in zip(options, shown)
    ]
    return turn.respond(
        message_type=M.SLOT_LIST,
        content="\n".join(lines),
        cards=[{"type": "slot_list", "equipment_id": r["id"], "equipment_name": eq.name, "items": items, "window": label,
                "choice_kind": "slot"}],
        actions=[M.choice("slot", o["value"], ("Book " if not for_booking else "") + o["label"]) for o in options[:3]]
        + [M.link("calendar", "Open booking calendar", f"/book-equipment?equipment_id={eq.pk}")],
        source_label=M.SOURCE_PORTAL,
        extra={"equipment_id": r["id"]},
    )


# --------------------------------------------------------------------------- cost estimate


def _estimate_lines(user, eq, samples: int, data: dict[str, Any] | None) -> tuple[list[str], dict[str, Any]]:
    if not data or data.get("estimate") is None:
        note = (data or {}).get("note") or "No active charge profile, so no estimate is available."
        return [f"**{eq.name}**: {note}"], {"equipment_id": int(eq.pk), "equipment_name": eq.name, "estimate": None}
    br = charge_breakdown(user, data["estimate"])
    label = _field_a_label(user, eq) or "Samples"
    lines = [f"**{eq.name}**", f"- {label}: {samples}"]
    if data.get("total_time_minutes"):
        lines.append(f"- Instrument time: {int(float(data['total_time_minutes']))} minutes")
    if data.get("profile_type"):
        lines.append(f"- Pricing basis: {str(data['profile_type']).replace('_', ' ').lower()} charge profile "
                     f"for {str(data.get('user_type') or '').replace('_', ' ')} users")
    for item in (data.get("breakdown") or [])[:4]:
        if isinstance(item, dict) and item.get("description"):
            lines.append(f"  - {item['description']}: {M.money(item.get('amount'))}")
    lines.append(f"- Charge: {M.money(br['charge'])}")
    if br["gst_percent"]:
        lines.append(f"- GST ({br['gst_percent']:g}%): {M.money(br['gst_amount'])}")
    lines.append(f"- Estimated total: **{M.money(br['total'])}**")
    card = {
        "equipment_id": int(eq.pk),
        "equipment_name": eq.name,
        "samples": samples,
        "sample_label": label,
        "total_time_minutes": data.get("total_time_minutes"),
        "profile_type": data.get("profile_type"),
        "breakdown": (data.get("breakdown") or [])[:6],
        **br,
    }
    return lines, card


def start_estimate(turn: Turn) -> dict[str, Any]:
    samples = turn.ents.sample_count or turn.state.get("samples")
    st.restart(turn.state, "estimate", samples=samples)
    eq, rows, total = resolve_for(turn)
    if eq is not None:
        return estimate_for(turn, [eq], samples)
    if rows and total <= MAX_COMPARE:
        eqs = [e for e in (eqsvc.get_visible(turn.user, r["id"]) for r in rows) if e is not None]
        return estimate_for(turn, eqs, samples)
    if rows:
        turn.state["step"] = "choose_equipment"
        return equipment_list(turn, rows, total, heading="Which instrument should I estimate?", purpose="estimate",
                              query={"techniques": list(turn.ents.techniques or [])})
    return technique_prompt(turn, workflow="estimate", question="Which equipment should I estimate the cost for?")


def estimate_for(turn: Turn, eqs: list, samples: int | None) -> dict[str, Any]:
    n = int(samples or 1)
    blocks, cards = [], []
    for eq in eqs[:MAX_COMPARE]:
        lines, card = _estimate_lines(turn.user, eq, n, estimate(turn.user, eq, n, turn.state.get("input_values")))
        blocks.append("\n".join(lines))
        cards.append(card)
    if len(eqs) == 1:
        remember_equipment(turn.state, eqs[0])
    head = "**Cost estimate**" + ("" if samples else " (for 1 sample; tell me the number of samples for an exact figure)")
    content = head + "\n\n" + "\n\n".join(blocks)
    content += "\n\nEstimates use the portal charge engine and your user category. The booking page shows the final charge."
    actions = []
    if len(eqs) == 1 and eqsvc.row(eqs[0])["bookable"]:
        actions.append(M.choice("equipment_action", f"book:{eqs[0].pk}", f"Book {eqs[0].name}", primary=True))
        actions.append(M.choice("equipment_action", f"slots:{eqs[0].pk}", "Check slots"))
    st.set_choice(turn.state, kind="equipment_action", prompt="Next step",
                  options=[{"value": a["choice"]["value"], "label": a["label"]} for a in actions])
    return turn.respond(
        message_type=M.TEXT,
        content=content,
        cards=[{"type": "cost_estimate", "items": cards}],
        actions=actions,
        source_label=M.SOURCE_PRICING,
    )


# --------------------------------------------------------------------------- booking


def start_booking(turn: Turn) -> dict[str, Any]:
    prev_eq = turn.state.get("last_equipment_id")
    st.restart(
        turn.state,
        "booking",
        samples=turn.ents.sample_count,
        period=turn.ents.period,
        date_text=turn.text if (turn.ents.has_date or turn.ents.earliest) else None,
        earliest=turn.ents.earliest or None,
    )
    eq, rows, total = resolve_for(turn)
    if eq is None and not rows and prev_eq and _CONTEXT_RE.search(terminology.normalize(turn.text)):
        eq = eqsvc.get_visible(turn.user, prev_eq)
    if eq is not None:
        remember_equipment(turn.state, eq)
        return continue_booking(turn)
    if rows:
        turn.state["step"] = "choose_equipment"
        label = ", ".join(terminology.TECHNIQUES[k].label.split(" (")[0] for k in (turn.ents.techniques or [])[:2])
        heading = f"Which {label} would you like to book?" if label else "Which instrument would you like to book?"
        return equipment_list(turn, rows, total, heading=heading, purpose="book",
                              query={"techniques": list(turn.ents.techniques or [])})
    return technique_prompt(turn, workflow="booking", question="Which equipment would you like to book?")


def _ask_samples(turn: Turn, eq, label: str) -> dict[str, Any]:
    turn.state["step"] = "ask_samples"
    options = [{"value": str(n), "label": str(n)} for n in SAMPLE_QUICK_REPLIES]
    st.set_choice(turn.state, kind="samples", prompt=label, options=options)
    return turn.respond(
        message_type=M.FORM_REQUEST,
        content=f"**{eq.name}**: {label.rstrip('?')}?",
        cards=[{"type": "form_request", "equipment_name": eq.name, "submit_label": "Continue",
                "fields": [{"key": "samples", "label": label, "type": "NUMERIC", "required": True, "min": 1}],
                "choice_kind": "samples"}],
        actions=[M.choice("samples", o["value"], o["label"]) for o in options],
        source_label=M.SOURCE_PORTAL,
    )


def _ask_field(turn: Turn, eq, field: dict[str, Any]) -> dict[str, Any]:
    turn.state["step"] = "ask_field"
    turn.state["field_key"] = field["key"]
    turn.state["field_type"] = field["type"]
    options = list(field.get("options") or [])
    if field["type"] == "TOGGLE" and not options:
        options = [{"value": "true", "label": "Yes"}, {"value": "false", "label": "No"}]
    if options:
        st.set_choice(turn.state, kind="field", prompt=field["label"], options=options)
    else:
        st.clear_choice(turn.state)
    help_text = f"\n\n{field['help']}" if field.get("help") else ""
    return turn.respond(
        message_type=M.FORM_REQUEST,
        content=f"**{eq.name}** needs one more detail: **{field['label']}**.{help_text}",
        cards=[{"type": "form_request", "equipment_name": eq.name, "submit_label": "Continue", "fields": [field],
                "choice_kind": "field" if options else None}],
        actions=[M.choice("field", o["value"], o["label"]) for o in options[:8]],
        source_label=M.SOURCE_PORTAL,
    )


def _portal_handoff(turn: Turn, eq, labels: list[str]) -> dict[str, Any]:
    st.restart(turn.state)
    return turn.respond(
        message_type=M.FORM_REQUEST,
        content=f"**{eq.name}** needs booking details that are entered on the booking page ("
        + ", ".join(labels[:5])
        + "). Open the booking page to complete them; your equipment is preselected.",
        actions=[M.link("portal_booking", "Open booking page", f"/book-equipment?equipment_id={eq.pk}")],
        source_label=M.SOURCE_PORTAL,
    )


def continue_booking(turn: Turn) -> dict[str, Any]:
    from iic_booking.research_copilot.services.intelligence import actions_enabled
    from iic_booking.research_copilot.services.v2.mutations import booking as booking_mut

    s = turn.state
    eq = eqsvc.get_visible(turn.user, s.get("equipment_id"))
    if eq is None:
        st.restart(s)
        return M.error("That equipment is not available to your account.", intent=turn.intent)
    r = eqsvc.row(eq)
    if not r["bookable"]:
        st.restart(s)
        return turn.respond(message_type=M.TEXT, content=f"**{eq.name}** is currently {r['status_label']} and can't be booked.",
                            actions=[M.link("open_equipment", "Open equipment page", r["href"])], source_label=M.SOURCE_PORTAL)

    label = _field_a_label(turn.user, eq)
    if label and not s.get("samples"):
        return _ask_samples(turn, eq, label)
    samples = int(s.get("samples") or 1)
    provided = dict(s.get("input_values") or {})
    missing = booking_mut.missing_input_fields(user=turn.user, equipment=eq, samples=samples, provided=provided)
    blocked = [f["label"] for f in missing if not f["chat_fillable"]]
    if blocked:
        return _portal_handoff(turn, eq, blocked)
    if missing:
        return _ask_field(turn, eq, missing[0])

    data = estimate(turn.user, eq, samples, provided)
    required = required_slot_count(eq, (data or {}).get("total_time_minutes"))
    slot_ids = [int(x) for x in (s.get("slot_ids") or [])]
    if slot_ids and len(slot_ids) < required:
        # A single slot picked from the availability list: extend it to the back-to-back run the inputs need.
        rows = _slots_on_day_of(turn.user, eq, slot_ids[0])
        run = next((r for r in contiguous_runs(rows, required) if int(r[0]["slot_id"]) == slot_ids[0]), None)
        slot_ids = [int(x["slot_id"]) for x in run] if run else []
        if slot_ids:
            s["slot_ids"] = slot_ids
        else:
            s.pop("slot_ids", None)
    if not slot_ids:
        s["required_slots"] = required
        if s.get("earliest"):
            _lookup, rows, _label = lookup_slots(turn.user, eq, text=s.get("date_text") or "", period=s.get("period"))
            runs = contiguous_runs(rows, required)
            if runs:
                slot_ids = [int(x["slot_id"]) for x in runs[0]]
                s["slot_ids"] = slot_ids
        if not slot_ids:
            return show_slots(turn, eq, for_booking=True, required=required)

    prep = booking_mut.prepare_booking_create(
        user=turn.user, equipment_id=int(eq.pk), slot_ids=slot_ids, sample_count=samples, input_values=provided or None
    )
    if not prep.get("ok"):
        s.pop("slot_ids", None)
        if prep.get("error") in {"SLOT_NOT_BOOKABLE", "SLOT_UNAVAILABLE", "SLOT_NOT_FOUND", "SLOT_EQUIPMENT_MISMATCH"}:
            resp = show_slots(turn, eq, for_booking=True, required=required)
            resp["content"] = "That slot is no longer available. " + resp["content"]
            return resp
        return M.error(prep.get("message") or "The booking could not be prepared.", intent=turn.intent,
                       actions=[M.link("portal_booking", "Open booking page", f"/book-equipment?equipment_id={eq.pk}")],
                       offer_ticket=True)
    if prep.get("status") == "NEEDS_PORTAL_FORM":
        fields = prep.get("missing_fields") or []
        if fields and all(f.get("chat_fillable") for f in fields):
            return _ask_field(turn, eq, fields[0])
        if fields:
            return _portal_handoff(turn, eq, [f["label"] for f in fields])
        s.pop("samples", None)
        resp = _ask_samples(turn, eq, label or "Number of samples")
        resp["content"] = (", ".join(prep.get("missing_inputs") or []) or "Those inputs are not valid.") + "\n\n" + resp["content"]
        return resp
    if prep.get("status") != "READY_FOR_CONFIRMATION":
        return M.error(prep.get("message") or "The booking could not be prepared.", intent=turn.intent)
    return booking_summary(turn, eq, prep, data, executable=bool(prep.get("executable")) and actions_enabled())


def booking_summary(turn: Turn, eq, prep: dict[str, Any], data: dict[str, Any] | None, *, executable: bool) -> dict[str, Any]:
    from iic_booking.research_copilot.services.v2.mutations import booking as booking_mut
    from iic_booking.research_copilot.services.v2.mutations import proposals as prop_store
    from iic_booking.research_copilot.services.v2.orchestrator import _proposal_card, _store_context

    s = turn.state
    br = charge_breakdown(turn.user, prep.get("estimated_amount"))
    balance = prep.get("wallet_balance")
    after = None
    if balance is not None and br["total"] is not None:
        try:
            after = float(Decimal(str(balance)) - Decimal(str(br["total"])))
        except Exception:  # noqa: BLE001
            after = None
    start = _dt(prep.get("start_time"))
    policy, _until = booking_mut._cancellation_window_note(equipment=eq, start=start) if start else ("", None)
    labels = {f.field_key: (f.field_label or f.field_key) for f in _fields_for(turn.user, eq)}
    inputs = [{"key": k, "label": labels.get(k, k), "value": v} for k, v in sorted((prep.get("input_values") or {}).items())]
    s["slot_ids"] = prep.get("slot_ids") or []
    s["step"] = "confirm"
    card = _proposal_card(prep) | {
        "message_type": M.BOOKING_SUMMARY,
        "executable": executable,
        "slot_count": len(prep.get("slot_ids") or []),
        "inputs": inputs,
        "charge": br["charge"],
        "gst_percent": br["gst_percent"],
        "gst_amount": br["gst_amount"],
        "total_amount": br["total"],
        "balance_after_total": after,
        "policy_note": policy,
        "total_time_minutes": (data or {}).get("total_time_minutes"),
    }
    lines = ["**Booking summary**", "", f"- Equipment: **{eq.name}**"]
    if start:
        end = _dt(prep.get("end_time"))
        lines.append(f"- Date: {start:%A %d %B %Y}")
        lines.append(f"- Time: {start:%H:%M}" + (f"-{end:%H:%M}" if end else ""))
    if prep.get("duration_minutes"):
        lines.append(f"- Duration: {prep['duration_minutes']} minutes ({len(prep.get('slot_ids') or [])} slot(s))")
    for item in inputs:
        lines.append(f"- {item['label']}: {item['value']}")
    lines.append(f"- Estimated charge: {M.money(br['charge'])}")
    if br["gst_percent"]:
        lines.append(f"- GST ({br['gst_percent']:g}%): {M.money(br['gst_amount'])}")
    lines.append(f"- Estimated total: **{M.money(br['total'])}**")
    if balance is not None:
        lines.append(f"- Wallet balance: {M.money(balance)}" + (f" (about {M.money(after)} after this booking)" if after is not None else ""))
    if after is not None and after < 0:
        lines.append("- Your wallet may not cover this booking. Recharge or request credit before confirming.")
    if policy:
        lines.append(f"- Cancellation: {policy}")
    lines.append("")
    edit_options = [
        {"value": "slot", "label": "Change slot"},
        {"value": "samples", "label": "Change samples"},
        {"value": "abort", "label": "Cancel"},
    ]
    st.set_choice(s, kind="booking_edit", prompt="Booking summary", options=edit_options)
    actions: list[dict[str, Any]] = []
    if executable:
        lines.append("Nothing is booked until you press **Confirm Booking**. The portal checks the slot again at that moment.")
        actions.append(
            {
                "id": "confirm_proposal",
                "label": "Confirm Booking",
                "prompt": "Confirm",
                "enabled": True,
                "requires_confirmation": True,
                "proposal_id": prep.get("proposal_id"),
                "confirmation_token": prep.get("confirmation_token"),
                "mutation_action": "CREATE_BOOKING",
            }
        )
        _store_context(
            turn.conversation,
            {
                "proposal_id": prep.get("proposal_id"),
                "confirmation_token": prep.get("confirmation_token"),
                "pending_action": "CREATE_BOOKING",
                "equipment_id": int(eq.pk),
            },
        )
    else:
        prop_store.invalidate_proposal(prep.get("proposal_id"))
        card["proposal_id"] = None
        card["confirmation_token"] = None
        lines.append("Booking from Copilot is not enabled for your account yet. Open the booking page to book this slot.")
    actions += [M.choice("booking_edit", o["value"], o["label"]) for o in edit_options]
    actions.append(M.link("portal_booking", "Open booking page", prep.get("portal_href") or f"/book-equipment?equipment_id={eq.pk}"))
    return turn.respond(
        message_type=M.BOOKING_SUMMARY,
        content="\n".join(lines),
        cards=[card],
        actions=actions,
        source_label=f"{M.SOURCE_PORTAL} + {M.SOURCE_PRICING}",
        extra={
            "equipment_id": int(eq.pk),
            "pending_action": "CREATE_BOOKING" if executable else None,
            "proposal_id": prep.get("proposal_id") if executable else None,
            "executable": executable,
        },
    )


def choose_slot(turn: Turn, value: str) -> dict[str, Any]:
    """A slot run picked from a SLOT_LIST (availability or booking)."""
    s = turn.state
    ids = _ids(value)
    if not ids:
        return M.error("That slot option is not valid. Please choose again.", intent=turn.intent)
    workflow = s.get("workflow")
    if workflow != "booking":
        st.restart(s, "booking", equipment_id=s.get("equipment_id"), equipment_name=s.get("equipment_name"),
                   period=s.get("period"), date_text=s.get("date_text"))
    s["slot_ids"] = ids
    st.clear_choice(s)
    return continue_booking(turn)

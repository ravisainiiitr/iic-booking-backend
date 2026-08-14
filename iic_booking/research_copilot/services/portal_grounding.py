"""Deterministic portal-data grounding for Research Copilot (AI.14).

Server-side tool selection + execution — the LLM never decides permissions.
Tool JSON is injected as PORTAL DATA (trusted application facts for this user).
"""

from __future__ import annotations

import json
import re
from typing import Any

from iic_booking.research_copilot.services import tools as tools_svc


def _wants(text: str, *needles: str) -> bool:
    lower = (text or "").lower()
    return any(n in lower for n in needles)


def plan_tool_calls(*, text: str) -> list[tuple[str, dict[str, Any]]]:
    """Return ordered (tool_name, arguments) for this user turn. Max 3 tools."""
    plans: list[tuple[str, dict[str, Any]]] = []
    lower = (text or "").lower()

    # Extract optional booking id / equipment id / date / file extension
    booking_m = re.search(r"\bbooking\s*#?\s*(\d+)\b", lower) or re.search(r"\b#(\d{3,})\b", lower)
    booking_id = int(booking_m.group(1)) if booking_m else None
    eq_m = re.search(r"\bequipment(?:_id)?\s*[:=]?\s*(\d+)\b", lower)
    equipment_id = int(eq_m.group(1)) if eq_m else None
    date_m = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", lower)
    day = date_m.group(1) if date_m else None
    ext_m = re.search(r"\.(dm3|dm4|tif|tiff|jpg|jpeg|png|csv|xlsx?|spc|raw|ser|emi|mrc|hdf5?)\b", lower)
    file_ext = ext_m.group(1) if ext_m else None

    if _wants(lower, "wallet", "balance", "recharge", "credit"):
        plans.append(("get_wallet", {}))

    if _wants(lower, "next booking", "upcoming booking", "when is my"):
        plans.append(("get_next_booking", {}))
    elif _wants(lower, "my booking", "bookings", "booking status", "cancel my"):
        plans.append(("search_bookings", {}))

    if booking_id and _wants(lower, "sample", "received", "accepted", "rejected", "trace", "status"):
        plans.append(("get_sample_status", {"booking_id": booking_id}))
    elif _wants(
        lower,
        "sample status",
        "status of my sample",
        "my sample status",
        "sample received",
        "sample accepted",
        "sample rejected",
        "has my sample",
    ):
        plans.append(("get_sample_status", {"booking_id": booking_id} if booking_id else {}))

    if booking_id and _wants(lower, "result", "download", "analysis file"):
        plans.append(("get_booking_results", {"booking_id": booking_id}))
    elif _wants(lower, "my result", "results available", "download my result", "is my result"):
        plans.append(("get_booking_results", {"booking_id": booking_id} if booking_id else {}))

    if booking_id and _wants(lower, "deadline", "sample submission", "submit by"):
        plans.append(("get_sample_deadline", {"booking_id": booking_id}))
    elif _wants(lower, "sample submission deadline", "submission deadline"):
        plans.append(("get_sample_deadline", {"booking_id": booking_id} if booking_id else {}))

    if _wants(lower, "slot", "availability", "available tomorrow", "available today") and equipment_id:
        args: dict[str, Any] = {"equipment_id": equipment_id}
        if day:
            args["date"] = day
        plans.append(("search_slots", args))

    if _wants(lower, "cost", "charge", "price", "fee", "how much") and equipment_id:
        plans.append(("estimate_booking_cost", {"equipment_id": equipment_id}))

    if _wants(lower, "software", "digitalmicrograph", "imagej", "origin", "matlab", "analyze", "dm4", "dm3") or file_ext:
        args = {}
        if equipment_id:
            args["equipment_id"] = equipment_id
        if file_ext:
            args["file_type"] = file_ext
        q = text.strip()[:120]
        if q:
            args["query"] = q
        plans.append(("recommend_software", args))

    if _wants(lower, "equipment", "instrument", "fesem", "sem", "tem", "xrd", "afm", "xps", "where is", "location", "resolution", "capability"):
        # Prefer a short keyword query from the user text
        q = text.strip()[:120]
        plans.append(("search_equipment", {"query": q, "limit": 5}))

    if _wants(lower, "sop", "manual", "documentation", "how should i prepare", "sample prep", "guide"):
        plans.append(("search_documentation", {"query": text.strip()[:200]}))

    # Deduplicate by tool name preserving order; cap at 3
    seen: set[str] = set()
    out: list[tuple[str, dict[str, Any]]] = []
    for name, args in plans:
        if name in seen:
            continue
        seen.add(name)
        out.append((name, args))
        if len(out) >= 3:
            break
    return out


def _first_equipment_id_from_tool_result(result: dict[str, Any]) -> int | None:
    """Best-effort resolve a single equipment id from search_equipment output."""
    data = result.get("data")
    rows: list[Any] = []
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        for key in ("results", "equipment", "items", "matches"):
            if isinstance(data.get(key), list):
                rows = data[key]
                break
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in ("equipment_id", "id", "pk"):
            raw = row.get(key)
            if raw is None or raw == "":
                continue
            try:
                return int(raw)
            except (TypeError, ValueError):
                continue
        # Fallback: parse /equipments/123 or equipment:123 style ids
        for key in ("url", "source_id", "href"):
            text = str(row.get(key) or "")
            m = re.search(r"(?:equipment[=:/]|equipments?/|equipment_id[=:])\s*(\d+)", text, re.I)
            if m:
                return int(m.group(1))
    return None


def run_portal_grounding(*, user, text: str) -> dict[str, Any]:
    """
    Execute planned read tools for this turn.

    Returns:
      {
        "block": str,          # injected into system prompt
        "actions": list[dict], # portal action cards
        "tool_results": list,  # compact audit/debug
        "modes": list[str],    # e.g. ["PORTAL_DATA"]
      }
    """
    plans = plan_tool_calls(text=text)
    lower = (text or "").lower()
    wants_cost = _wants(lower, "cost", "charge", "price", "fee", "how much")
    wants_slots = _wants(lower, "slot", "availability", "available tomorrow", "available today", "when can i book")
    # If user names an instrument family without equipment_id, search first so pricing/slots
    # can use authoritative portal ids (AI.20) — never invent prices in the LLM.
    planned_names = {n for n, _ in plans}
    if (wants_cost or wants_slots) and "search_equipment" not in planned_names:
        q = text.strip()[:120]
        if q:
            plans = [("search_equipment", {"query": q, "limit": 5}), *plans][:3]
    if not plans:
        return {"block": "", "actions": [], "tool_results": [], "modes": []}

    tool_results: list[dict] = []
    actions: list[dict] = []
    lines = [
        "<<<PORTAL_DATA>>>",
        "The following facts were retrieved from the IIC Booking portal for THIS authenticated user.",
        "Treat them as authoritative application data. Do not invent additional portal facts.",
        "Label portal-derived claims as based on the user's booking/equipment data.",
        "Mutating suggestions must still require portal confirmation — never claim the action is already done.",
    ]

    resolved_equipment_id: int | None = None
    executed: set[str] = set()

    def _run(name: str, args: dict[str, Any]) -> None:
        nonlocal resolved_equipment_id
        result = tools_svc.execute_tool(name=name, arguments=args, user=user)
        executed.add(name)
        tool_results.append({"tool": name, "ok": bool(result.get("ok")), "error": result.get("error")})
        for a in result.get("actions") or []:
            if isinstance(a, dict) and a.get("id"):
                actions.append(a)
        if name == "search_equipment" and result.get("ok"):
            resolved_equipment_id = _first_equipment_id_from_tool_result(result) or resolved_equipment_id
        payload = {k: result.get(k) for k in ("ok", "error", "message", "data") if k in result}
        try:
            serialized = json.dumps(payload, default=str)[:3500]
        except Exception:
            serialized = str(payload)[:3500]
        lines.append(f"\n### Tool `{name}`")
        lines.append(serialized)

    for name, args in plans:
        _run(name, args)

    # Chain authoritative pricing/slot tools once equipment id is known (server-side).
    if resolved_equipment_id:
        if wants_cost and "estimate_booking_cost" not in executed:
            _run("estimate_booking_cost", {"equipment_id": resolved_equipment_id})
        if wants_slots and "search_slots" not in executed:
            _run("search_slots", {"equipment_id": resolved_equipment_id})

    lines.append("<<<END_PORTAL_DATA>>>")
    return {
        "block": "\n".join(lines),
        "actions": actions,
        "tool_results": tool_results,
        "modes": ["PORTAL_DATA"],
    }

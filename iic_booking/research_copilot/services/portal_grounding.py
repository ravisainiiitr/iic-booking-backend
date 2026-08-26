"""Deterministic portal-data grounding for Research Copilot (AI.14).

Server-side tool selection + execution — the LLM never decides permissions.
Tool JSON is injected as PORTAL DATA (trusted application facts for this user).
"""

from __future__ import annotations

import json
import re
from typing import Any

from iic_booking.research_copilot.services import tools as tools_svc

# Tools allowed for anonymous / public asks (2B).
PUBLIC_TOOL_ALLOWLIST = frozenset(
    {
        "search_documentation",
        "search_equipment",
        "search_slots",
        "estimate_booking_cost",
    }
)


def _wants(text: str, *needles: str) -> bool:
    lower = (text or "").lower()
    return any(n in lower for n in needles)


def _resolve_equipment_id_from_text(text: str) -> int | None:
    """Best-effort equipment id from free text via catalog search."""
    q = (text or "").strip()
    if len(q) < 2:
        return None
    try:
        from iic_booking.research_copilot.services.structured_search import search_equipment

        hits = search_equipment(query=q[:120], limit=3)
        for h in hits:
            sid = getattr(h, "source_id", "") or ""
            if ":" in sid:
                return int(sid.split(":")[1])
    except Exception:  # noqa: BLE001
        return None
    return None


def plan_tool_calls(*, text: str, public: bool = False) -> list[tuple[str, dict[str, Any]]]:
    """Return ordered (tool_name, arguments) for this user turn. Max 3 tools."""
    plans: list[tuple[str, dict[str, Any]]] = []
    lower = (text or "").lower()

    booking_m = re.search(r"\bbooking\s*#?\s*(\d+)\b", lower) or re.search(r"\b#(\d{3,})\b", lower)
    booking_id = int(booking_m.group(1)) if booking_m else None
    eq_m = re.search(r"\bequipment(?:_id)?\s*[:=]?\s*(\d+)\b", lower)
    equipment_id = int(eq_m.group(1)) if eq_m else None
    date_m = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", lower)
    day = date_m.group(1) if date_m else None
    ext_m = re.search(r"\.(dm3|dm4|tif|tiff|jpg|jpeg|png|csv|xlsx?|spc|raw|ser|emi|mrc|hdf5?)\b", lower)
    file_ext = ext_m.group(1) if ext_m else None

    needs_equipment = _wants(
        lower,
        "slot",
        "availability",
        "available",
        "free slot",
        "cost",
        "charge",
        "price",
        "fee",
        "how much",
        "estimate",
        "book",
    )
    if equipment_id is None and needs_equipment and _wants(
        lower, "fesem", "sem", "tem", "xrd", "pxrd", "afm", "xps", "icp", "apreo", "equipment", "instrument"
    ):
        equipment_id = _resolve_equipment_id_from_text(text)

    if not public and _wants(lower, "wallet", "balance", "recharge", "credit"):
        plans.append(("get_wallet", {}))

    if not public:
        if _wants(lower, "next booking", "upcoming booking", "when is my"):
            plans.append(("get_next_booking", {}))
        elif _wants(lower, "my booking", "bookings", "booking status", "cancel my"):
            plans.append(("search_bookings", {}))

        if booking_id and _wants(lower, "sample", "received", "accepted", "rejected", "trace", "status"):
            plans.append(("get_sample_status", {"booking_id": booking_id}))
        elif _wants(lower, "sample status", "sample received", "sample accepted", "sample rejected", "has my sample"):
            plans.append(("get_sample_status", {"booking_id": booking_id} if booking_id else {}))

        if booking_id and _wants(lower, "result", "download", "analysis file"):
            plans.append(("get_booking_results", {"booking_id": booking_id}))
        elif _wants(lower, "my result", "results available", "download my result", "is my result"):
            plans.append(("get_booking_results", {"booking_id": booking_id} if booking_id else {}))

        if booking_id and _wants(lower, "deadline", "sample submission", "submit by"):
            plans.append(("get_sample_deadline", {"booking_id": booking_id}))
        elif _wants(lower, "sample submission deadline", "submission deadline"):
            plans.append(("get_sample_deadline", {"booking_id": booking_id} if booking_id else {}))

    if _wants(lower, "slot", "availability", "available tomorrow", "available today", "free slot", "free slots") and equipment_id:
        args: dict[str, Any] = {"equipment_id": equipment_id}
        if day:
            args["date"] = day
        if public:
            args["public"] = True
        plans.append(("search_slots", args))

    if _wants(lower, "cost", "charge", "price", "fee", "how much", "estimate") and equipment_id:
        est_args: dict[str, Any] = {"equipment_id": equipment_id}
        if public:
            est_args["public"] = True
        plans.append(("estimate_booking_cost", est_args))

    if not public and (
        _wants(lower, "software", "digitalmicrograph", "imagej", "origin", "matlab", "analyze", "dm4", "dm3") or file_ext
    ):
        args = {}
        if equipment_id:
            args["equipment_id"] = equipment_id
        if file_ext:
            args["file_type"] = file_ext
        q = text.strip()[:120]
        if q:
            args["query"] = q
        plans.append(("recommend_software", args))

    if _wants(
        lower,
        "equipment",
        "instrument",
        "fesem",
        "sem",
        "tem",
        "xrd",
        "pxrd",
        "afm",
        "xps",
        "where is",
        "location",
        "resolution",
        "capability",
        "find equipment",
    ):
        q = text.strip()[:120]
        plans.append(("search_equipment", {"query": q, "limit": 5}))

    # Docs / FAQ — HOLD, sample accept, manuals, remote analysis troubleshooting
    if _wants(
        lower,
        "sop",
        "manual",
        "documentation",
        "how should i prepare",
        "sample prep",
        "guide",
        "hold",
        "what does hold",
        "accept a sample",
        "remote analysis",
        "won't connect",
        "troubleshoot",
        "operator manual",
        "faq",
        "meaning",
        "what is",
        "how do i",
    ):
        plans.append(("search_documentation", {"query": text.strip()[:200]}))

    # Deduplicate by tool name preserving order; cap at 3
    seen: set[str] = set()
    out: list[tuple[str, dict[str, Any]]] = []
    for name, args in plans:
        if public and name not in PUBLIC_TOOL_ALLOWLIST:
            continue
        if name in seen:
            continue
        seen.add(name)
        out.append((name, args))
        if len(out) >= 3:
            break
    return out


def run_portal_grounding(*, user, text: str, public: bool = False) -> dict[str, Any]:
    """
    Execute planned read tools for this turn.

    Returns:
      {
        "block": str,
        "actions": list[dict],
        "tool_results": list,
        "modes": list[str],
      }
    """
    plans = plan_tool_calls(text=text, public=public)
    if not plans:
        return {"block": "", "actions": [], "tool_results": [], "modes": []}

    tool_results: list[dict] = []
    actions: list[dict] = []
    audience = "anonymous / public visitor" if public else "THIS authenticated user"
    lines = [
        "<<<PORTAL_DATA>>>",
        f"The following facts were retrieved from the IIC Booking portal for a {audience}.",
        "Treat them as ground truth. Do not invent balances, slots, or prices beyond this block.",
    ]
    if public:
        lines.append(
            "Public mode: do not claim personal bookings/wallet. Offer Sign in for book/recharge/my bookings."
        )

    for name, args in plans:
        result = tools_svc.execute_tool(name=name, arguments=args, user=user)
        tool_results.append({"tool": name, "ok": bool(result.get("ok")), "summary": result.get("message") or ""})
        if result.get("ok"):
            lines.append(f"### tool:{name}")
            lines.append(json.dumps(result.get("data"), default=str)[:3500])
            for a in result.get("actions") or []:
                if a.get("id") and all(x.get("id") != a.get("id") for x in actions):
                    actions.append(a)
        else:
            lines.append(f"### tool:{name} FAILED")
            lines.append(json.dumps({"error": result.get("error"), "message": result.get("message")}))

    lines.append("<<<END_PORTAL_DATA>>>")
    return {
        "block": "\n".join(lines),
        "actions": actions,
        "tool_results": tool_results,
        "modes": ["PORTAL_DATA"] + (["PUBLIC"] if public else []),
    }

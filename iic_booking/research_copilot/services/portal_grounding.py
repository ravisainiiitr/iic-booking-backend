"""Deterministic portal-data grounding for Research Copilot (AI.14).

Server-side tool selection + execution — the LLM never decides permissions.
Tool JSON is injected as PORTAL DATA (trusted application facts for this user).
"""

from __future__ import annotations

import json
import re
from typing import Any

from iic_booking.research_copilot.services import tools as tools_svc
from iic_booking.research_copilot.services.access_control import AccessMode
from iic_booking.research_copilot.services.query_intelligence import extract_num_samples
from iic_booking.research_copilot.services.structured_search import (
    _equipment_family,
    xrd_family_clarification,
)


def _wants(text: str, *needles: str) -> bool:
    lower = (text or "").lower()
    return any(n in lower for n in needles)


def plan_tool_calls(*, text: str) -> list[tuple[str, dict[str, Any]]]:
    """Return ordered (tool_name, arguments) for this user turn. Max 3 tools."""
    plans: list[tuple[str, dict[str, Any]]] = []
    lower = (text or "").lower()

    # Extract optional booking id / equipment id / date / file extension / samples
    booking_m = re.search(r"\bbooking\s*#?\s*(\d+)\b", lower) or re.search(r"\b#(\d{3,})\b", lower)
    booking_id = int(booking_m.group(1)) if booking_m else None
    eq_m = re.search(r"\bequipment(?:_id)?\s*[:=]?\s*(\d+)\b", lower)
    equipment_id = int(eq_m.group(1)) if eq_m else None
    date_m = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", lower)
    day = date_m.group(1) if date_m else None
    ext_m = re.search(r"\.(dm3|dm4|tif|tiff|jpg|jpeg|png|csv|xlsx?|spc|raw|ser|emi|mrc|hdf5?)\b", lower)
    file_ext = ext_m.group(1) if ext_m else None
    num_samples = extract_num_samples(text)

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
    elif _wants(
        lower,
        "my result",
        "results available",
        "download my result",
        "is my result",
        "are my results",
        "where are my results",
        "where can i download",
        "results ready",
        "analyzed files",
        "analyzed data",
    ):
        plans.append(("get_booking_results", {"booking_id": booking_id} if booking_id else {}))

    if booking_id and _wants(lower, "deadline", "sample submission", "submit by"):
        plans.append(("get_sample_deadline", {"booking_id": booking_id}))
    elif _wants(
        lower,
        "sample submission deadline",
        "submission deadline",
        "when should i submit",
        "submit my sample",
        "sample deadline",
    ):
        plans.append(("get_sample_deadline", {"booking_id": booking_id} if booking_id else {}))

    if (
        _wants(lower, "slot", "availability", "available tomorrow", "available today", "when can i book")
        or (
            _wants(lower, "when is", "available")
            and not _wants(lower, "next booking", "my booking", "my sample", "my result")
        )
    ) and equipment_id:
        args: dict[str, Any] = {"equipment_id": equipment_id}
        if day:
            args["date"] = day
        plans.append(("search_slots", args))

    if _wants(lower, "cost", "charge", "price", "fee", "how much", "pi rate", "pi pricing") and equipment_id:
        cost_args: dict[str, Any] = {"equipment_id": equipment_id}
        if num_samples is not None:
            cost_args["num_samples"] = num_samples
        plans.append(("estimate_booking_cost", cost_args))

    remote_analysis_ask = _wants(
        lower,
        "remote analysis",
        "analyze remotely",
        "analysis pc",
        "analysis workstation",
        "guacamole",
        "raa",
        "remote desktop",
    )
    if (
        _wants(lower, "software", "digitalmicrograph", "imagej", "origin", "matlab", "analyze", "dm4", "dm3")
        or file_ext
        or remote_analysis_ask
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

    # Equipment catalog search — skip pure definitional/general-science questions
    # (AI.21.2): "What is XRD?" should not dump full instrument specs into the LLM prompt.
    definitional = bool(
        re.search(r"\b(what is|what's|whats|define|explain|difference between|mean by)\b", lower)
    ) and not _wants(
        lower,
        "book",
        "slot",
        "available",
        "cost",
        "price",
        "fee",
        "charge",
        "software",
        "prepare",
        "my booking",
        "location",
        "where is",
    )
    if (
        not definitional
        and _wants(
            lower,
            "equipment",
            "instrument",
            "fesem",
            "sem",
            "tem",
            "xrd",
            "afm",
            "xps",
            "where is",
            "location",
            "resolution",
            "capability",
        )
    ):
        q = text.strip()[:120]
        plans.append(("search_equipment", {"query": q, "limit": 5}))

    prepare_docs = _wants(
        lower,
        "sop",
        "manual",
        "documentation",
        "how should i prepare",
        "sample prep",
        "guide",
        "what should i prepare",
        "prepare before",
        "prepare for",
    )
    if prepare_docs:
        plans.append(("search_documentation", {"query": text.strip()[:200]}))
        # Docs answer prep questions; avoid dumping the equipment catalog into the prompt.
        if not _wants(lower, "where is", "location", "cost", "price", "fee", "charge", "slot"):
            plans = [(n, a) for n, a in plans if n != "search_equipment"]

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

    # AI.21.2: software recommendation does not need a full equipment catalog dump
    # unless the user also asks for location / instrument details / cost / slots.
    names = {n for n, _ in out}
    if "recommend_software" in names and "search_equipment" in names:
        if not _wants(
            lower,
            "where is",
            "location",
            "cost",
            "price",
            "fee",
            "charge",
            "how much",
            "slot",
            "availability",
            "available",
        ):
            out = [(n, a) for n, a in out if n != "search_equipment"]
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


def _compact_tool_payload(result: dict[str, Any], *, tool_name: str) -> dict[str, Any]:
    """Shrink portal tool JSON before injecting into the LLM prompt (AI.21.2)."""
    payload: dict[str, Any] = {k: result.get(k) for k in ("ok", "error", "message") if k in result}
    data = result.get("data")
    # Pricing / slots only need the matched instrument identity, not a catalog dump.
    list_limit = 1 if tool_name == "search_equipment" else 4
    snippet_limit = 120 if tool_name == "search_equipment" else 160
    if isinstance(data, list):
        rows = []
        for row in data[:list_limit]:
            if not isinstance(row, dict):
                rows.append(row)
                continue
            compact = {
                k: row.get(k)
                for k in (
                    "id",
                    "equipment_id",
                    "title",
                    "name",
                    "equipment_name",
                    "status",
                    "url",
                    "score",
                    "family",
                    "software_id",
                    "slug",
                    "is_default",
                )
                if k in row
            }
            snippet = row.get("snippet") or row.get("description") or ""
            if snippet:
                compact["snippet"] = str(snippet)[:snippet_limit]
            rows.append(compact or {k: row.get(k) for k in list(row)[:6]})
        payload["data"] = rows
    elif isinstance(data, dict):
        # Prefer concise portal fields; drop bulky nested blobs.
        keep = {
            k: data.get(k)
            for k in (
                "booking",
                "booking_id",
                "virtual_booking_id",
                "equipment_id",
                "equipment_name",
                "date",
                "status",
                "note",
                "estimate",
                "source",
                "balance",
                "slots",
                "results",
                "deadline",
                "sample_status",
                "requires_confirmation",
                "message",
                "amount",
                "currency",
                "charge_profile",
                "samples",
                "num_samples",
                "unit_price",
                "total",
                "citations",
                "context_preview",
                "intent",
                "low_confidence",
                "pricing_resolution",
                "pricing_profile",
                "user_type",
            )
            if k in data
        }
        if "booking" in keep and isinstance(keep["booking"], dict):
            b = keep["booking"]
            keep["booking"] = {
                k: b.get(k)
                for k in (
                    "booking_id",
                    "virtual_booking_id",
                    "equipment",
                    "equipment_id",
                    "status",
                    "date",
                    "start",
                    "end",
                    "slot",
                )
                if k in b
            }
        if "slots" in keep and isinstance(keep["slots"], list):
            slim_slots = []
            for slot in keep["slots"][:6]:
                if isinstance(slot, dict):
                    slim_slots.append(
                        {
                            k: slot.get(k)
                            for k in ("date", "start", "end", "status", "available", "slot_id")
                            if k in slot
                        }
                    )
                else:
                    slim_slots.append(slot)
            keep["slots"] = slim_slots
        if "estimate" in keep and isinstance(keep["estimate"], dict):
            est = keep["estimate"]
            keep["estimate"] = {
                k: est.get(k)
                for k in (
                    "amount",
                    "total",
                    "currency",
                    "charge_profile",
                    "samples",
                    "num_samples",
                    "unit_price",
                    "breakdown",
                    "note",
                )
                if k in est
            }
        if "results" in keep and isinstance(keep["results"], list):
            keep["results"] = keep["results"][:5]
        if "citations" in data and isinstance(data.get("citations"), list):
            keep["citations"] = [
                {
                    k: c.get(k)
                    for k in ("title", "snippet", "category", "source_type", "url")
                    if isinstance(c, dict) and k in c
                }
                for c in data["citations"][:2]
                if isinstance(c, dict)
            ]
            for c in keep["citations"]:
                if "snippet" in c:
                    c["snippet"] = str(c["snippet"])[:160]
        if "context_preview" in data:
            keep["context_preview"] = str(data.get("context_preview") or "")[:500]
        payload["data"] = keep or {k: data.get(k) for k in list(data)[:10]}
    elif data is not None:
        payload["data"] = data
    return payload


def _compact_tool_payload_for_prompt(
    result: dict[str, Any],
    *,
    tool_name: str,
    wants_cost: bool = False,
    multi_tool: bool = False,
) -> dict[str, Any]:
    """AI.22.2: extra-tight compaction for mixed portal turns (cost + prepare)."""
    compact = _compact_tool_payload(result, tool_name=tool_name)
    if tool_name != "search_documentation":
        return compact
    data = compact.get("data")
    if not isinstance(data, dict):
        return compact
    if wants_cost or multi_tool:
        if "context_preview" in data:
            data["context_preview"] = str(data.get("context_preview") or "")[:280]
        cites = data.get("citations")
        if isinstance(cites, list):
            data["citations"] = cites[:1]
            for c in data["citations"]:
                if isinstance(c, dict) and "snippet" in c:
                    c["snippet"] = str(c["snippet"])[:120]
    return compact


def run_portal_grounding(*, user, text: str, access_mode: str | AccessMode | None = None) -> dict[str, Any]:
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
    from iic_booking.research_copilot.services.access_control import (
        AccessMode,
        resolve_access_mode,
        tool_allowed_for_mode,
    )
    from iic_booking.research_copilot.services.tools import TOOL_REGISTRY

    mode = AccessMode(str(access_mode)) if access_mode else resolve_access_mode(user=user)

    plans = plan_tool_calls(text=text)
    # Backend ACL — drop any planned non-public tools before execution (AI.24.1).
    filtered: list[tuple[str, dict]] = []
    for name, args in plans:
        spec = next((t for t in TOOL_REGISTRY if t.name == name), None)
        if spec is None:
            continue
        if not tool_allowed_for_mode(access_level=spec.access_level, access_mode=mode):
            continue
        filtered.append((name, args))
    plans = filtered

    lower = (text or "").lower()
    wants_cost = _wants(lower, "cost", "charge", "price", "fee", "how much", "pi rate", "pi pricing")
    wants_slots = _wants(
        lower,
        "slot",
        "availability",
        "available tomorrow",
        "available today",
        "when can i book",
    ) or (
        bool(re.search(r"\b(is|are)\s+.+\bavailable\b", lower) or re.search(r"\bavailable\s+(tomorrow|today|this week)\b", lower))
        and "equipment" not in lower
        and "instrument" not in lower
        and not _wants(lower, "next booking", "my booking", "my sample", "my result")
        and any(h in lower for h in ("xrd", "pxrd", "fesem", "sem", "tem", "afm", "xps", "gi-xrd"))
    )
    # Availability slots are authenticated-only unless a future public endpoint exists.
    if mode == AccessMode.PUBLIC:
        wants_slots = False
    num_samples = extract_num_samples(text)
    # If user names an instrument family without equipment_id, search first so pricing/slots
    # can use authoritative portal ids (AI.20) — never invent prices in the LLM.
    planned_names = {n for n, _ in plans}
    if (wants_cost or wants_slots) and "search_equipment" not in planned_names:
        q = text.strip()[:120]
        if q:
            plans = [("search_equipment", {"query": q, "limit": 5}), *plans][:3]
    if not plans:
        return {
            "block": "",
            "actions": [],
            "tool_results": [],
            "modes": [],
            "clarification": None,
            "structured": {},
            "deterministic_reply": None,
        }

    tool_results: list[dict] = []
    actions: list[dict] = []
    if mode == AccessMode.PUBLIC:
        lines = [
            "<<<PORTAL_DATA>>>",
            "The following facts were retrieved from APPROVED PUBLIC IIC portal information.",
            "Treat them as authoritative public catalogue data. Do not invent prices or private facts.",
            "Do not reveal internal infrastructure, hostnames, IPs, tunnels, secrets, or private bookings.",
            "Answer concisely using these facts (prefer under 8 short sentences).",
        ]
    else:
        lines = [
            "<<<PORTAL_DATA>>>",
            "The following facts were retrieved from the IIC Booking portal for THIS authenticated user.",
            "Treat them as authoritative application data. Do not invent additional portal facts.",
            "Label portal-derived claims as based on the user's booking/equipment data.",
            "Mutating suggestions must still require portal confirmation — never claim the action is already done.",
            "Answer concisely using these facts (prefer under 8 short sentences).",
        ]

    resolved_equipment_id: int | None = None
    executed: set[str] = set()
    last_equipment_hits: list[dict[str, Any]] = []
    multi_tool = len(plans) >= 2 or wants_cost  # cost often chains a second tool
    wants_prepare_docs = _wants(
        lower,
        "sop",
        "manual",
        "documentation",
        "how should i prepare",
        "sample prep",
        "guide",
        "what should i prepare",
        "prepare before",
        "prepare for",
    )
    structured: dict[str, Any] = {
        "equipment_name": "",
        "estimate": None,
        "pricing_resolution": None,
        "documentation_preview": "",
        "documentation_citations": [],
        "equipment_hits": [],
        "tool_errors": [],
    }

    def _run(name: str, args: dict[str, Any]) -> dict[str, Any]:
        nonlocal resolved_equipment_id
        result = tools_svc.execute_tool(name=name, arguments=args, user=user, access_mode=mode)
        executed.add(name)
        tool_results.append({"tool": name, "ok": bool(result.get("ok")), "error": result.get("error")})
        if not result.get("ok"):
            structured["tool_errors"].append(
                {
                    "tool": name,
                    "error": result.get("error"),
                    "message": result.get("message"),
                }
            )
        for a in result.get("actions") or []:
            if isinstance(a, dict) and a.get("id"):
                actions.append(a)
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        if name == "estimate_booking_cost" and result.get("ok"):
            structured["estimate"] = data.get("estimate")
            structured["pricing_resolution"] = data.get("pricing_resolution")
            structured["equipment_name"] = str(data.get("equipment_name") or structured["equipment_name"] or "")[:120]
        if name == "search_documentation" and result.get("ok"):
            structured["documentation_preview"] = str(data.get("context_preview") or "")[:400]
            cites = data.get("citations") if isinstance(data.get("citations"), list) else []
            structured["documentation_citations"] = cites[:2]
        if name == "search_equipment" and result.get("ok"):
            rows = result.get("data") if isinstance(result.get("data"), list) else []
            last_equipment_hits.clear()
            for row in rows:
                if isinstance(row, dict):
                    last_equipment_hits.append(row)
            structured["equipment_hits"] = list(last_equipment_hits[:6])
            resolved_equipment_id = _first_equipment_id_from_tool_result(result) or resolved_equipment_id
            if last_equipment_hits and not structured["equipment_name"]:
                structured["equipment_name"] = str(
                    last_equipment_hits[0].get("title")
                    or last_equipment_hits[0].get("name")
                    or last_equipment_hits[0].get("equipment_name")
                    or ""
                )[:120]
            # AI.22.2: when equipment search is only to resolve an id for pricing/slots
            # (especially mixed cost+prepare), inject a one-line identity — not a catalog dump.
            # Catalog dumps + docs + estimate overloaded llama3.2:1b and caused Q-U-001 timeout.
            if wants_cost or wants_slots:
                title = structured["equipment_name"] or ""
                lines.append(f"\n### Tool `{name}`")
                lines.append(
                    json.dumps(
                        {
                            "ok": True,
                            "resolved_equipment_id": resolved_equipment_id,
                            "name": title,
                            "note": "identity_only_for_pricing_or_slots",
                        },
                        separators=(",", ":"),
                    )
                )
                return result
        compact = _compact_tool_payload_for_prompt(
            result,
            tool_name=name,
            wants_cost=wants_cost,
            multi_tool=multi_tool or wants_prepare_docs,
        )
        # Mixed cost+docs: keep serialized portal JSON smaller for CPU 1b generation.
        ser_limit = 1100
        if name == "search_documentation" and (wants_cost or multi_tool):
            ser_limit = 550
        elif multi_tool and name not in {"estimate_booking_cost", "get_wallet", "get_next_booking"}:
            ser_limit = 750
        try:
            serialized = json.dumps(compact, default=str, separators=(",", ":"))[:ser_limit]
        except Exception:
            serialized = str(compact)[:ser_limit]
        lines.append(f"\n### Tool `{name}`")
        lines.append(serialized)
        return result

    for name, args in plans:
        _run(name, args)

    # AI.22.1: bare "XRD" with multiple families → clarify before pricing/slots chain.
    if (wants_cost or wants_slots) and last_equipment_hits:
        class _H:
            def __init__(self, title: str, family: str = ""):
                self.title = title
                self.family = family or _equipment_family(title)

        hit_objs = [_H(str(r.get("title") or r.get("name") or ""), str(r.get("family") or "")) for r in last_equipment_hits]
        # Attach families from titles when tool payload omitted family
        xrd_q = xrd_family_clarification(text=text, hits=hit_objs)
        if xrd_q:
            return {
                "block": "",
                "actions": actions,
                "tool_results": tool_results,
                "modes": ["CLARIFICATION"],
                "clarification": xrd_q,
                "structured": structured,
                "deterministic_reply": None,
            }

    # Chain authoritative pricing/slot tools once equipment id is known (server-side).
    if resolved_equipment_id:
        if wants_cost and "estimate_booking_cost" not in executed:
            cost_args: dict[str, Any] = {"equipment_id": resolved_equipment_id}
            if num_samples is not None:
                cost_args["num_samples"] = num_samples
            _run("estimate_booking_cost", cost_args)
        if wants_slots and "search_slots" not in executed:
            _run("search_slots", {"equipment_id": resolved_equipment_id})

    lines.append("<<<END_PORTAL_DATA>>>")
    block = "\n".join(lines)
    # AI.22.2 safety net: oversized portal blocks stall CPU 1b generation (Q-U-001 ~60s timeout).
    if len(block) > 3200:
        block = block[:3100] + "\n…(portal context truncated for latency)\n<<<END_PORTAL_DATA>>>"

    deterministic_reply = None
    # Mixed cost + prepare: portal tools already hold both answers. Skip LLM on the
    # constrained CPU envelope to eliminate the measured Q-U-001 timeout without
    # inventing prices or prep guidance.
    tool_names = {t.get("tool") for t in tool_results}
    if (
        wants_cost
        and wants_prepare_docs
        and "estimate_booking_cost" in tool_names
        and "search_documentation" in tool_names
        and structured.get("estimate")
    ):
        deterministic_reply = _format_cost_prepare_reply(structured)

    wants_equipment_list = _wants(
        lower,
        "which equipment",
        "what equipment",
        "equipment do we have",
        "equipment available",
        "services are available",
        "which instruments",
    )
    if (
        deterministic_reply is None
        and wants_equipment_list
        and structured.get("equipment_hits")
        and "recommend_software" not in tool_names
        and not wants_prepare_docs
    ):
        # AI.22.2: Q-V-003 timed out while the 1b model narrated a long invented catalog.
        deterministic_reply = _format_equipment_list_reply(structured["equipment_hits"])

    # Do not let the LLM invent portal successes when tools returned not-found / forbidden.
    if deterministic_reply is None and structured.get("tool_errors"):
        err0 = structured["tool_errors"][0]
        code = str(err0.get("error") or "")
        if code in {
            "booking_not_found",
            "equipment_not_found",
            "forbidden",
            "invalid_booking_id",
            "missing_equipment_id",
        }:
            msg = str(err0.get("message") or code)
            deterministic_reply = (
                f"Based on **PORTAL DATA**: {msg} "
                "I will not invent booking, equipment, or pricing details that the portal did not return."
            )

    return {
        "block": block,
        "actions": actions,
        "tool_results": tool_results,
        "modes": ["PORTAL_DATA"] + (["DETERMINISTIC"] if deterministic_reply else []),
        "clarification": None,
        "structured": structured,
        "deterministic_reply": deterministic_reply,
    }


def _format_equipment_list_reply(hits: list[dict[str, Any]]) -> str:
    """Concise portal-grounded equipment listing (no LLM narration)."""
    parts = ["Based on **PORTAL DATA**, matching equipment:"]
    for row in hits[:5]:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or row.get("name") or row.get("equipment_name") or "Equipment").strip()
        eid = row.get("equipment_id") or row.get("id") or row.get("pk")
        family = str(row.get("family") or "").strip()
        bit = f"- {title}"
        if eid is not None:
            bit += f" (id={eid})"
        if family:
            bit += f" [{family}]"
        parts.append(bit)
    if len(parts) == 1:
        parts.append("- No equipment rows matched this query in the portal catalog.")
    parts.append(
        "Ask for slots, sample cost, preparation, or remote-analysis software for a specific instrument."
    )
    return "\n".join(parts)


def _format_cost_prepare_reply(structured: dict[str, Any]) -> str:
    """Compose a concise portal-grounded reply for mixed cost+prepare questions."""
    est = structured.get("estimate") if isinstance(structured.get("estimate"), dict) else {}
    eq_name = structured.get("equipment_name") or "the selected equipment"
    amount = est.get("amount")
    currency = est.get("currency") or "INR"
    profile = est.get("charge_profile") or est.get("pricing_profile") or "standard"
    parts = [
        "Based on **PORTAL DATA** for your account:",
        "",
        f"**Cost estimate ({eq_name}):** "
        + (f"{currency} {amount}" if amount is not None else "see portal calculate for the live total")
        + f" (charge profile: `{profile}`).",
    ]
    pi = structured.get("pricing_resolution") if isinstance(structured.get("pricing_resolution"), dict) else {}
    if pi:
        parts.append(
            "PI rate applies only when the server-side pricing resolver marks the "
            f"billing identity as PI (current: billing_identity_is_pi="
            f"{pi.get('billing_identity_is_pi')}, resolved_profile="
            f"{pi.get('resolved_pricing_profile')})."
        )
    preview = (structured.get("documentation_preview") or "").strip()
    if preview:
        parts.extend(["", "**What to prepare (documentation):**", preview[:420]])
    else:
        parts.extend(
            [
                "",
                "**What to prepare:** Open the equipment SOP / sample-preparation guide "
                "from Documentation in the portal for authoritative steps.",
            ]
        )
    cites = structured.get("documentation_citations") or []
    titles = [str(c.get("title") or "").strip() for c in cites if isinstance(c, dict) and c.get("title")]
    if titles:
        parts.append("Sources: " + "; ".join(titles[:2]))
    parts.append(
        "Amounts and PI eligibility are determined by the portal charge engine — "
        "not by the assistant. Confirm any booking changes in the portal UI."
    )
    return "\n".join(parts)

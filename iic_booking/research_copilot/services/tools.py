"""Tool registry — read-only tools executable; mutating tools return confirmation action cards."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    mutating: bool
    roles: tuple[str, ...]


TOOL_REGISTRY: list[ToolSpec] = [
    ToolSpec("search_equipment", "Search instruments by name/capability", False, ("*",)),
    ToolSpec("search_slots", "Search available slots for equipment/date", False, ("*",)),
    ToolSpec("search_bookings", "List caller's bookings", False, ("*",)),
    ToolSpec("get_wallet", "Wallet summary for caller", False, ("student", "faculty", "external", "admin")),
    ToolSpec("search_documentation", "RAG over docs", False, ("*",)),
    ToolSpec("recommend_software", "Recommend analysis software via R6 catalog", False, ("*",)),
    ToolSpec("create_booking", "Prepare booking options (requires confirmation)", True, ("student", "faculty", "external", "admin")),
    ToolSpec("cancel_booking", "Prepare cancel action (requires confirmation)", True, ("student", "faculty", "external", "admin")),
    ToolSpec("create_support_ticket", "Create support ticket", True, ("*",)),
    ToolSpec("launch_remote_analysis", "Open Analysis Workspace for a booking", True, ("student", "faculty", "operator", "admin")),
]


def list_tools_for_role(role_bucket: str) -> list[dict]:
    out = []
    for t in TOOL_REGISTRY:
        if "*" in t.roles or role_bucket in t.roles or role_bucket == "admin":
            out.append(
                {
                    "name": t.name,
                    "description": t.description,
                    "mutating": t.mutating,
                    # Read-only tools are executable; mutating tools return action cards only.
                    "available": not t.mutating or t.name in {"create_booking", "cancel_booking", "launch_remote_analysis"},
                }
            )
    return out


def _ok(data: dict | list | None = None, **extra) -> dict:
    payload = {"ok": True}
    if data is not None:
        payload["data"] = data
    payload.update(extra)
    return payload


def _err(code: str, message: str, **extra) -> dict:
    return {"ok": False, "error": code, "message": message, **extra}


def _search_equipment(*, arguments: dict, user) -> dict:
    from iic_booking.research_copilot.services.structured_search import search_equipment

    query = str(arguments.get("query") or arguments.get("q") or "").strip()
    limit = int(arguments.get("limit") or 8)
    hits = search_equipment(query=query, limit=max(1, min(limit, 20)))
    return _ok(
        [
            {
                "equipment_id": int(h.source_id.split(":")[1]) if ":" in h.source_id else None,
                "title": h.title,
                "snippet": h.snippet,
                "url": h.url,
                "score": h.score,
            }
            for h in hits
        ],
        actions=[
            {
                "id": "open_equipment",
                "label": f"Open {h.title}",
                "href": h.url,
                "enabled": True,
            }
            for h in hits[:3]
        ],
    )


def _search_slots(*, arguments: dict, user) -> dict:
    from iic_booking.equipment.models import Equipment

    equipment_id = arguments.get("equipment_id")
    day_raw = arguments.get("date") or arguments.get("day")
    if not equipment_id:
        return _err("missing_equipment_id", "equipment_id is required for slot search")
    try:
        eq = Equipment.objects.get(pk=int(equipment_id))
    except Exception:
        return _err("equipment_not_found", f"Equipment {equipment_id} not found")

    try:
        day = date.fromisoformat(str(day_raw)) if day_raw else date.today() + timedelta(days=1)
    except ValueError:
        return _err("invalid_date", "date must be YYYY-MM-DD")

    # Prefer existing daily slots endpoint logic if available; otherwise return guidance.
    slots: list[dict[str, Any]] = []
    try:
        from iic_booking.equipment import api_views as eq_api

        # Many codebases expose a helper; fall back to model-level if not.
        if hasattr(eq_api, "get_daily_slots_for_equipment"):
            slots = list(eq_api.get_daily_slots_for_equipment(eq, day, user=user) or [])
    except Exception:
        slots = []

    if not slots:
        return _ok(
            {
                "equipment_id": eq.id,
                "equipment_name": eq.name,
                "date": day.isoformat(),
                "slots": [],
                "note": "Live slot enumeration requires the portal availability API. Open the equipment page for authoritative slots.",
            },
            actions=[
                {
                    "id": "open_equipment_slots",
                    "label": f"View availability — {eq.name}",
                    "href": f"/equipments/{eq.id}",
                    "enabled": True,
                }
            ],
        )

    return _ok(
        {
            "equipment_id": eq.id,
            "equipment_name": eq.name,
            "date": day.isoformat(),
            "slots": slots[:40],
        },
        actions=[
            {
                "id": f"book_slot_{i}",
                "label": f"Book {s.get('start') or s.get('start_time') or 'slot'}",
                "href": f"/book-equipment?equipment={eq.id}&date={day.isoformat()}",
                "enabled": True,
                "hint": "Opens portal booking with prefilled equipment/date; confirmation uses normal booking APIs.",
            }
            for i, s in enumerate(slots[:5])
        ],
    )


def _search_bookings(*, arguments: dict, user) -> dict:
    from iic_booking.equipment.models import Booking

    status_filter = (arguments.get("status") or "").strip().upper()
    qs = Booking.objects.filter(user=user).select_related("equipment").order_by("-start_datetime")[:30]
    if status_filter:
        qs = [b for b in qs if str(getattr(b, "status", "")).upper() == status_filter]
    rows = []
    for b in qs[:20]:
        eq = getattr(b, "equipment", None)
        rows.append(
            {
                "booking_id": b.id,
                "equipment": getattr(eq, "name", None),
                "status": getattr(b, "status", None),
                "start": getattr(b, "start_datetime", None).isoformat()
                if getattr(b, "start_datetime", None)
                else None,
                "end": getattr(b, "end_datetime", None).isoformat()
                if getattr(b, "end_datetime", None)
                else None,
                "url": f"/my-bookings?booking={b.id}",
            }
        )
    return _ok(
        rows,
        actions=[
            {
                "id": f"open_booking_{r['booking_id']}",
                "label": f"View booking #{r['booking_id']}",
                "href": r["url"],
                "enabled": True,
            }
            for r in rows[:5]
        ],
    )


def _get_wallet(*, arguments: dict, user) -> dict:
    _ = arguments
    try:
        from iic_booking.users.models import Wallet

        wallet = Wallet.objects.filter(user=user).first()
        if not wallet:
            return _ok({"balance": None, "note": "No wallet found for this user."})
        balance = getattr(wallet, "balance", None)
        return _ok(
            {
                "balance": str(balance) if balance is not None else None,
                "currency": getattr(wallet, "currency", "INR"),
            },
            actions=[{"id": "open_wallet", "label": "Open Wallet", "href": "/wallet", "enabled": True}],
        )
    except Exception as exc:
        return _err("wallet_unavailable", f"Wallet lookup failed: {exc}")


def _search_documentation(*, arguments: dict, user) -> dict:
    from iic_booking.research_copilot.services import rag as rag_svc
    from iic_booking.research_copilot.services.context_builder import build_context

    query = str(arguments.get("query") or arguments.get("q") or "").strip()
    if len(query) < 2:
        return _err("query_too_short", "Provide a documentation query")
    ctx = build_context(user)
    retrieval = rag_svc.retrieve(
        query=query,
        role_bucket=ctx.role_bucket,
        department_id=ctx.department_id,
        user=user,
        conversation=None,
    )
    return _ok(
        {
            "intent": retrieval.intent,
            "low_confidence": retrieval.low_confidence,
            "citations": rag_svc.citations_as_dicts(retrieval.citations),
            "context_preview": (retrieval.context_block or "")[:1200],
        }
    )


def _recommend_software(*, arguments: dict, user) -> dict:
    """Reuse R6 AnalysisSoftwareCatalog / EquipmentAnalysisSoftware — no new mapping system."""
    _ = user
    from iic_booking.remote_analysis.catalog_models import AnalysisSoftwareCatalog, EquipmentAnalysisSoftware

    equipment_id = arguments.get("equipment_id")
    query = str(arguments.get("query") or arguments.get("q") or "").strip().lower()
    rows: list[dict] = []

    if equipment_id:
        qs = (
            EquipmentAnalysisSoftware.objects.filter(equipment_id=int(equipment_id), catalog__is_active=True)
            .select_related("catalog")
            .order_by("sort_order", "catalog__name")
        )
        for i, row in enumerate(qs[:12]):
            cat = row.catalog
            # Heuristic stars: default first, then sort order.
            stars = 5 if row.is_default else max(2, 5 - min(i, 3))
            rows.append(
                {
                    "software_id": cat.id,
                    "name": cat.name,
                    "slug": cat.slug,
                    "is_default": row.is_default,
                    "stars": stars,
                    "description": (getattr(cat, "description", None) or "")[:240],
                }
            )
    else:
        qs = AnalysisSoftwareCatalog.objects.filter(is_active=True).order_by("name")
        if query:
            qs = qs.filter(name__icontains=query)
        for i, cat in enumerate(qs[:12]):
            rows.append(
                {
                    "software_id": cat.id,
                    "name": cat.name,
                    "slug": cat.slug,
                    "is_default": False,
                    "stars": 4 if i == 0 else 3,
                    "description": (getattr(cat, "description", None) or "")[:240],
                }
            )

    actions = [
        {
            "id": "open_software_catalog",
            "label": "Open Software Catalog",
            "href": "/remote-analysis/software-catalog",
            "enabled": True,
        }
    ]
    if equipment_id:
        actions.insert(
            0,
            {
                "id": "analyze_data",
                "label": "Analyze Data",
                "href": "/my-bookings",
                "enabled": True,
                "hint": "Open a completed booking to start Remote Analysis with recommended software.",
            },
        )
    return _ok(rows, actions=actions)


def _prepare_create_booking(*, arguments: dict, user) -> dict:
    equipment_id = arguments.get("equipment_id")
    day = arguments.get("date") or ""
    href = "/book-equipment"
    params = []
    if equipment_id:
        params.append(f"equipment={equipment_id}")
    if day:
        params.append(f"date={day}")
    if params:
        href = href + "?" + "&".join(params)
    return _ok(
        {
            "requires_confirmation": True,
            "message": "I can open the booking flow with these details. Booking is confirmed only after the portal booking API succeeds under your permissions.",
            "equipment_id": equipment_id,
            "date": day,
        },
        actions=[
            {
                "id": "book_equipment",
                "label": "Book Equipment",
                "href": href,
                "enabled": True,
                "hint": "Uses existing booking APIs; Copilot does not bypass authorization.",
            }
        ],
    )


def _prepare_cancel_booking(*, arguments: dict, user) -> dict:
    booking_id = arguments.get("booking_id")
    if not booking_id:
        return _err("missing_booking_id", "booking_id is required")
    from iic_booking.equipment.models import Booking

    try:
        booking = Booking.objects.select_related("equipment").get(pk=int(booking_id), user=user)
    except Booking.DoesNotExist:
        return _err("booking_not_found", "Booking not found for this user")
    return _ok(
        {
            "requires_confirmation": True,
            "booking_id": booking.id,
            "equipment": getattr(booking.equipment, "name", None),
            "status": getattr(booking, "status", None),
            "message": "Cancellation uses the existing portal cancellation API and policy. Confirm in My Bookings.",
        },
        actions=[
            {
                "id": "cancel_booking",
                "label": "Cancel Booking",
                "href": f"/my-bookings?booking={booking.id}&action=cancel",
                "enabled": True,
                "hint": "Opens booking details for policy-aware cancellation.",
            }
        ],
    )


def _prepare_launch_remote_analysis(*, arguments: dict, user) -> dict:
    booking_id = arguments.get("booking_id")
    if not booking_id:
        return _err("missing_booking_id", "booking_id is required")
    from iic_booking.equipment.models import Booking

    try:
        booking = Booking.objects.get(pk=int(booking_id), user=user)
    except Booking.DoesNotExist:
        return _err("booking_not_found", "Booking not found for this user")
    return _ok(
        {
            "requires_confirmation": True,
            "booking_id": booking.id,
            "message": "Full desktop Remote Analysis continues through Analysis Workspace.",
        },
        actions=[
            {
                "id": "launch_remote_analysis",
                "label": "Open Analysis Workspace",
                "href": f"/analysis-workspace/{booking.id}",
                "enabled": True,
            }
        ],
    )


def _create_support_ticket(*, arguments: dict, user) -> dict:
    _ = (arguments, user)
    return _ok(
        {"requires_confirmation": True, "message": "Open Tickets to create a support request with conversation context."},
        actions=[{"id": "create_support_ticket", "label": "Open Tickets", "href": "/tickets", "enabled": True}],
    )


_HANDLERS = {
    "search_equipment": _search_equipment,
    "search_slots": _search_slots,
    "search_bookings": _search_bookings,
    "get_wallet": _get_wallet,
    "search_documentation": _search_documentation,
    "recommend_software": _recommend_software,
    "create_booking": _prepare_create_booking,
    "cancel_booking": _prepare_cancel_booking,
    "create_support_ticket": _create_support_ticket,
    "launch_remote_analysis": _prepare_launch_remote_analysis,
}


def execute_tool(*, name: str, arguments: dict, user) -> dict:
    """Execute a registered tool. Mutating tools only prepare authorized portal action cards."""
    handler = _HANDLERS.get(name)
    if not handler:
        return _err("unknown_tool", f"Tool '{name}' is not registered")
    try:
        return handler(arguments=arguments or {}, user=user)
    except Exception as exc:  # noqa: BLE001 — tool boundary
        return _err("tool_failed", f"Tool '{name}' failed: {exc}")


def enrich_actions_from_message(*, user, text: str, base_actions: list[dict] | None = None) -> list[dict]:
    """Heuristic action enrichment for common intents (safe navigation cards)."""
    actions = list(base_actions or [])
    lower = (text or "").lower()
    seen = {a.get("id") for a in actions}

    def add(action: dict) -> None:
        if action["id"] not in seen:
            actions.insert(0, action)
            seen.add(action["id"])

    if any(w in lower for w in ("book", "slot", "availability", "sem", "fesem", "tem", "xrd")):
        add({"id": "book_equipment", "label": "Book Equipment", "href": "/book-equipment", "enabled": True})
        add({"id": "open_equipments", "label": "Browse Equipments", "href": "/equipments", "enabled": True})
    if any(w in lower for w in ("software", "digitalmicrograph", "imagej", "origin", "matlab", "analyze")):
        add(
            {
                "id": "recommend_software",
                "label": "Software Catalog",
                "href": "/remote-analysis/software-catalog",
                "enabled": True,
            }
        )
    if any(w in lower for w in ("my booking", "upcoming", "cancel")):
        add({"id": "open_my_bookings", "label": "My Bookings", "href": "/my-bookings", "enabled": True})
    if "wallet" in lower:
        add({"id": "open_wallet", "label": "Open Wallet", "href": "/wallet", "enabled": True})
    return actions

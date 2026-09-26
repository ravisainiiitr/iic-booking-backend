"""Phase A deterministic read tools — wrap existing portal domain data."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from iic_booking.equipment.models import Equipment
from iic_booking.research_copilot.services.v2 import flag
from iic_booking.research_copilot.services.v2.datetime_resolver import DateWindow, resolve_date_window
from iic_booking.research_copilot.services.v2.equipment_resolver import resolve_equipment
from iic_booking.research_copilot.services.v2.response_builder import (
    build_response,
    clarify_equipment_markdown,
    equipment_markdown,
    slots_markdown,
)


def _slot_cache_key(equipment_id: int, start, end) -> str:
    return f"copilot_slots:{equipment_id}:{start}:{end}"


def search_available_slots(*, user, text: str, equipment_id: int | None = None, context_equipment_id: int | None = None) -> dict:
    if not flag("COPILOT_AVAILABILITY", True):
        return build_response(kind="ERROR", content="Availability tools are disabled.", escalate=False)

    resolved = resolve_equipment(text=text, user=user, context_equipment_id=equipment_id or context_equipment_id)
    if resolved.confidence == "AMBIGUOUS":
        cards = [
            {
                "type": "equipment_choice",
                "title": "Select equipment",
                "items": [{"id": c.id, "name": c.name, "href": c.url} for c in resolved.candidates],
            }
        ]
        actions = [{"id": f"eq_{c.id}", "label": c.name, "href": c.url, "enabled": True} for c in resolved.candidates[:5]]
        return build_response(
            kind="CLARIFICATION",
            content=clarify_equipment_markdown(resolved.candidates),
            cards=cards,
            actions=actions,
            metadata={"equipment_resolution": resolved.confidence},
        )
    if resolved.confidence == "NOT_FOUND" or not resolved.equipment_id:
        return build_response(
            kind="CLARIFICATION",
            content="I could not identify which equipment you mean. Try a name like **FESEM**, **PXRD**, or open **Equipments**.",
            actions=[{"id": "open_equipments", "label": "Browse equipment", "href": "/equipments", "enabled": True}],
        )

    from iic_booking.research_copilot.services.v2.slot_availability import find_bookable_slots

    window: DateWindow = resolve_date_window(text)
    eq = Equipment.objects.filter(pk=resolved.equipment_id).first()
    name = eq.name if eq else (resolved.equipment_name or "Equipment")
    signed_in = bool(user is not None and getattr(user, "is_authenticated", False))

    ttl = int(getattr(settings, "COPILOT_AVAILABILITY_CACHE_TTL_SECONDS", 45) or 45)
    viewer = f"u{user.pk}" if signed_in else "anon"
    ck = _slot_cache_key(resolved.equipment_id, window.start_date.isoformat(), window.end_date.isoformat())
    ck = f"{ck}:{viewer}:{window.after_time or ''}"
    cached = cache.get(ck)
    if cached is not None:
        lookup_data = cached
    else:
        lookup = find_bookable_slots(
            user=user if signed_in else None,
            equipment_id=resolved.equipment_id,
            start_date=window.start_date,
            end_date=window.end_date,
            after_time=window.after_time,
        )
        lookup_data = {
            "ok": lookup.ok,
            "rows": lookup.rows,
            "message": lookup.message,
            "error": lookup.error,
            "bookable_equipment": lookup.bookable_equipment,
            "slot_window_max_date": lookup.slot_window_max_date,
        }
        cache.set(ck, lookup_data, ttl)

    equipment_href = f"/equipment/{resolved.equipment_id}"
    book_href = f"/book-equipment?equipment_id={resolved.equipment_id}"
    if not lookup_data.get("ok"):
        return build_response(
            kind="LIVE_DATA",
            content=lookup_data.get("message") or "Slot availability could not be loaded.",
            actions=[{"id": "view_equipment", "label": f"View {name}", "href": equipment_href, "enabled": True}],
            metadata={"equipment_id": resolved.equipment_id, "error": lookup_data.get("error"), "deterministic": True},
        )

    rows = lookup_data.get("rows") or []
    lowered = (text or "").lower()
    earliest = "earliest" in lowered or "first available" in lowered or "next available" in lowered
    display_rows = rows[:5] if earliest else rows[:12]

    content = slots_markdown(equipment_name=name, rows=display_rows, window_label=window.label)
    if not lookup_data.get("bookable_equipment", True) and lookup_data.get("message"):
        content = lookup_data["message"]
    elif not display_rows and lookup_data.get("slot_window_max_date"):
        content += (
            f"\n\nBooking for your account currently opens up to **{lookup_data['slot_window_max_date']}**; "
            "later dates appear once the slot window opens."
        )

    cards = [
        {
            "type": "slots",
            "title": f"{name} — Available",
            "window": window.label,
            "equipment_id": resolved.equipment_id,
            "equipment_name": name,
            "items": display_rows,
            "can_book": signed_in,
        }
    ]
    actions: list[dict[str, Any]] = [
        {"id": "view_equipment", "label": f"View {name}", "href": equipment_href, "enabled": True},
    ]
    if signed_in:
        actions.append({"id": "book_equipment", "label": "Open booking page", "href": book_href, "enabled": True})
        for row in display_rows[:3]:
            start_local = timezone.localtime(datetime.fromisoformat(row["start"]))
            actions.append(
                {
                    "id": f"book_slot_{row['slot_id']}",
                    "label": f"Book {start_local.strftime('%a %d %b %H:%M')}",
                    "type": "copilot_prepare_booking",
                    "enabled": True,
                    "requires_confirmation": True,
                    "payload": {"equipment_id": resolved.equipment_id, "slot_ids": [row["slot_id"]]},
                }
            )
    else:
        actions.append(
            {"id": "sign_in_to_book", "label": "Sign in to book", "href": "/auth", "enabled": True}
        )
    return build_response(
        kind="LIVE_DATA",
        content=content,
        cards=cards,
        actions=actions,
        metadata={
            "equipment_id": resolved.equipment_id,
            "equipment_name": name,
            "equipment_resolution": resolved.confidence,
            "window": window.label,
            "slot_count": len(display_rows),
            "earliest_slot_id": display_rows[0]["slot_id"] if display_rows else None,
            "deterministic": True,
        },
    )


def search_equipment_catalog(*, user, text: str) -> dict:
    if not flag("COPILOT_EQUIPMENT_SEARCH", True):
        return build_response(kind="ERROR", content="Equipment search is disabled.")
    from iic_booking.research_copilot.services.structured_search import search_equipment

    hits = search_equipment(query=text[:120], limit=8)
    # Capability keywords
    lower = (text or "").lower()
    if not hits and any(x in lower for x in ("eds", "edx", "elemental", "morphology", "nanoparticle", "xrd", "sem")):
        hits = search_equipment(query="EDS" if "eds" in lower or "elemental" in lower else text[:80], limit=8)
        if not hits:
            hits = search_equipment(query="SEM" if "morphology" in lower or "sem" in lower else "XRD", limit=8)

    rows = [
        {
            "id": int(h.source_id.split(":")[1]) if ":" in h.source_id else None,
            "name": h.title,
            "snippet": h.snippet,
            "href": h.url,
            "location": "",
        }
        for h in hits
    ]
    actions = [{"id": f"eq_{r['id']}", "label": r["name"], "href": r["href"], "enabled": True} for r in rows if r.get("id")]
    cards = [{"type": "equipment_list", "title": "IIC equipment", "items": rows}]
    return build_response(
        kind="LIVE_DATA",
        content=equipment_markdown(rows),
        cards=cards,
        actions=actions[:6],
        metadata={"deterministic": True, "count": len(rows)},
    )


def estimate_cost(*, user, text: str, context_equipment_id: int | None = None) -> dict:
    if not flag("COPILOT_PRICING", True):
        return build_response(kind="ERROR", content="Pricing tools are disabled.")
    from iic_booking.research_copilot.services import tools as tools_svc

    resolved = resolve_equipment(text=text, user=user, context_equipment_id=context_equipment_id)
    if resolved.confidence in {"AMBIGUOUS", "NOT_FOUND"} or not resolved.equipment_id:
        if resolved.confidence == "AMBIGUOUS":
            return build_response(kind="CLARIFICATION", content=clarify_equipment_markdown(resolved.candidates))
        return build_response(kind="CLARIFICATION", content="Which equipment should I estimate? e.g. FESEM or PXRD.")

    result = tools_svc._estimate_booking_cost(arguments={"equipment_id": resolved.equipment_id}, user=user)
    data = (result or {}).get("data") or {}
    est = data.get("estimate")
    name = data.get("equipment_name") or resolved.equipment_name

    wallet_bal = None
    sufficient = None
    if user is not None and getattr(user, "is_authenticated", False):
        w = tools_svc._get_wallet(arguments={}, user=user)
        wallet_bal = ((w or {}).get("data") or {}).get("balance")
        try:
            if est is not None and wallet_bal is not None:
                from decimal import Decimal

                sufficient = Decimal(str(wallet_bal)) >= Decimal(str(est))
        except Exception:  # noqa: BLE001
            sufficient = None

    if est is None:
        content = f"No active charge profile found for **{name}**. Open booking to calculate the authoritative total."
    else:
        content = (
            f"**Estimated cost** for **{name}**: ₹{est:,.2f} (INR).\n\n"
            f"{data.get('note') or 'This is an ESTIMATE — portal calculate remains the final charge.'}"
        )
        if wallet_bal is not None:
            content += f"\n\n**Current wallet:** ₹{wallet_bal}"
            if sufficient is True:
                content += "\nYour balance appears sufficient for this estimate."
            elif sufficient is False:
                content += "\n**Warning:** balance may be insufficient for this estimate."

    actions = list((result or {}).get("actions") or [])
    if sufficient is False:
        actions.extend(
            [
                {"id": "recharge", "label": "Recharge wallet", "prompt": "I want to recharge my wallet.", "enabled": True, "requires_confirmation": True},
                {"id": "credit", "label": "Request wallet credit", "prompt": "Request wallet credit.", "href": "/wallet/credit-facility", "enabled": True, "requires_confirmation": True},
            ]
        )
    return build_response(
        kind="LIVE_DATA",
        content=content,
        cards=[
            {
                "type": "estimate",
                "equipment_id": resolved.equipment_id,
                "estimate": est,
                "currency": "INR",
                "wallet_balance": wallet_bal,
                "sufficient": sufficient,
                "is_estimate": True,
            }
        ],
        actions=actions,
        metadata={"equipment_id": resolved.equipment_id, "deterministic": True, "estimate": est, "wallet_balance": wallet_bal},
    )


def my_bookings(*, user) -> dict:
    if not flag("COPILOT_USER_CONTEXT", True):
        return build_response(kind="ERROR", content="User context tools are disabled.")
    if user is None or not getattr(user, "is_authenticated", False):
        return build_response(
            kind="ACTION_REQUIRED",
            content="Sign in to view your bookings.",
            actions=[{"id": "sign_in", "label": "Sign in", "href": "/auth", "enabled": True}],
        )
    from iic_booking.research_copilot.services import tools as tools_svc

    result = tools_svc._search_bookings(arguments={}, user=user)
    rows = (result or {}).get("data") or []
    if isinstance(rows, dict):
        rows = rows.get("bookings") or rows.get("results") or []
    lines = ["**Your recent bookings**", ""]
    items = []
    for r in (rows if isinstance(rows, list) else [])[:8]:
        if not isinstance(r, dict):
            continue
        lines.append(f"- #{r.get('booking_id')} {r.get('equipment') or ''} — {r.get('status')}")
        items.append(r)
    if len(lines) == 2:
        lines.append("No bookings found.")
    return build_response(
        kind="LIVE_DATA",
        content="\n".join(lines),
        cards=[{"type": "bookings", "items": items}],
        actions=list((result or {}).get("actions") or [])[:5]
        or [{"id": "my_bookings", "label": "My bookings", "href": "/my-bookings", "enabled": True}],
        metadata={"deterministic": True},
    )


def next_booking(*, user) -> dict:
    if user is None or not getattr(user, "is_authenticated", False):
        return build_response(kind="ACTION_REQUIRED", content="Sign in to view your next booking.", actions=[{"id": "sign_in", "label": "Sign in", "href": "/auth", "enabled": True}])
    from iic_booking.research_copilot.services import tools as tools_svc

    result = tools_svc._get_next_booking(arguments={}, user=user)
    data = (result or {}).get("data") or {}
    if not data or data.get("booking_id") is None:
        content = "You have no upcoming booking in portal data."
    else:
        start = data.get("start")
        try:
            start_dt = timezone.localtime(datetime.fromisoformat(start)) if start else None
        except (TypeError, ValueError):
            start_dt = None
        start_label = f"{start_dt:%a %d %b %Y, %H:%M} {start_dt.tzname()}" if start_dt else (start or "")
        content = f"**Next booking** #{data.get('booking_id')} — {data.get('equipment')} ({data.get('status')})\nStart: {start_label}"
    return build_response(kind="LIVE_DATA", content=content, actions=list((result or {}).get("actions") or []), metadata={"deterministic": True})


def wallet_balance(*, user) -> dict:
    if user is None or not getattr(user, "is_authenticated", False):
        return build_response(kind="ACTION_REQUIRED", content="Sign in to view wallet balance.", actions=[{"id": "sign_in", "label": "Sign in", "href": "/auth", "enabled": True}])
    if not flag("COPILOT_WALLET_READ", True):
        return build_response(kind="ERROR", content="Wallet read tools are disabled.")
    from iic_booking.research_copilot.services import tools as tools_svc

    result = tools_svc._get_wallet(arguments={}, user=user)
    data = (result or {}).get("data") or {}
    bal = data.get("balance")
    content = f"**Wallet balance:** ₹{bal}" if bal is not None else (data.get("note") or "No wallet found.")
    return build_response(
        kind="LIVE_DATA",
        content=content + "\n\n_Authoritative wallet actions remain on the Wallet page._",
        cards=[{"type": "wallet", "balance": bal, "currency": data.get("currency") or "INR"}],
        actions=list((result or {}).get("actions") or []),
        metadata={"deterministic": True},
    )


def wallet_transactions(*, user) -> dict:
    if user is None or not getattr(user, "is_authenticated", False):
        return build_response(kind="ACTION_REQUIRED", content="Sign in to view transactions.", actions=[{"id": "sign_in", "label": "Sign in", "href": "/auth", "enabled": True}])
    if not flag("COPILOT_WALLET_READ", True):
        return build_response(kind="ERROR", content="Wallet read tools are disabled.")
    wallet = None
    if hasattr(user, "get_accessible_wallet"):
        wallet = user.get_accessible_wallet()
    if wallet is None:
        return build_response(kind="LIVE_DATA", content="No accessible wallet found.")
    try:
        from iic_booking.users.models import SubWallet, SubWalletTransaction

        sub_ids = list(SubWallet.objects.filter(wallet=wallet).values_list("pk", flat=True)[:20])
        txs = list(
            SubWalletTransaction.objects.filter(sub_wallet_id__in=sub_ids).order_by("-created_at")[:8]
        )
    except Exception:  # noqa: BLE001
        return build_response(
            kind="LIVE_DATA",
            content="Open **Wallet** for your full statement.",
            actions=[{"id": "open_wallet", "label": "Open Wallet", "href": "/wallet", "enabled": True}],
        )
    lines = ["**Recent wallet transactions**", ""]
    items = []
    for t in txs:
        amt = getattr(t, "amount", None)
        ttype = getattr(t, "transaction_type", None) or ""
        desc = getattr(t, "description", None) or ttype or ""
        created = getattr(t, "created_at", None)
        lines.append(f"- {ttype} {amt} — {desc}")
        items.append(
            {
                "amount": str(amt) if amt is not None else None,
                "type": ttype,
                "description": desc,
                "created_at": created.isoformat() if created else None,
            }
        )
    if len(lines) == 2:
        lines.append("No transactions found.")
    return build_response(
        kind="LIVE_DATA",
        content="\n".join(lines),
        cards=[{"type": "transactions", "items": items}],
        actions=[{"id": "open_wallet", "label": "Open Wallet", "href": "/wallet", "enabled": True}],
        metadata={"deterministic": True},
    )


def credit_status(*, user) -> dict:
    if user is None or not getattr(user, "is_authenticated", False):
        return build_response(kind="ACTION_REQUIRED", content="Sign in to view credit status.", actions=[{"id": "sign_in", "label": "Sign in", "href": "/auth", "enabled": True}])
    if not flag("COPILOT_WALLET_READ", True):
        return build_response(kind="ERROR", content="Wallet read tools are disabled.")
    from iic_booking.research_copilot.services.v2.mutations import domain_bridge

    code, data = domain_bridge.call_wallet_credit_summary(user=user)
    if code >= 400:
        msg = (data or {}).get("error") or (data or {}).get("message") or "Credit facility summary unavailable."
        return build_response(
            kind="LIVE_DATA",
            content=f"{msg}\n\nOpen **Wallet → Credit Facility** for authoritative status.",
            actions=[{"id": "credit", "label": "Credit Facility", "href": "/wallet/credit-facility", "enabled": True}],
            metadata={"deterministic": True},
        )
    outstanding = data.get("outstanding_amount") or data.get("outstanding")
    eligibility = data.get("eligibility")
    content = (
        "**Wallet credit status** (portal data)\n\n"
        f"- Outstanding: {outstanding if outstanding is not None else '—'}\n"
        f"- Eligibility: {eligibility if eligibility is not None else '—'}\n\n"
        "New credit cannot be requested while a previous credit remains outstanding under portal rules. "
        "Main Administrator approves all credit."
    )
    return build_response(
        kind="LIVE_DATA",
        content=content,
        cards=[{"type": "credit_status", "outstanding": outstanding, "eligibility": eligibility, "summary": data}],
        actions=[
            {"id": "credit", "label": "Credit Facility", "href": "/wallet/credit-facility", "enabled": True},
            {"id": "request_credit", "label": "Request credit", "prompt": "Request wallet credit.", "enabled": True, "requires_confirmation": True},
        ],
        metadata={"deterministic": True},
    )


def wallet_spend_month(*, user) -> dict:
    """Rough month debit total from SubWalletTransaction — labeled as portal-derived, not LLM."""
    if user is None or not getattr(user, "is_authenticated", False):
        return build_response(kind="ACTION_REQUIRED", content="Sign in required.", actions=[{"id": "sign_in", "label": "Sign in", "href": "/auth", "enabled": True}])
    from django.db.models import Sum
    from django.utils import timezone

    from iic_booking.users.models import SubWallet, SubWalletTransaction

    wallet = user.get_accessible_wallet() if hasattr(user, "get_accessible_wallet") else None
    if not wallet:
        return build_response(kind="LIVE_DATA", content="No accessible wallet found.")
    start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    sub_ids = list(SubWallet.objects.filter(wallet=wallet).values_list("pk", flat=True))
    total = (
        SubWalletTransaction.objects.filter(sub_wallet_id__in=sub_ids, transaction_type="debit", created_at__gte=start).aggregate(
            s=Sum("amount")
        )["s"]
        or 0
    )
    return build_response(
        kind="LIVE_DATA",
        content=f"**Wallet debits this month** (portal ledger): ₹{total}\n\nOpen Wallet for the full statement.",
        cards=[{"type": "spend_summary", "period": "month", "debit_total": str(total)}],
        actions=[{"id": "open_wallet", "label": "Open Wallet", "href": "/wallet", "enabled": True}],
        metadata={"deterministic": True},
    )


def sample_or_results(*, user, text: str, which: str) -> dict:
    if user is None or not getattr(user, "is_authenticated", False):
        return build_response(kind="ACTION_REQUIRED", content="Sign in required.", actions=[{"id": "sign_in", "label": "Sign in", "href": "/auth", "enabled": True}])
    from iic_booking.research_copilot.services import tools as tools_svc

    if which == "sample":
        result = tools_svc._get_sample_status(arguments={}, user=user)
    else:
        result = tools_svc._get_booking_results(arguments={}, user=user)
    data = (result or {}).get("data") or {}
    content = f"```json\n{data}\n```" if data else ((result or {}).get("error") or {}).get("message") or "No data."
    if isinstance(data, dict) and data:
        # Friendlier
        content = "**Status**\n\n" + "\n".join(f"- {k}: {v}" for k, v in list(data.items())[:12])
    return build_response(kind="LIVE_DATA", content=str(content)[:4000], actions=list((result or {}).get("actions") or []), metadata={"deterministic": True})


def ra_status(*, user) -> dict:
    if user is None or not getattr(user, "is_authenticated", False):
        return build_response(kind="ACTION_REQUIRED", content="Sign in required.", actions=[{"id": "sign_in", "label": "Sign in", "href": "/auth", "enabled": True}])
    from iic_booking.equipment.models import Booking

    b = Booking.objects.filter(user=user).order_by("-booking_id").first()
    if not b:
        return build_response(kind="LIVE_DATA", content="No bookings found to check Remote Analysis status.")
    try:
        from iic_booking.equipment.remote_analysis_integration.eligibility import BookingAnalysisEligibilityService

        elig = BookingAnalysisEligibilityService().evaluate(b)
        eligible = getattr(elig, "eligible", None)
        reason = getattr(elig, "reason", None) or getattr(elig, "message", "") or ""
        content = f"**Remote Analysis** for booking #{b.booking_id}:\n\n- Eligible: {eligible}\n- Detail: {reason or '—'}"
    except Exception as exc:  # noqa: BLE001
        content = f"Could not evaluate Remote Analysis eligibility ({exc}). Open Analysis Workspace for details."
    return build_response(
        kind="LIVE_DATA",
        content=content,
        actions=[{"id": "open_ra", "label": "Analysis Workspace", "href": f"/analysis-workspace/{b.booking_id}", "enabled": True}],
        metadata={"deterministic": True},
    )


def affiliations(*, user) -> dict:
    if user is None or not getattr(user, "is_authenticated", False):
        return build_response(kind="ACTION_REQUIRED", content="Sign in required.", actions=[{"id": "sign_in", "label": "Sign in", "href": "/auth", "enabled": True}])
    lines = ["**Your affiliations / faculty**", ""]
    try:
        from iic_booking.users.models.channel_i_identity import UserAffiliation

        rows = UserAffiliation.objects.filter(user=user).select_related("faculty")[:10]
        for a in rows:
            fac = getattr(a, "faculty", None)
            lines.append(f"- {getattr(fac, 'name', None) or getattr(fac, 'email', None) or a}")
        if len(lines) == 2:
            lines.append("No affiliations on file. Open Profile for joining requests.")
    except Exception:  # noqa: BLE001
        lines.append("Open **Profile** to view faculty affiliations.")
    return build_response(
        kind="LIVE_DATA",
        content="\n".join(lines),
        actions=[{"id": "profile", "label": "Profile", "href": "/profile", "enabled": True}],
        metadata={"deterministic": True},
    )


def pending_actions(*, user) -> dict:
    if user is None or not getattr(user, "is_authenticated", False):
        return build_response(kind="ACTION_REQUIRED", content="Sign in to see pending actions.", actions=[{"id": "sign_in", "label": "Sign in", "href": "/auth", "enabled": True}])
    items = []
    # Upcoming booking reminder
    try:
        from iic_booking.research_copilot.services import tools as tools_svc

        nb = tools_svc._get_next_booking(arguments={}, user=user)
        data = (nb or {}).get("data") or {}
        if data.get("booking_id"):
            items.append({"id": "next_booking", "label": f"Upcoming booking #{data.get('booking_id')}", "href": "/my-bookings"})
    except Exception:  # noqa: BLE001
        pass
    items.append({"id": "wallet", "label": "Review wallet / recharge if needed", "href": "/wallet"})
    items.append({"id": "tickets", "label": "Check support tickets", "href": "/tickets"})
    lines = ["**Pending / useful next steps**", ""] + [f"- {i['label']}" for i in items]
    return build_response(
        kind="LIVE_DATA",
        content="\n".join(lines),
        cards=[{"type": "pending_actions", "items": items}],
        actions=[{"id": i["id"], "label": i["label"], "href": i["href"], "enabled": True} for i in items],
        metadata={"deterministic": True},
    )


def docs_rag(*, user, text: str) -> dict:
    if not flag("COPILOT_RAG", True):
        return build_response(kind="ERROR", content="RAG is disabled.")
    from iic_booking.research_copilot.services import rag as rag_svc
    from iic_booking.research_copilot.services.context_builder import build_context

    ctx = build_context(user)
    retrieval = rag_svc.retrieve(
        query=text,
        role_bucket=ctx.role_bucket if user else "public",
        department_id=ctx.department_id if user else None,
        user=user,
        conversation=None,
    )
    cites = rag_svc.citations_as_dicts(retrieval.citations)
    if not cites and not (retrieval.context_block or "").strip():
        return build_response(
            kind="ANSWER",
            content="I could not find published IIC documentation for that. Try a more specific equipment name, or open **Tickets**.",
            escalate=True,
            metadata={"deterministic": True, "rag": True},
        )
    # Prefer citation snippets over LLM
    lines = ["**From IIC knowledge documents**", ""]
    for c in cites[:5]:
        lines.append(f"- **{c.get('title')}**: {(c.get('snippet') or '')[:220]}")
    actions = []
    for c in cites[:3]:
        if c.get("url"):
            actions.append({"id": f"src_{c.get('source_id')}", "label": c.get("title") or "Source", "href": c["url"], "enabled": True})
    return build_response(
        kind="ANSWER",
        content="\n".join(lines),
        actions=actions,
        metadata={"deterministic": True, "rag": True, "citations": cites},
    )


MANUAL_NOT_FOUND_MARKER = "NOT_IN_MANUAL"

_MANUAL_SYSTEM_PROMPT = (
    "You are IIC Research Copilot. Answer the user's question about the instrument \"{name}\" "
    "using ONLY the numbered manual excerpts provided.\n"
    "Rules:\n"
    "- Every factual statement must end with its excerpt number in square brackets, e.g. [2].\n"
    "- Never invent values, settings, limits, part numbers or procedures that are not in the excerpts.\n"
    "- Reproduce safety warnings faithfully; do not soften them.\n"
    "- If the excerpts do not answer the question, reply with exactly " + MANUAL_NOT_FOUND_MARKER + " and nothing else.\n"
    "- Be concise: at most 180 words; use short numbered steps for procedures."
)


def _page_label(p: dict) -> str:
    page, end = p.get("page"), p.get("page_end")
    if page and end and end != page:
        return f"pp. {page}-{end}"
    if page:
        return f"p. {page}"
    return ""


def _manual_citations(passages: list[dict], *, signed_in: bool) -> list[dict]:
    out = []
    for i, p in enumerate(passages, 1):
        can_open = bool(p.get("has_file") and signed_in)
        out.append(
            {
                "n": i,
                "source_id": p["document_id"],
                "document_id": p["document_id"],
                "title": p.get("title") or "Manual",
                "snippet": (p.get("content") or "")[:300],
                "page": p.get("page"),
                "page_end": p.get("page_end"),
                "page_label": _page_label(p),
                "has_file": can_open,
                "file_endpoint": f"/api/v1/research-copilot/knowledge/documents/{p['document_id']}/file/" if can_open else "",
                "url": "",
                "category": "operator_manual",
                "source_type": "manual",
                "score": round(float(p.get("score") or 0.0), 3),
            }
        )
    return out


def _sources_block(citations: list[dict], used: set[int] | None = None) -> str:
    lines = ["", "**Sources**"]
    for c in citations:
        if used is not None and c["n"] not in used:
            continue
        label = c.get("page_label")
        lines.append(f"[{c['n']}] {c['title']}" + (f", {label}" if label else ""))
    return "\n".join(lines) if len(lines) > 2 else ""


def _equipment_profile(eq) -> list[str]:
    lines: list[str] = []
    if getattr(eq, "important_instruction", None):
        lines.append(f"**Important instructions:** {str(eq.important_instruction).strip()[:800]}")
    if getattr(eq, "description", None):
        lines.append(f"**Description:** {str(eq.description).strip()[:800]}")
    try:
        specs = list(eq.equipment_specifications.all().order_by("equipment_specification_id")[:12])
    except Exception:  # noqa: BLE001
        specs = []
    if specs:
        lines.append("**Specifications:**")
        for s in specs:
            lines.append(f"- {s.spec_key}: {str(s.spec_value).strip()[:200]}")
    return lines


def _resolve_manual_equipment(*, user, text: str, context_equipment_id: int | None):
    """Returns (equipment_id, equipment_name, resolution)."""
    from iic_booking.research_copilot.services import rag as rag_svc

    resolved = resolve_equipment(text=text, user=user, context_equipment_id=context_equipment_id)
    if resolved.confidence != "AMBIGUOUS":
        return resolved.equipment_id, resolved.equipment_name, resolved
    lower = (text or "").lower()
    named = [
        c
        for c in resolved.candidates
        if (c.name and c.name.lower() in lower) or (c.code and len(c.code) >= 4 and c.code.lower() in lower)
    ]
    if len(named) == 1:
        return named[0].id, named[0].name, resolved
    with_manual = [c for c in resolved.candidates if rag_svc.equipment_has_manual(equipment_id=c.id)]
    if len(with_manual) == 1:
        return with_manual[0].id, with_manual[0].name, resolved
    return None, None, resolved


def _generate_manual_answer(*, user, name: str, text: str, passages: list[dict]) -> tuple[str | None, str]:
    """Returns (answer_text or None, reason) where reason explains a None answer."""
    from iic_booking.research_copilot.services.inference_concurrency import CopilotBusyError, acquire_generation_slot
    from iic_booking.research_copilot.services.llm_gateway import default_max_tokens, get_gateway
    from iic_booking.research_copilot.throttles import consume_llm_quota

    if not flag("COPILOT_MANUAL_LLM", True):
        return None, "llm_disabled"
    ok, _msg = consume_llm_quota(user=user)
    if not ok:
        return None, "quota"
    # Small CPU-hosted model behind a request-scoped timeout: keep the prompt short.
    excerpts = []
    for i, p in enumerate(passages[:4], 1):
        label = _page_label(p)
        head = f"[{i}] {p.get('title') or 'Manual'}" + (f" ({label})" if label else "")
        excerpts.append(f"{head}\n{(p.get('content') or '')[:1100]}")
    messages = [
        {"role": "system", "content": _MANUAL_SYSTEM_PROMPT.format(name=name)},
        {"role": "user", "content": "Manual excerpts:\n\n" + "\n\n".join(excerpts) + f"\n\nQuestion: {text}"},
    ]
    try:
        with acquire_generation_slot(wait=False):
            result = get_gateway().generate(messages, max_tokens=min(default_max_tokens(), 450))
    except CopilotBusyError:
        return None, "busy"
    except Exception:  # noqa: BLE001
        return None, "error"
    answer = (getattr(result, "text", "") or "").strip() if result else ""
    if not answer or getattr(result, "error_category", ""):
        return None, "unavailable"
    return answer, ""


def equipment_manual_answer(*, user, text: str, context_equipment_id: int | None = None) -> dict | None:
    """
    Grounded answer from the uploaded manual(s) of one equipment, with page citations.

    Returns None when the question does not name a known equipment, so the caller can fall back
    to general document search.
    """
    import re

    from iic_booking.research_copilot.services import rag as rag_svc
    from iic_booking.research_copilot.services.context_builder import build_context

    if not flag("COPILOT_RAG", True):
        return None
    eid, name, resolved = _resolve_manual_equipment(user=user, text=text, context_equipment_id=context_equipment_id)
    if eid is None and resolved.confidence == "AMBIGUOUS":
        return build_response(
            kind="CLARIFICATION",
            content=clarify_equipment_markdown(resolved.candidates),
            cards=[
                {
                    "type": "equipment_choice",
                    "title": "Select equipment",
                    "items": [{"id": c.id, "name": c.name, "href": c.url} for c in resolved.candidates],
                }
            ],
            actions=[
                {"id": f"eq_{c.id}", "label": c.name, "prompt": f"{c.name}: {text}", "enabled": True}
                for c in resolved.candidates[:5]
            ],
            metadata={"deterministic": True, "equipment_resolution": resolved.confidence},
        )
    if eid is None:
        return None

    from iic_booking.research_copilot.services.v2.equipment_resolver import _qs_visible

    eq = _qs_visible(user).filter(pk=eid).first()
    if eq is None:
        return None
    name = eq.name or name or "Equipment"
    signed_in = bool(user is not None and getattr(user, "is_authenticated", False))
    ctx = build_context(user if signed_in else None)
    passages = rag_svc.manual_passages(
        query=text,
        equipment_id=eid,
        role_bucket=ctx.role_bucket,
        department_id=ctx.department_id,
    )
    equipment_href = f"/equipment/{eid}"
    base_actions = [
        {"id": "open_equipment", "label": "Equipment page", "href": equipment_href, "enabled": True},
        {"id": "find_slots", "label": "Find slots", "prompt": f"Search available slots for {name} this week", "enabled": True},
    ]
    meta = {"deterministic": True, "rag": True, "manual": True, "equipment_id": eid, "equipment_name": name}

    if not passages:
        profile = _equipment_profile(eq)
        lines = [f"No operating manual has been published for **{name}** in Copilot yet."]
        if profile:
            lines += ["", "Here is what the equipment page lists:", "", *profile]
        lines += ["", "For anything not covered here, contact the facility in-charge or raise a ticket."]
        return build_response(
            kind="ANSWER",
            content="\n".join(lines),
            actions=base_actions,
            escalate=not profile,
            metadata={**meta, "manual_found": False, "citations": []},
        )

    citations = _manual_citations(passages, signed_in=signed_in)
    card = {"type": "manual_sources", "title": f"{name} manual", "equipment_id": eid, "items": citations}
    answer, reason = _generate_manual_answer(user=user, name=name, text=text, passages=passages)

    if answer is not None and MANUAL_NOT_FOUND_MARKER not in answer:
        used = {int(n) for n in re.findall(r"\[(\d{1,2})\]", answer) if 1 <= int(n) <= len(citations)}
        answer = re.sub(
            r"\[(\d{1,2})\]",
            lambda m: m.group(0) if 1 <= int(m.group(1)) <= len(citations) else "",
            answer,
        )
        if used:
            content = answer + "\n" + _sources_block(citations, used)
            return build_response(
                kind="ANSWER",
                content=content,
                cards=[card],
                actions=base_actions,
                metadata={**meta, "manual_found": True, "llm_used": True, "citations": citations},
            )
        reason = "ungrounded"

    if answer is not None and MANUAL_NOT_FOUND_MARKER in answer:
        lines = [
            f"The published manual for **{name}** does not appear to cover that. Closest sections:",
            "",
        ]
        for c in citations[:3]:
            lines.append(f"- [{c['n']}] {c['page_label'] or c['title']}: {c['snippet'][:200]}")
        lines += ["", "Please check with the facility in-charge for anything not in the manual."]
        return build_response(
            kind="ANSWER",
            content="\n".join(lines) + "\n" + _sources_block(citations[:3]),
            cards=[card],
            actions=base_actions,
            escalate=True,
            metadata={**meta, "manual_found": True, "llm_used": True, "answer_in_manual": False, "citations": citations},
        )

    lines = [f"**From the {name} manual**", ""]
    for c in citations[:4]:
        lines.append(f"- [{c['n']}] {c['snippet'][:280].strip()}")
    if reason in {"busy", "quota"}:
        lines += ["", "_AI summaries are temporarily unavailable, so these are the most relevant manual passages._"]
    return build_response(
        kind="ANSWER",
        content="\n".join(lines) + "\n" + _sources_block(citations[:4]),
        cards=[card],
        actions=base_actions,
        metadata={**meta, "manual_found": True, "llm_used": False, "llm_skipped": reason, "citations": citations},
    )

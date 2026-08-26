"""Phase A deterministic read tools — wrap existing portal domain data."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from iic_booking.equipment.models import DailySlot, Equipment, SlotStatus
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

    window: DateWindow = resolve_date_window(text)
    eq = Equipment.objects.filter(pk=resolved.equipment_id).first()
    name = eq.name if eq else (resolved.equipment_name or "Equipment")

    ttl = int(getattr(settings, "COPILOT_AVAILABILITY_CACHE_TTL_SECONDS", 45) or 45)
    ck = _slot_cache_key(resolved.equipment_id, window.start_date.isoformat(), window.end_date.isoformat())
    cached = cache.get(ck)
    if cached is not None:
        rows = cached
    else:
        qs = (
            DailySlot.objects.filter(
                slot_master__equipment_id=resolved.equipment_id,
                date__gte=window.start_date,
                date__lte=window.end_date,
                status=SlotStatus.AVAILABLE,
                booking__isnull=True,
            )
            .select_related("slot_master")
            .order_by("start_datetime")[:80]
        )
        rows = []
        for s in qs:
            if window.after_time and s.start_datetime:
                local_t = timezone.localtime(s.start_datetime).time()
                if local_t < window.after_time:
                    continue
            rows.append(
                {
                    "slot_id": s.pk,
                    "date": s.date.isoformat() if s.date else None,
                    "start": s.start_datetime.isoformat() if s.start_datetime else None,
                    "end": s.end_datetime.isoformat() if s.end_datetime else None,
                    "status": s.status,
                }
            )
        cache.set(ck, rows, ttl)

    # earliest → keep first few; cheapest placeholder uses first (estimate separate)
    earliest = "earliest" in (text or "").lower() or "first available" in (text or "").lower()
    display_rows = rows[:5] if earliest else rows[:12]

    cards = [
        {
            "type": "slots",
            "title": f"{name} — Available",
            "window": window.label,
            "equipment_id": resolved.equipment_id,
            "items": display_rows,
        }
    ]
    actions = [
        {
            "id": "view_equipment",
            "label": f"View {name}",
            "href": f"/equipments/{resolved.equipment_id}",
            "enabled": True,
        },
        {
            "id": "book_equipment",
            "label": "Book",
            "href": f"/book-equipment?equipment={resolved.equipment_id}",
            "enabled": True,
            "requires_confirmation": True,
            "hint": "Opens portal booking — Copilot Phase A does not create bookings.",
        },
    ]
    return build_response(
        kind="LIVE_DATA",
        content=slots_markdown(equipment_name=name, rows=display_rows, window_label=window.label),
        cards=cards,
        actions=actions,
        metadata={
            "equipment_id": resolved.equipment_id,
            "equipment_name": name,
            "equipment_resolution": resolved.confidence,
            "window": window.label,
            "slot_count": len(display_rows),
            "deterministic": True,
        },
    )


def search_equipment_catalog(*, user, text: str) -> dict:
    if not flag("COPILOT_EQUIPMENT_SEARCH", True):
        return build_response(kind="ERROR", content="Equipment search is disabled.")
    from iic_booking.research_copilot.services.structured_search import search_equipment
    from iic_booking.research_copilot.services.v2.capability_map import match_capability

    hits = search_equipment(query=text[:120], limit=8)
    lower = (text or "").lower()
    if not hits:
        caps = match_capability(text)
        for cap in caps:
            for needle in cap["needles"]:
                hits = search_equipment(query=needle, limit=8)
                if hits:
                    break
            if hits:
                break
    if not hits and any(x in lower for x in ("eds", "edx", "elemental", "morphology", "nanoparticle", "xrd", "sem", "fesem")):
        for q in ("EDS", "FESEM", "XRD", "SEM"):
            if q.lower() in lower or (q == "EDS" and "elemental" in lower):
                hits = search_equipment(query=q, limit=8)
                if hits:
                    break

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
    if not rows:
        from iic_booking.research_copilot.services.v2.unanswered import log_unanswered, unanswered_response

        log_unanswered(user=user, query=text, intent="search_equipment", reason="EQUIPMENT_NOT_FOUND")
        return unanswered_response(query=text)
    return build_response(
        kind="LIVE_DATA",
        content=equipment_markdown(rows) + "\n\n_Source: Live portal equipment catalog_",
        cards=cards,
        actions=actions[:6],
        metadata={"deterministic": True, "count": len(rows), "equipment_choices": rows, "confidence": "HIGH_CONFIDENCE"},
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
        content = f"**Next booking** #{data.get('booking_id')} — {data.get('equipment')} ({data.get('status')})\nStart: {data.get('start')}"
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
        from iic_booking.research_copilot.services.v2.unanswered import log_unanswered, unanswered_response

        log_unanswered(user=user, query=text, intent="docs_rag", reason="RAG_EMPTY")
        return unanswered_response(query=text)
    # Prefer citation snippets over LLM
    lines = ["**From IIC knowledge documents** (portal RAG)", ""]
    for c in cites[:5]:
        lines.append(f"- **{c.get('title')}**: {(c.get('snippet') or '')[:220]}")
        lines.append(f"  _Source: {c.get('title')} — Live knowledge base_")
    actions = []
    for c in cites[:3]:
        if c.get("url"):
            actions.append({"id": f"src_{c.get('source_id')}", "label": c.get("title") or "Source", "href": c["url"], "enabled": True})
    return build_response(
        kind="ANSWER",
        content="\n".join(lines),
        actions=actions,
        metadata={"deterministic": True, "rag": True, "citations": cites, "confidence": "HIGH_CONFIDENCE" if cites else "MEDIUM_CONFIDENCE"},
    )


def capability_search(*, user, text: str) -> dict:
    """Map research goal → technique → live equipment catalog hits."""
    from django.db.models import Q

    from iic_booking.research_copilot.services.v2.capability_map import match_capability

    hits = match_capability(text)
    if not hits:
        from iic_booking.research_copilot.services.v2.unanswered import log_unanswered, unanswered_response

        log_unanswered(user=user, query=text, intent="capability_search", reason="NO_CAPABILITY_MATCH")
        return unanswered_response(query=text)

    lines = ["**Capability → technique → IIC equipment** (live catalog)", ""]
    cards_items = []
    actions = []
    for hit in hits[:3]:
        lines.append(f"### {hit['technique']}")
        lines.append(f"_Matched:_ {', '.join(hit['matched_phrases'][:3])}")
        q = Q()
        for n in hit["needles"]:
            q |= Q(name__icontains=n) | Q(description__icontains=n) | Q(code__icontains=n)
        eqs = list(Equipment.objects.filter(q, status="ACTIVE").order_by("name")[:6])
        if not eqs:
            lines.append("- No matching active equipment found in the portal catalog for these needles.")
        for eq in eqs:
            dept = getattr(getattr(eq, "internal_department", None), "name", None) or ""
            loc = getattr(eq, "location", None) or ""
            lines.append(f"- **{eq.name}** — {dept} {('· ' + loc) if loc else ''}")
            lines.append(f"  _Why:_ matches technique **{hit['technique']}** via catalog keywords.")
            cards_items.append(
                {
                    "id": eq.pk,
                    "name": eq.name,
                    "technique": hit["technique"],
                    "department": dept,
                    "location": loc,
                    "href": f"/equipments/{eq.pk}",
                }
            )
            actions.append({"id": f"eq_{eq.pk}", "label": eq.name, "href": f"/equipments/{eq.pk}", "prompt": f"Find available slots for {eq.name}", "enabled": True})
        lines.append("")
    return build_response(
        kind="LIVE_DATA",
        content="\n".join(lines),
        cards=[{"type": "equipment_list", "title": "Suggested instruments", "items": cards_items}],
        actions=actions[:8],
        metadata={"deterministic": True, "capability": True, "confidence": "HIGH_CONFIDENCE", "equipment_choices": cards_items},
    )


def compare_equipment(*, user, text: str, context_equipment_ids: list[int] | None = None) -> dict:
    """Side-by-side comparison from portal Equipment fields only."""
    from django.db.models import Q

    ids: list[int] = list(context_equipment_ids or [])
    # Prefer XRD family comparison when mentioned
    lower = (text or "").lower()
    needle = "xrd"
    for n in ("fesem", "sem", "nmr", "xps", "xrf", "pxrd", "xrd"):
        if n in lower:
            needle = "xrd" if n in ("xrd", "pxrd") else n
            break
    if len(ids) < 2:
        eqs = list(
            Equipment.objects.filter(Q(name__icontains=needle) | Q(code__icontains=needle), status="ACTIVE").order_by("name")[:4]
        )
    else:
        eqs = list(Equipment.objects.filter(pk__in=ids[:4], status="ACTIVE"))
    if len(eqs) < 2:
        return build_response(
            kind="CLARIFICATION",
            content=f"I need at least two portal instruments to compare (searched for “{needle}”). Try “Compare XRD machines”.",
            actions=[{"id": "search", "label": "Find equipment", "prompt": "Show me all XRD equipment", "enabled": True}],
            metadata={"deterministic": True},
        )

    lines = ["**Equipment comparison** (live portal data)", ""]
    rows = []
    for eq in eqs:
        dept = getattr(getattr(eq, "internal_department", None), "name", None) or "—"
        loc = getattr(eq, "location", None) or "—"
        mode = getattr(eq, "profile_type", None) or "—"
        make = getattr(eq, "make", None) or "—"
        model = getattr(eq, "model_information", None) or "—"
        lines.append(f"### {eq.name}")
        lines.append(f"- Department: {dept}")
        lines.append(f"- Location: {loc}")
        lines.append(f"- Mode/profile: {mode}")
        lines.append(f"- Make / model: {make} / {model}")
        lines.append("- Pricing / availability: use Estimate / Find slots (not invented here)")
        lines.append("")
        rows.append(
            {
                "id": eq.pk,
                "name": eq.name,
                "department": dept,
                "location": loc,
                "mode": mode,
                "make": make,
                "model": model,
                "href": f"/equipments/{eq.pk}",
            }
        )
    actions = [
        {"id": f"eq_{r['id']}", "label": f"Slots — {r['name']}", "prompt": f"Search available slots for {r['name']} this week", "enabled": True}
        for r in rows
    ]
    return build_response(
        kind="LIVE_DATA",
        content="\n".join(lines),
        cards=[{"type": "equipment_compare", "title": "Comparison", "items": rows}],
        actions=actions,
        metadata={"deterministic": True, "compare": True, "equipment_choices": rows, "confidence": "HIGH_CONFIDENCE"},
    )


def user_profile(*, user) -> dict:
    if user is None or not getattr(user, "is_authenticated", False):
        return build_response(kind="ACTION_REQUIRED", content="Sign in to view your profile.", actions=[{"id": "sign_in", "label": "Sign in", "href": "/auth", "enabled": True}])
    dept = getattr(getattr(user, "department", None), "name", None) or "—"
    lines = [
        "**Your profile** (live portal data)",
        "",
        f"- Name: {getattr(user, 'name', None) or '—'}",
        f"- Email: {getattr(user, 'email', None) or '—'}",
        f"- User type: {getattr(user, 'user_type', None) or '—'}",
        f"- Department: {dept}",
        f"- Employee / ID: {getattr(user, 'emp_id', None) or getattr(user, 'employee_id', None) or '—'}",
        "",
        "Protected identity fields cannot be changed via Copilot. Use Profile / Channel-I flows.",
    ]
    return build_response(
        kind="LIVE_DATA",
        content="\n".join(lines),
        cards=[{"type": "user_profile", "department": dept, "user_type": getattr(user, "user_type", None)}],
        actions=[{"id": "profile", "label": "Open Profile", "href": "/profile", "enabled": True}],
        metadata={"deterministic": True, "confidence": "HIGH_CONFIDENCE"},
    )


def daily_dashboard(*, user) -> dict:
    """Compose authoritative portal snippets into a personal research assistant digest."""
    if user is None or not getattr(user, "is_authenticated", False):
        return build_response(kind="ACTION_REQUIRED", content="Sign in for your daily research dashboard.", actions=[{"id": "sign_in", "label": "Sign in", "href": "/auth", "enabled": True}])

    sections = ["**Your research dashboard** (live portal data)", ""]
    actions = []
    # Next booking
    try:
        from iic_booking.research_copilot.services import tools as tools_svc

        nb = tools_svc._get_wallet(arguments={}, user=user)
        bal = ((nb or {}).get("data") or {}).get("balance")
        sections.append(f"**Wallet:** ₹{bal if bal is not None else '—'}")
        actions.append({"id": "wallet", "label": "Wallet", "prompt": "What is my wallet balance?", "enabled": True})

        nxt = tools_svc._get_next_booking(arguments={}, user=user)
        data = (nxt or {}).get("data") or {}
        if data.get("booking_id"):
            sections.append(
                f"**Next booking:** #{data.get('booking_id')} — {data.get('equipment')} ({data.get('status')}) · {data.get('start')}"
            )
            actions.append({"id": "next", "label": "Next booking", "prompt": "What is my next booking?", "enabled": True})
        else:
            sections.append("**Next booking:** none upcoming")
    except Exception:  # noqa: BLE001
        sections.append("**Wallet / bookings:** open portal pages if this summary is incomplete.")

    # Credit outstanding (best-effort)
    try:
        from iic_booking.research_copilot.services.v2.mutations import domain_bridge

        code, summary = domain_bridge.call_wallet_credit_summary(user=user)
        if code < 400 and isinstance(summary, dict):
            outstanding = summary.get("outstanding_amount") or summary.get("outstanding")
            sections.append(f"**Outstanding credit:** {outstanding if outstanding is not None else '—'}")
    except Exception:  # noqa: BLE001
        pass

    sections.append("")
    sections.append("**Suggested next steps**")
    sections.append("- Find equipment / slots if you need a new booking")
    sections.append("- Check Remote Analysis if a result or workspace is due")
    sections.append("- Open Support Tickets only if something is stuck")
    actions.extend(
        [
            {"id": "slots", "label": "Find slots", "prompt": "Search available slots for FESEM this week", "enabled": True},
            {"id": "ra", "label": "Remote Analysis", "prompt": "What is my Remote Analysis status?", "enabled": True},
            {"id": "tickets", "label": "Tickets", "href": "/tickets", "enabled": True},
        ]
    )
    return build_response(
        kind="LIVE_DATA",
        content="\n".join(sections),
        cards=[{"type": "daily_dashboard"}],
        actions=actions[:8],
        metadata={"deterministic": True, "dashboard": True, "confidence": "HIGH_CONFIDENCE"},
    )


def support_ticket_assist(*, user, text: str) -> dict:
    """Diagnose lightly then deep-link; never auto-create tickets (flag COPILOT_TICKET_CREATE)."""
    if user is None or not getattr(user, "is_authenticated", False):
        return build_response(kind="ACTION_REQUIRED", content="Sign in to create support requests.", actions=[{"id": "sign_in", "label": "Sign in", "href": "/auth", "enabled": True}])
    create_enabled = flag("COPILOT_TICKET_CREATE", False)
    content = (
        "**Support assist**\n\n"
        "I can help you check booking / wallet / Remote Analysis status first. "
        "Ticket creation via Copilot stays confirmation-gated"
        + (" and is currently **enabled**." if create_enabled else " and is currently **disabled** (open Tickets in the portal).")
        + "\n\nDescribe the issue, or open **Tickets** to file manually."
    )
    return build_response(
        kind="ACTION_REQUIRED",
        content=content,
        actions=[
            {"id": "tickets", "label": "Open Tickets", "href": "/tickets", "enabled": True, "requires_confirmation": True},
            {"id": "bookings", "label": "My bookings", "prompt": "List my recent bookings.", "enabled": True},
            {"id": "ra", "label": "RA status", "prompt": "What is my Remote Analysis status?", "enabled": True},
        ],
        metadata={"deterministic": True, "ticket_create_enabled": create_enabled},
    )

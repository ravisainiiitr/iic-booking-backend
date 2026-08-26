"""Deterministic intent classification for Copilot V2 Phase A/B."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ResolvedIntent:
    intent: str
    deterministic: bool
    params: dict[str, Any] = field(default_factory=dict)
    needs_equipment: bool = False
    needs_auth: bool = False


def resolve_intent(text: str) -> ResolvedIntent:
    lower = (text or "").lower().strip()
    if len(lower) < 2:
        return ResolvedIntent(intent="empty", deterministic=False)

    # Explicit confirmation of a prepared proposal
    if lower in {"confirm", "confirm booking", "yes, book it", "yes book it", "proceed", "book this", "confirm cancellation", "confirm cancel", "confirm reschedule"} or (
        lower.startswith("confirm ") and len(lower) < 40
    ):
        return ResolvedIntent("confirm_proposal", True, needs_auth=True)

    # Cancel / reschedule (before generic booking reads)
    if any(x in lower for x in ("cancel my booking", "cancel booking", "cancel my next", "cancel the booking")):
        return ResolvedIntent("prepare_cancel", True, needs_auth=True)
    if any(x in lower for x in ("reschedule", "move my booking", "move my next", "change my booking slot", "find another slot for my booking")):
        return ResolvedIntent("prepare_reschedule", True, needs_auth=True)

    # Prepare booking (does not execute)
    if any(
        x in lower
        for x in (
            "book it",
            "book this",
            "book the",
            "book earliest",
            "book the earliest",
            "book fesem",
            "book pxrd",
            "book xrd",
            "prepare booking",
            "i want to book",
            "make a booking",
        )
    ):
        return ResolvedIntent("prepare_booking", True, needs_auth=True, needs_equipment=True)

    # Pending actions
    if any(
        x in lower
        for x in (
            "what do i need",
            "pending action",
            "what's pending",
            "whats pending",
            "to do today",
            "pending?",
            "pending actions",
        )
    ):
        return ResolvedIntent("pending_actions", True, needs_auth=True)

    # Wallet (read only)
    if any(x in lower for x in ("wallet balance", "my balance", "how much balance", "what is my balance", "what's my balance")):
        return ResolvedIntent("wallet_balance", True, needs_auth=True)
    if any(x in lower for x in ("wallet transaction", "my transaction", "wallet statement", "show my transaction")):
        return ResolvedIntent("wallet_transactions", True, needs_auth=True)

    # Bookings
    if any(x in lower for x in ("my next booking", "next booking", "upcoming booking")):
        return ResolvedIntent("next_booking", True, needs_auth=True)
    if any(x in lower for x in ("my booking", "show my booking", "list my booking", "what are my booking")):
        return ResolvedIntent("my_bookings", True, needs_auth=True)

    # Sample / results / RA / faculty
    if any(x in lower for x in ("sample status", "sample accepted", "sample received", "has my sample")):
        return ResolvedIntent("sample_status", True, needs_auth=True)
    if any(x in lower for x in ("result ready", "my result", "results available", "has my result", "report arrived")):
        return ResolvedIntent("results", True, needs_auth=True)
    if any(x in lower for x in ("remote analysis", "raa status", "analysis workspace", "analysis pc")):
        return ResolvedIntent("ra_status", True, needs_auth=True)
    if any(x in lower for x in ("my faculty", "who is my faculty", "my affiliation", "my supervisor")):
        return ResolvedIntent("affiliations", True, needs_auth=True)

    # Availability
    slot_words = ("slot", "slots", "availability", "available", "free slot", "earliest", "this week", "tomorrow")
    if any(x in lower for x in slot_words) and any(
        x in lower for x in ("fesem", "sem", "tem", "xrd", "pxrd", "afm", "xps", "icp", "equipment", "instrument", "find", "search")
    ):
        earliest = "earliest" in lower or "first available" in lower
        cheapest = "cheapest" in lower
        return ResolvedIntent(
            "search_slots",
            True,
            params={"earliest": earliest, "cheapest": cheapest},
            needs_equipment=True,
        )
    if any(x in lower for x in ("slot", "slots", "availability", "available")):
        return ResolvedIntent(
            "search_slots",
            True,
            params={"earliest": "earliest" in lower, "cheapest": "cheapest" in lower},
            needs_equipment=True,
        )

    # Cost
    if any(x in lower for x in ("how much", "cost", "estimate", "price", "fee", "charge")):
        return ResolvedIntent("estimate_cost", True, needs_equipment=True)

    # Equipment discovery / capabilities
    if any(
        x in lower
        for x in (
            "what xrd",
            "what fesem",
            "what sem",
            "equipment is available",
            "facilities",
            "which equipment",
            "supports eds",
            "elemental",
            "nanoparticle",
            "i need fesem",
            "i need xrd",
        )
    ):
        return ResolvedIntent("search_equipment", True, needs_equipment=False)

    if any(x in lower for x in ("what can", "capability", "capabilities", "sop", "sample prep", "manual", "documentation", "how do i prepare", "hold mean")):
        return ResolvedIntent("docs_rag", True)

    if any(x in lower for x in ("find equipment", "search equipment", "recommend equipment")):
        return ResolvedIntent("search_equipment", True)

    return ResolvedIntent("general", False)

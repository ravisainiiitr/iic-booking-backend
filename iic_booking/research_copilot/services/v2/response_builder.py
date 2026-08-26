"""Structured Copilot V2 response envelopes + markdown fallbacks."""

from __future__ import annotations

from typing import Any


def build_response(
    *,
    kind: str,
    content: str,
    cards: list[dict[str, Any]] | None = None,
    actions: list[dict[str, Any]] | None = None,
    escalate: bool = False,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "response_kind": kind,
        "content": content,
        "cards": cards or [],
        "suggested_actions": actions or [],
        "escalate_hint": escalate,
        "confidence": 0.35 if escalate else 0.88,
        "metadata": metadata or {},
    }


def slots_markdown(*, equipment_name: str, rows: list[dict], window_label: str) -> str:
    if not rows:
        return (
            f"**{equipment_name}** — no AVAILABLE slots found for **{window_label}** in portal data.\n\n"
            "Open the equipment calendar for the authoritative view (generation/window rules may apply)."
        )
    lines = [f"**{equipment_name} — Available slots ({window_label})**", ""]
    for r in rows[:12]:
        day = r.get("date") or ""
        start = (r.get("start") or "")[11:16] if r.get("start") else ""
        end = (r.get("end") or "")[11:16] if r.get("end") else ""
        lines.append(f"- {day}  {start}–{end}")
    lines.append("")
    lines.append("_Live portal data. Estimates (if shown) are not final charges._")
    return "\n".join(lines)


def equipment_markdown(rows: list[dict]) -> str:
    if not rows:
        return "No matching IIC equipment found in the catalog for that query."
    lines = ["**IIC equipment matches**", ""]
    for i, r in enumerate(rows[:8], 1):
        loc = r.get("location") or "—"
        lines.append(f"{i}. **{r.get('name')}** — {loc}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet'][:160]}")
    return "\n".join(lines)


def clarify_equipment_markdown(cands: list) -> str:
    lines = ["Which equipment do you mean?", ""]
    for c in cands[:8]:
        name = c.name if hasattr(c, "name") else c.get("name")
        lines.append(f"- {name}")
    lines.append("")
    lines.append("Reply with the full equipment name.")
    return "\n".join(lines)

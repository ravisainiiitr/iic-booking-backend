"""
Pure scoring and selection. No database access: everything needed is in the inputs, so a stored
``ShortlistRun.inputs_snapshot`` reproduces the same outcome.

Selection order:
 1. OIC overrides to SELECTED take seats first (flagged); overrides to WAITLISTED / NOT_SELECTED leave the pool.
 2. Reserved seats (``reserved_pct``) go to under-represented departments by rank, within caps.
 3. Remaining seats by rank, skipping candidates whose faculty-group or department cap is reached.
 4. If seats remain, caps are relaxed: department cap first, then faculty cap.
 5. Everyone else is waitlisted in rank order.
Ranking: score descending; candidates within ``tie_window`` points of the top of their tie group are
ordered by a seeded lottery key = SHA-256(seed:nomination_id).
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any

SELECTED = "SELECTED"
WAITLISTED = "WAITLISTED"
NOT_SELECTED = "NOT_SELECTED"
INELIGIBLE = "INELIGIBLE"


def make_seed(call_id: int, timestamp_iso: str, public_input: str) -> str:
    raw = f"{int(call_id)}|{timestamp_iso}|{(public_input or '').strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def lottery_key(seed: str, nomination_id: int) -> str:
    return hashlib.sha256(f"{seed}:{int(nomination_id)}".encode("utf-8")).hexdigest()


def _r2(value: float) -> float:
    return round(float(value) + 0.0, 2)


def tenure_points(months: float | None, w: dict) -> float:
    if months is None:
        return float(w["tenure_unknown"])
    full, zero, top = float(w["tenure_full_months"]), float(w["tenure_zero_months"]), float(w["tenure_max"])
    if months >= full:
        return top
    if months <= zero:
        return 0.0
    return _r2(top * (months - zero) / (full - zero))


def demand_points(metric: float, all_metrics: list[float], w: dict) -> float:
    """Percentile of the candidate's demand among eligible candidates; zero demand earns nothing."""
    top = float(w["demand_max"])
    if metric <= 0 or not all_metrics:
        return 0.0
    n = len(all_metrics)
    if n == 1:
        return top
    less = sum(1 for v in all_metrics if v < metric)
    equal_others = sum(1 for v in all_metrics if v == metric) - 1
    return _r2(top * (less + 0.5 * equal_others) / (n - 1))


def department_points(dept_key: str, gaps: dict[str, float], override_keys: set[str], w: dict) -> float:
    top = float(w["department_max"])
    if dept_key in override_keys:
        return top
    gap = gaps.get(dept_key, 0.0)
    positive = [g for g in gaps.values() if g > 0]
    if gap <= 0 or not positive:
        return 0.0
    return _r2(top * min(1.0, gap / max(positive)))


def score_candidates(candidates: list[dict], context: dict) -> dict[int, dict]:
    """Return {nomination_id: {"total": float, "breakdown": {...}}} for eligible candidates."""
    w = context["weights"]
    gaps = {str(k): float(v) for k, v in (context.get("department_gaps") or {}).items()}
    overrides = {str(k) for k in (context.get("underrepresented_override") or [])}
    eligible = [c for c in candidates if c.get("eligible")]
    metrics = [float(c["factors"].get("demand_metric") or 0) for c in eligible]
    out: dict[int, dict] = {}
    for c in eligible:
        f = c["factors"]
        b: dict[str, Any] = {}
        b["first_time_equipment"] = float(w["first_time_equipment"]) if f.get("first_time_equipment") else 0.0
        b["never_trained_anywhere"] = float(w["never_trained_anywhere"]) if f.get("never_trained_anywhere") else 0.0
        need = max(0, min(int(w["need_max_points"]), int(f.get("need_points") or 0)))
        b["research_need"] = float(need * float(w["need_per_point"]))
        b["demand"] = demand_points(float(f.get("demand_metric") or 0), metrics, w)
        b["tenure"] = tenure_points(f.get("tenure_months"), w)
        b["department_underrepresentation"] = department_points(str(c.get("department_key")), gaps, overrides, w)
        b["group_no_certified"] = 0.0 if f.get("group_has_certified") else float(w["group_no_certified"])
        b["cooldown"] = float(w["cooldown_penalty"]) if f.get("cooldown") else 0.0
        b["prior_no_show"] = float(w["no_show_penalty"]) if f.get("no_show") else 0.0
        if "recent_selection_penalty" in w:
            b["recent_selection"] = float(w["recent_selection_penalty"]) if f.get("recent_selection") else 0.0
        if "group_repeat_per_selection" in w:
            b["group_repeat"] = float(w["group_repeat_per_selection"]) * int(f.get("group_recent_selections") or 0)
        total = _r2(sum(b.values()))
        out[int(c["nomination_id"])] = {"total": total, "breakdown": b}
    return out


@dataclass
class Placement:
    nomination_id: int
    outcome: str
    rank: int | None = None
    tie_group: int | None = None
    lottery_key: str = ""
    seat_type: str = ""
    waitlist_position: int | None = None
    note: str = ""
    overridden: bool = False
    score: float = 0.0
    breakdown: dict = field(default_factory=dict)


def rank_candidates(scored: list[dict], seed: str, tie_window: float) -> list[dict]:
    """Order eligible candidates; adds rank, tie_group and lottery_key."""
    base = sorted(scored, key=lambda c: (-c["score"], c["nomination_id"]))
    groups: list[list[dict]] = []
    for c in base:
        if groups and groups[-1][0]["score"] - c["score"] <= tie_window + 1e-9:
            groups[-1].append(c)
        else:
            groups.append([c])
    ordered: list[dict] = []
    tie_id = 0
    for group in groups:
        for c in group:
            c["lottery_key"] = lottery_key(seed, c["nomination_id"])
        if len(group) > 1:
            tie_id += 1
            group.sort(key=lambda c: c["lottery_key"])
            for c in group:
                c["tie_group"] = tie_id
        else:
            group[0]["tie_group"] = None
        ordered.extend(group)
    for i, c in enumerate(ordered, start=1):
        c["rank"] = i
    return ordered


def caps_for(seats: int, per_department_pct: int, reserved_pct: int) -> tuple[int, int]:
    dept_cap = max(1, math.floor(seats * per_department_pct / 100))
    reserved = math.floor(seats * reserved_pct / 100)
    return dept_cap, reserved


def select(inputs: dict) -> list[Placement]:
    """Deterministic selection from a frozen inputs snapshot."""
    candidates = inputs["candidates"]
    seats = int(inputs["seats"])
    per_faculty_cap = int(inputs["per_faculty_cap"])
    dept_cap, reserved = caps_for(seats, int(inputs["per_department_pct"]), int(inputs["reserved_pct"]))
    seed = inputs["seed"]
    tie_window = float(inputs.get("tie_window", 1))
    overrides = {int(k): v for k, v in (inputs.get("overrides") or {}).items()}
    underrep = {str(k) for k in (inputs.get("underrepresented_departments") or [])}

    scores = score_candidates(candidates, inputs["scoring_context"])
    scored = []
    for c in candidates:
        if not c.get("eligible"):
            continue
        s = scores[int(c["nomination_id"])]
        scored.append(
            {
                "nomination_id": int(c["nomination_id"]),
                "faculty_id": c.get("faculty_id"),
                "department_key": str(c.get("department_key")),
                "score": s["total"],
                "breakdown": s["breakdown"],
            }
        )
    ordered = rank_candidates(scored, seed, tie_window)
    placements: dict[int, Placement] = {}
    fac_count: dict[Any, int] = {}
    dept_count: dict[str, int] = {}
    chosen: list[int] = []

    def place(c: dict, seat_type: str, note: str = "", overridden: bool = False) -> None:
        chosen.append(c["nomination_id"])
        fac_count[c["faculty_id"]] = fac_count.get(c["faculty_id"], 0) + 1
        dept_count[c["department_key"]] = dept_count.get(c["department_key"], 0) + 1
        placements[c["nomination_id"]] = _placement(c, SELECTED, seat_type=seat_type, note=note, overridden=overridden)

    def fac_ok(c):
        return fac_count.get(c["faculty_id"], 0) < per_faculty_cap

    def dept_ok(c):
        return dept_count.get(c["department_key"], 0) < dept_cap

    for c in ordered:
        if overrides.get(c["nomination_id"], {}).get("outcome") == SELECTED:
            place(c, "OVERRIDE", "Selected by OIC override", overridden=True)
    pool = [c for c in ordered if c["nomination_id"] not in overrides]
    skipped_note: dict[int, str] = {}

    reserved_filled = 0
    for c in pool:
        if reserved_filled >= reserved or len(chosen) >= seats:
            break
        if c["department_key"] in underrep and fac_ok(c) and dept_ok(c):
            place(c, "RESERVED", "Reserved seat (under-represented department)")
            reserved_filled += 1

    for c in pool:
        if len(chosen) >= seats:
            break
        if c["nomination_id"] in placements:
            continue
        if not fac_ok(c):
            skipped_note[c["nomination_id"]] = "Faculty group cap reached"
        elif not dept_ok(c):
            skipped_note[c["nomination_id"]] = "Department cap reached"
        else:
            place(c, "GENERAL")

    for relax, note in (("department", "Department cap relaxed (seats unfilled)"), ("faculty", "Faculty cap relaxed (seats unfilled)")):
        for c in pool:
            if len(chosen) >= seats:
                break
            if c["nomination_id"] in placements:
                continue
            if relax == "department" and not fac_ok(c):
                continue
            place(c, "RELAXED", note)

    waitlist = [
        c
        for c in ordered
        if c["nomination_id"] not in placements
        and overrides.get(c["nomination_id"], {}).get("outcome", WAITLISTED) == WAITLISTED
    ]
    for pos, c in enumerate(waitlist, start=1):
        forced = c["nomination_id"] in overrides
        placements[c["nomination_id"]] = _placement(
            c,
            WAITLISTED,
            waitlist_position=pos,
            note="Waitlisted by OIC override" if forced else skipped_note.get(c["nomination_id"], ""),
            overridden=forced,
        )
    for c in ordered:
        if c["nomination_id"] not in placements:
            placements[c["nomination_id"]] = _placement(c, NOT_SELECTED, note="Not selected by OIC override", overridden=True)

    result = [placements[c["nomination_id"]] for c in ordered]
    for c in candidates:
        if not c.get("eligible"):
            result.append(
                Placement(
                    nomination_id=int(c["nomination_id"]),
                    outcome=INELIGIBLE,
                    note="; ".join(c.get("ineligible_reasons") or []) or "Ineligible",
                )
            )
    return result


def _placement(c: dict, outcome: str, **kwargs) -> Placement:
    return Placement(
        nomination_id=c["nomination_id"],
        outcome=outcome,
        rank=c.get("rank"),
        tie_group=c.get("tie_group"),
        lottery_key=c.get("lottery_key", ""),
        score=c["score"],
        breakdown=c["breakdown"],
        **kwargs,
    )


def pick_promotion(waitlist: list[dict], occupied: list[dict], *, seats: int, per_faculty_cap: int, per_department_pct: int,
                   reserved_pct: int, underrep: set[str]) -> dict | None:
    """Next waitlisted candidate for a vacated seat, honouring caps and the reserved quota where possible."""
    if not waitlist or len(occupied) >= seats:
        return None
    dept_cap, reserved = caps_for(seats, per_department_pct, reserved_pct)
    fac: dict[Any, int] = {}
    dept: dict[str, int] = {}
    for o in occupied:
        fac[o["faculty_id"]] = fac.get(o["faculty_id"], 0) + 1
        dept[str(o["department_key"])] = dept.get(str(o["department_key"]), 0) + 1
    reserved_held = sum(1 for o in occupied if str(o["department_key"]) in underrep)

    def within_caps(c):
        return fac.get(c["faculty_id"], 0) < per_faculty_cap and dept.get(str(c["department_key"]), 0) < dept_cap

    if reserved_held < reserved:
        for c in waitlist:
            if str(c["department_key"]) in underrep and within_caps(c):
                return c
    for c in waitlist:
        if within_caps(c):
            return c
    for c in waitlist:
        if fac.get(c["faculty_id"], 0) < per_faculty_cap:
            return c
    return waitlist[0]

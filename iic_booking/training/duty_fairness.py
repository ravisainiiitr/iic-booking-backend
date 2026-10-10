"""
Fair rotation of operator duty. Pure: everything comes in as plain data, so the snapshot stored on an allocation
explains (and reproduces) why someone was suggested.

Priority (higher = next in line), with weights from the operator policy:
  load              − load × (term hours ÷ roster average)            fewer hours this term → earlier
  rotation          + rotation × min(days since last duty, full) ÷ full  waiting longest → earlier
  faculty_share     − faculty_share × max(0, group hours share − group member share) × 2
  department_share  − department_share × max(0, department hours share − department member share) × 2
  repeat            − repeat × allocations already given this term
Ties are broken by a hash of (proposal start, user) so the same person is not always first.

Gates: no current certification/TA basis or a conflict → not eligible (hard). Cooling period, weekly cap and
term cap → blocked, but the OIC may still choose the person with a recorded reason.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any


def _hours(minutes: float) -> float:
    return round(float(minutes) / 60.0, 2)


def _tiebreak(seed: str, user_id: int) -> str:
    return hashlib.sha256(f"{seed}:{int(user_id)}".encode()).hexdigest()


def _share_penalties(candidates: list[dict], key: str) -> dict[Any, dict]:
    members: dict[Any, int] = {}
    minutes: dict[Any, float] = {}
    for c in candidates:
        g = c.get(key)
        if g is None:
            continue
        members[g] = members.get(g, 0) + 1
        minutes[g] = minutes.get(g, 0.0) + float(c.get("term_minutes") or 0)
    total_members = sum(members.values()) or 1
    total_minutes = sum(minutes.values())
    out = {}
    for g, n in members.items():
        member_share = n / total_members
        hours_share = (minutes[g] / total_minutes) if total_minutes else member_share
        out[g] = {"member_share": round(member_share, 4), "hours_share": round(hours_share, 4), "over": round(max(0.0, hours_share - member_share), 4)}
    return out


def rank(candidates: list[dict], proposal: dict, policy: dict, *, now: datetime) -> list[dict]:
    """Return candidates with ``priority``, ``rank`` (eligible and unblocked only), ``blocked``, ``reasons``."""
    w = policy["weights"]
    max_week = float(policy["max_hours_week"]) * 60
    max_term = float(policy["max_hours_term"]) * 60
    cooling = timedelta(days=int(policy.get("cooling_days") or 0))
    start: datetime = proposal["start"]
    end: datetime = proposal["end"]
    add_minutes = float(proposal["minutes"])
    add_by_week: dict[str, float] = proposal.get("minutes_by_week") or {}
    seed = start.isoformat()

    pool = [c for c in candidates if c.get("eligible_basis")]
    avg_term = (sum(float(c.get("term_minutes") or 0) for c in pool) / len(pool)) if pool else 0.0
    fac = _share_penalties(pool, "faculty_id")
    dept = _share_penalties(pool, "department_id")
    full_days = float(w.get("rotation_full_days") or 30)

    out = []
    for c in candidates:
        row = {**c, "blocked": [], "hard_blocked": [], "reasons": [], "priority": None, "rank": None}
        if not c.get("eligible_basis"):
            row["hard_blocked"].append(c.get("basis_reason") or "Not eligible")
        for conflict in c.get("conflicts") or []:
            row["hard_blocked"].append(conflict)
        term = float(c.get("term_minutes") or 0)
        cap_week = float(c["max_hours_week"]) * 60 if c.get("max_hours_week") else max_week
        worst_week = 0.0
        for week, mins in add_by_week.items():
            worst_week = max(worst_week, float((c.get("week_minutes") or {}).get(week, 0)) + float(mins))
        if worst_week > cap_week:
            row["blocked"].append(f"Weekly cap: {_hours(worst_week)} h would exceed {_hours(cap_week)} h")
        if term + add_minutes > max_term:
            row["blocked"].append(f"Term cap: {_hours(term + add_minutes)} h would exceed {_hours(max_term)} h")
        last_end = c.get("last_duty_end")
        next_start = c.get("next_duty_start")
        cooling_hit = None
        if cooling and last_end and last_end <= start and start - last_end < cooling:
            cooling_hit = last_end + cooling
        if cooling and next_start and next_start >= end and next_start - end < cooling:
            cooling_hit = cooling_hit or next_start
        if cooling_hit:
            row["blocked"].append(f"Cooling period ({policy.get('cooling_days')} day(s) between duty blocks)")
        row["cooling_until"] = (last_end + cooling).isoformat() if cooling and last_end and last_end + cooling > now else None

        load_ratio = (term / avg_term) if avg_term else 0.0
        days_since = (now - last_end).total_seconds() / 86400 if last_end and last_end < now else full_days
        f_over = fac.get(c.get("faculty_id"), {}).get("over", 0.0)
        d_over = dept.get(c.get("department_id"), {}).get("over", 0.0)
        repeats = int(c.get("allocations_term") or 0)
        parts = {
            "load": -float(w["load"]) * load_ratio,
            "rotation": float(w["rotation"]) * min(days_since, full_days) / full_days,
            "faculty_share": -float(w["faculty_share"]) * f_over * 2,
            "department_share": -float(w["department_share"]) * d_over * 2,
            "repeat": -float(w["repeat"]) * repeats,
        }
        row["priority"] = round(100 + sum(parts.values()), 2)
        row["breakdown"] = {k: round(v, 2) for k, v in parts.items()}
        row["metrics"] = {
            "term_hours": _hours(term),
            "average_term_hours": _hours(avg_term),
            "days_since_last_duty": round(days_since, 1) if last_end else None,
            "allocations_term": repeats,
            "faculty_hours_share": fac.get(c.get("faculty_id"), {}).get("hours_share"),
            "faculty_member_share": fac.get(c.get("faculty_id"), {}).get("member_share"),
            "department_hours_share": dept.get(c.get("department_id"), {}).get("hours_share"),
            "department_member_share": dept.get(c.get("department_id"), {}).get("member_share"),
        }
        if avg_term and term < avg_term:
            row["reasons"].append(f"Fewer hours this term ({_hours(term)} h vs average {_hours(avg_term)} h)")
        elif not term:
            row["reasons"].append("No duty hours yet this term")
        if last_end is None:
            row["reasons"].append("Has not had a duty block yet")
        elif days_since >= 7:
            row["reasons"].append(f"Waiting {int(days_since)} days since the last duty")
        if f_over:
            row["reasons"].append("Their faculty group already has more than its fair share of hours")
        if d_over:
            row["reasons"].append("Their department already has more than its fair share of hours")
        row["tiebreak"] = _tiebreak(seed, c["user_id"])
        out.append(row)

    ranked = sorted(
        [r for r in out if not r["hard_blocked"] and not r["blocked"]],
        key=lambda r: (-r["priority"], r["tiebreak"]),
    )
    for i, r in enumerate(ranked, start=1):
        r["rank"] = i
    rest = sorted([r for r in out if r["hard_blocked"] or r["blocked"]], key=lambda r: (bool(r["hard_blocked"]), -r["priority"], r["tiebreak"]))
    for r in ranked[:1]:
        r["reasons"].insert(0, "Next in the fair rotation")
    return ranked + rest

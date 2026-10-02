"""Pure scoring and selection: deterministic, caps, reserved quota, relaxation, ties, overrides."""

from iic_booking.training import scoring
from iic_booking.training.models import DEFAULT_SCORING_WEIGHTS

SEED = scoring.make_seed(7, "2026-10-01T10:00:00+05:30", "4821")


def cand(nid, *, faculty, dept, need=2, demand=0, tenure=24, first=True, never=True, group_cert=False,
         cooldown=False, no_show=False, eligible=True, reasons=None):
    return {
        "nomination_id": nid,
        "faculty_id": faculty,
        "department_key": str(dept),
        "eligible": eligible,
        "ineligible_reasons": reasons or [],
        "flags": [],
        "factors": {
            "first_time_equipment": first,
            "never_trained_anywhere": never,
            "need_points": need,
            "demand_metric": demand,
            "tenure_months": tenure,
            "group_has_certified": group_cert,
            "cooldown": cooldown,
            "no_show": no_show,
        },
    }


def inputs(cands, *, seats, fac_cap=1, dept_pct=40, reserved_pct=20, underrep=(), overrides=None, gaps=None, seed=SEED):
    return {
        "call_id": 7,
        "seats": seats,
        "per_faculty_cap": fac_cap,
        "per_department_pct": dept_pct,
        "reserved_pct": reserved_pct,
        "tie_window": 1,
        "seed": seed,
        "overrides": overrides or {},
        "underrepresented_departments": [str(d) for d in underrep],
        "scoring_context": {
            "weights": dict(DEFAULT_SCORING_WEIGHTS),
            "department_gaps": gaps or {},
            "underrepresented_override": [],
        },
        "candidates": cands,
    }


def by_id(placements):
    return {p.nomination_id: p for p in placements}


def test_score_breakdown_matches_design_weights():
    c = cand(1, faculty=1, dept=1, need=3, demand=0, tenure=None, first=True, never=True, group_cert=False)
    out = scoring.score_candidates([c], {"weights": DEFAULT_SCORING_WEIGHTS, "department_gaps": {}})[1]
    b = out["breakdown"]
    assert b["first_time_equipment"] == 30
    assert b["never_trained_anywhere"] == 10
    assert b["research_need"] == 18
    assert b["demand"] == 0
    assert b["tenure"] == 5  # unknown programme end → neutral half points
    assert b["group_no_certified"] == 7
    assert out["total"] == 70


def test_penalties_and_tenure_curve():
    w = DEFAULT_SCORING_WEIGHTS
    assert scoring.tenure_points(18, w) == 10
    assert scoring.tenure_points(6, w) == 0
    assert scoring.tenure_points(12, w) == 5
    c = cand(1, faculty=1, dept=1, need=1, cooldown=True, no_show=True, first=False, never=False, group_cert=True)
    out = scoring.score_candidates([c], {"weights": w, "department_gaps": {}})[1]
    assert out["breakdown"]["cooldown"] == -15
    assert out["breakdown"]["prior_no_show"] == -10
    assert out["total"] == 6 + 10 - 25


def test_demand_percentile_and_department_gap_points():
    w = DEFAULT_SCORING_WEIGHTS
    assert scoring.demand_points(0, [0, 3, 9], w) == 0
    assert scoring.demand_points(9, [0, 3, 9], w) == 15
    assert scoring.demand_points(3, [0, 3, 9], w) == 7.5
    gaps = {"1": 0.30, "2": 0.15, "3": -0.2}
    assert scoring.department_points("1", gaps, set(), w) == 10
    assert scoring.department_points("2", gaps, set(), w) == 5
    assert scoring.department_points("3", gaps, set(), w) == 0
    assert scoring.department_points("3", gaps, {"3"}, w) == 10


def test_selection_is_deterministic_for_fixed_seed():
    cands = [cand(i, faculty=i, dept=i % 3, need=2) for i in range(1, 9)]
    first = scoring.select(inputs(cands, seats=3))
    second = scoring.select(inputs(cands, seats=3))
    assert [(p.nomination_id, p.outcome, p.rank) for p in first] == [(p.nomination_id, p.outcome, p.rank) for p in second]


def test_tie_break_uses_seeded_lottery_within_one_point():
    # Equal scores → one tie group; order follows SHA-256(seed:id), and a different seed reorders.
    cands = [cand(i, faculty=i, dept=i, need=2) for i in range(1, 7)]
    placements = scoring.select(inputs(cands, seats=2, dept_pct=100, reserved_pct=0))
    keys = [scoring.lottery_key(SEED, p.nomination_id) for p in placements]
    assert keys == sorted(keys)
    assert all(p.tie_group == 1 for p in placements)
    other = scoring.select(inputs(cands, seats=2, dept_pct=100, reserved_pct=0, seed=scoring.make_seed(7, "x", "99")))
    assert [p.nomination_id for p in placements] != [p.nomination_id for p in other]


def test_scores_more_than_one_point_apart_are_not_tied():
    high = cand(1, faculty=1, dept=1, need=3)
    low = cand(2, faculty=2, dept=2, need=2)
    placements = scoring.select(inputs([low, high], seats=1, dept_pct=100, reserved_pct=0))
    assert placements[0].nomination_id == 1 and placements[0].outcome == "SELECTED"
    assert placements[0].tie_group is None
    assert placements[1].outcome == "WAITLISTED" and placements[1].waitlist_position == 1


def test_faculty_cap_one_seat_per_group():
    cands = [
        cand(1, faculty=10, dept=1, need=3),
        cand(2, faculty=10, dept=2, need=3, demand=5),
        cand(3, faculty=11, dept=3, need=1),
    ]
    res = by_id(scoring.select(inputs(cands, seats=2, dept_pct=100, reserved_pct=0)))
    selected = {nid for nid, p in res.items() if p.outcome == "SELECTED"}
    assert 3 in selected and len(selected & {1, 2}) == 1
    skipped = (res[1] if res[1].outcome == "WAITLISTED" else res[2])
    assert skipped.note == "Faculty group cap reached"


def test_department_cap_40_percent():
    cands = [cand(i, faculty=i, dept=1, need=3) for i in range(1, 5)] + [cand(9, faculty=9, dept=2, need=1)]
    res = by_id(scoring.select(inputs(cands, seats=5, dept_pct=40, reserved_pct=0)))
    # 5 seats, dept cap = 2 → only 2 of dept 1 in the general pass, dept 2 gets a seat; remaining seats via relaxation
    general = [p for p in res.values() if p.seat_type == "GENERAL"]
    assert sum(1 for p in general if p.nomination_id != 9) == 2
    assert res[9].outcome == "SELECTED"
    relaxed = [p for p in res.values() if p.seat_type == "RELAXED"]
    assert len(relaxed) == 2 and all("Department cap relaxed" in p.note for p in relaxed)


def test_reserved_seats_go_to_underrepresented_first():
    cands = [cand(i, faculty=i, dept=1, need=3, demand=10) for i in range(1, 6)] + [cand(20, faculty=20, dept=5, need=1)]
    res = by_id(scoring.select(inputs(cands, seats=5, dept_pct=100, reserved_pct=20, underrep=[5])))
    assert res[20].outcome == "SELECTED" and res[20].seat_type == "RESERVED"
    assert sum(1 for p in res.values() if p.outcome == "SELECTED") == 5


def test_faculty_cap_relaxed_only_when_seats_remain():
    cands = [cand(1, faculty=10, dept=1), cand(2, faculty=10, dept=2)]
    res = by_id(scoring.select(inputs(cands, seats=2, dept_pct=100, reserved_pct=0)))
    assert {p.outcome for p in res.values()} == {"SELECTED"}
    assert any("Faculty cap relaxed" in p.note for p in res.values())


def test_overrides_and_ineligible():
    cands = [cand(1, faculty=1, dept=1, need=3), cand(2, faculty=2, dept=2, need=1), cand(3, faculty=3, dept=3, eligible=False, reasons=["Account on hold"])]
    res = by_id(
        scoring.select(
            inputs(cands, seats=1, dept_pct=100, reserved_pct=0, overrides={"2": {"outcome": "SELECTED", "reason": "x"}, "1": {"outcome": "NOT_SELECTED", "reason": "y"}})
        )
    )
    assert res[2].outcome == "SELECTED" and res[2].overridden and res[2].seat_type == "OVERRIDE"
    assert res[1].outcome == "NOT_SELECTED" and res[1].overridden
    assert res[3].outcome == "INELIGIBLE" and "Account on hold" in res[3].note


def test_pick_promotion_respects_caps_and_reserved_quota():
    occupied = [{"nomination_id": 1, "faculty_id": 10, "department_key": "1"}]
    waitlist = [
        {"nomination_id": 2, "faculty_id": 10, "department_key": "1"},
        {"nomination_id": 3, "faculty_id": 11, "department_key": "1"},
        {"nomination_id": 4, "faculty_id": 12, "department_key": "9"},
    ]
    kwargs = dict(seats=5, per_faculty_cap=1, per_department_pct=40, reserved_pct=20)
    assert scoring.pick_promotion(waitlist, occupied, underrep={"9"}, **kwargs)["nomination_id"] == 4
    assert scoring.pick_promotion(waitlist, occupied, underrep=set(), **kwargs)["nomination_id"] == 3
    assert scoring.pick_promotion(waitlist, occupied * 5, underrep=set(), **kwargs) is None

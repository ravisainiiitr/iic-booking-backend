"""
Nomination calls, eligibility, shortlist runs, publication, seat confirmation, waitlist promotion, appeals.

TrainingNomination: SUBMITTED → (student confirms interest) → ELIGIBLE | INELIGIBLE at shortlisting;
SUBMITTED → WITHDRAWN; after publication SELECTED | WAITLISTED | NOT_SELECTED (| INELIGIBLE);
SELECTED → CONFIRMED | DECLINED | EXPIRED (no response before the deadline → next waitlisted promoted).
"""

from __future__ import annotations

import csv
import io
import logging
import secrets
from datetime import timedelta

from dateutil.relativedelta import relativedelta
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from iic_booking.users.display import get_user_display_name
from iic_booking.users.models.user_type import UserType

from . import access, notify, scoring
from .audit import audit
from .errors import TrainingError
from .models import (
    NEED_CATEGORY_POINTS,
    AppealStatus,
    AttendanceStatus,
    AwardStatus,
    CallStatus,
    CertificationAward,
    CertificationLevel,
    EntryOutcome,
    EventKind,
    EventStatus,
    NeedCategory,
    NominationCall,
    NominationStatus,
    Registration,
    RegistrationSource,
    RegistrationStatus,
    RunStatus,
    SelectionAppeal,
    SelectionMode,
    ShortlistEntry,
    ShortlistRun,
    TrainingEvent,
    TrainingNomination,
)
from .policy import add_working_days, effective_policy, policy_snapshot

logger = logging.getLogger(__name__)

UNDERREP_MIN_GAP = 0.05
DEMAND_LOOKBACK_DAYS = 365
NO_SHOW_LOOKBACK_MONTHS = 24
HELD_STATUSES = (AwardStatus.ACTIVE, AwardStatus.PROVISIONAL, AwardStatus.DORMANT)


# ---------------------------------------------------------------------------
# Calls and nominations
# ---------------------------------------------------------------------------
def open_call(actor, data: dict) -> NominationCall:
    from iic_booking.equipment.models import Equipment

    event = None
    if data.get("event_id"):
        event = TrainingEvent.objects.filter(pk=data["event_id"]).select_related("equipment").first()
        if event is None:
            raise TrainingError("Event not found.", status=404)
        equipment = event.equipment
    else:
        equipment = Equipment.objects.filter(pk=data.get("equipment_id")).first()
    if equipment is None:
        raise TrainingError("Equipment is required.")
    if not access.can_manage_equipment(actor, equipment.equipment_id):
        raise TrainingError("Only the equipment's OIC can open a call.", status=403, code="forbidden")
    if not access.equipment_in_pilot(equipment):
        raise TrainingError("Training is not enabled for this equipment yet.", code="not_in_pilot")
    try:
        seats = int(data.get("seats") or 0)
    except (TypeError, ValueError):
        raise TrainingError("Seats must be a number.") from None
    if seats < 1 or seats > 200:
        raise TrainingError("Seats must be between 1 and 200.")
    deadline = parse_datetime(str(data.get("deadline") or ""))
    if deadline is None:
        raise TrainingError("Deadline must be an ISO date-time.")
    if timezone.is_naive(deadline):
        deadline = timezone.make_aware(deadline)
    if deadline <= timezone.now():
        raise TrainingError("Deadline must be in the future.")
    policy = effective_policy(equipment)
    snap = policy_snapshot(policy)
    with transaction.atomic():
        if event is None:
            title = (data.get("title") or f"Hands-on training: {equipment.name}").strip()[:255]
            event = TrainingEvent.objects.create(
                kind=EventKind.HANDS_ON,
                title=title,
                slug=f"training-{equipment.code.lower()[:30]}-{secrets.token_hex(3)}",
                description=(data.get("description") or "").strip(),
                equipment=equipment,
                department=equipment.internal_department,
                level_awarded=CertificationLevel.objects.filter(code="TRAINED").first(),
                status=EventStatus.OPEN,
                capacity=seats,
                selection_mode=SelectionMode.NOMINATION,
                registration_closes_at=deadline,
                venue=(data.get("venue") or "").strip()[:255],
                created_by=actor,
                published_at=timezone.now(),
            )
        else:
            event.status = EventStatus.OPEN
            event.capacity = seats
            event.registration_closes_at = deadline
            event.save(update_fields=["status", "capacity", "registration_closes_at", "updated_at"])
        call = NominationCall.objects.create(
            event=event,
            equipment=equipment,
            title=(data.get("title") or event.title)[:255],
            seats=seats,
            deadline=deadline,
            eligibility=data.get("eligibility") or {"student_types": list(access.STUDENT_TYPES), "min_tenure_months": policy.min_tenure_months_after_training},
            caps_snapshot={
                "per_faculty_cap": policy.per_faculty_cap,
                "per_department_pct": policy.per_department_pct,
                "reserved_pct": policy.reserved_pct,
                "policy": snap,
            },
            policy=policy if policy.pk else None,
            policy_version=policy.version,
            status=CallStatus.OPEN,
            notes=(data.get("notes") or "").strip(),
            opened_by=actor,
        )
        audit(actor, "call.opened", call, after={"seats": seats, "deadline": deadline.isoformat(), "policy_version": policy.version})
    _notify_call_open(call, actor)
    return call


def close_call(call: NominationCall, actor) -> NominationCall:
    _require_manager(call, actor)
    if call.status != CallStatus.OPEN:
        raise TrainingError("Only open calls can be closed.")
    call.status = CallStatus.CLOSED
    call.closed_at = timezone.now()
    call.save(update_fields=["status", "closed_at"])
    TrainingEvent.objects.filter(pk=call.event_id, status=EventStatus.OPEN).update(status=EventStatus.SHORTLISTING)
    audit(actor, "call.closed", call)
    return call


def call_accepts_nominations(call: NominationCall) -> bool:
    return call.status == CallStatus.OPEN and call.deadline > timezone.now()


def nominate(faculty, data: dict) -> TrainingNomination:
    from iic_booking.users.models import User

    if not access.is_faculty(faculty):
        raise TrainingError("Only faculty can nominate students.", status=403, code="forbidden")
    call = NominationCall.objects.select_related("equipment", "event").filter(pk=data.get("call_id")).first()
    if call is None:
        raise TrainingError("Call not found.", status=404)
    if not call_accepts_nominations(call):
        raise TrainingError("This call is no longer accepting nominations.")
    student = User.objects.filter(pk=data.get("student_id")).first()
    if student is None:
        raise TrainingError("Student not found.", status=404)
    if student.user_type not in access.STUDENT_TYPES:
        raise TrainingError("Only IIT Roorkee students can be nominated.")
    if not access.is_valid_nominator(faculty, student):
        raise TrainingError(
            "You can only nominate students you supervise or who have joined your wallet (approved).",
            status=403,
            code="forbidden",
        )
    category = data.get("need_category") or NeedCategory.EXPLORATORY
    if category not in NeedCategory.values:
        raise TrainingError("Invalid need category.")
    justification = (data.get("justification") or "").strip()
    if len(justification) < 20:
        raise TrainingError("Please justify the nomination (at least 20 characters).")
    expected = data.get("expected_hours_month")
    try:
        expected = int(expected) if expected not in (None, "") else None
    except (TypeError, ValueError):
        raise TrainingError("Expected monthly usage must be a number of hours.") from None
    with transaction.atomic():
        existing = TrainingNomination.objects.select_for_update().filter(call=call, student=student).first()
        if existing and existing.status != NominationStatus.WITHDRAWN:
            raise TrainingError("This student is already nominated for this call.")
        if existing:
            existing.delete()
        nomination = TrainingNomination.objects.create(
            call=call,
            student=student,
            nominator=faculty,
            need_category=category,
            justification=justification,
            expected_hours_month=expected,
        )
        audit(faculty, "nomination.created", nomination, after={"call": call.pk, "student": student.pk, "need": category})
    notify.send(
        "training_nomination_received_student_email",
        [student],
        context=_call_context(call, summary=f"{_name(faculty)} nominated you for {call.title}. Confirm your interest before {notify.fmt_dt(call.deadline)} to be considered.", deadline=notify.fmt_dt(call.deadline)),
        title="You have been nominated for training",
        message=f"{_name(faculty)} nominated you for {call.title}. Confirm your interest.",
        path="/my-trainings?tab=applications",
        actor=faculty,
        event="training.nomination.created",
    )
    return nomination


def withdraw_nomination(nomination: TrainingNomination, user) -> TrainingNomination:
    if user.id not in (nomination.nominator_id, nomination.student_id):
        raise TrainingError("You cannot withdraw this nomination.", status=403, code="forbidden")
    if nomination.status not in (NominationStatus.SUBMITTED, NominationStatus.ELIGIBLE, NominationStatus.INELIGIBLE):
        raise TrainingError("Nominations can be withdrawn only before the selection is published.")
    nomination.status = NominationStatus.WITHDRAWN
    nomination.save(update_fields=["status", "updated_at"])
    audit(user, "nomination.withdrawn", nomination)
    return nomination


def confirm_interest(nomination: TrainingNomination, student, *, sop_acknowledged: bool = False) -> TrainingNomination:
    if nomination.student_id != student.id:
        raise TrainingError("This is not your nomination.", status=403, code="forbidden")
    if nomination.status not in (NominationStatus.SUBMITTED, NominationStatus.ELIGIBLE, NominationStatus.INELIGIBLE):
        raise TrainingError("Interest can be confirmed only while the nomination is open.")
    if not call_accepts_nominations(nomination.call):
        raise TrainingError("The call has closed.")
    now = timezone.now()
    nomination.student_confirmed_at = now
    nomination.status = NominationStatus.SUBMITTED
    if sop_acknowledged:
        nomination.sop_acknowledged_at = now
    nomination.save(update_fields=["student_confirmed_at", "sop_acknowledged_at", "status", "updated_at"])
    audit(student, "nomination.interest_confirmed", nomination)
    return nomination


def adjust_need(nomination: TrainingNomination, actor, *, points: int, reason: str) -> TrainingNomination:
    _require_manager(nomination.call, actor)
    reason = (reason or "").strip()
    if not reason:
        raise TrainingError("A reason is required to adjust the research-need score.")
    base = NEED_CATEGORY_POINTS.get(nomination.need_category, 0)
    try:
        target = int(points)
    except (TypeError, ValueError):
        raise TrainingError("Points must be 0–3.") from None
    if not 0 <= target <= 3:
        raise TrainingError("Points must be 0–3.")
    before = {"need_adjustment": nomination.need_adjustment}
    nomination.need_adjustment = target - base
    nomination.need_adjust_reason = reason
    nomination.save(update_fields=["need_adjustment", "need_adjust_reason", "updated_at"])
    audit(actor, "nomination.need_adjusted", nomination, before=before, after={"need_adjustment": nomination.need_adjustment}, note=reason)
    return nomination


# ---------------------------------------------------------------------------
# Eligibility and factors
# ---------------------------------------------------------------------------
def equipment_scope_ids(equipment) -> list[int]:
    from iic_booking.equipment.mode_utils import mode_family_ids
    from iic_booking.equipment.models import Equipment

    ids = set(mode_family_ids(equipment)) | {equipment.equipment_id}
    if equipment.equipment_group_id:
        ids |= set(Equipment.objects.filter(equipment_group_id=equipment.equipment_group_id).values_list("equipment_id", flat=True))
    return sorted(ids)


def _scope_award_q(scope_ids: list[int], group_id) -> Q:
    q = Q(equipment_id__in=scope_ids)
    if group_id:
        q |= Q(equipment_group_id=group_id)
    return q


def last_session_end(call: NominationCall):
    last = call.event.sessions.exclude(status="CANCELLED").order_by("-end_at").values_list("end_at", flat=True).first()
    return last, last is not None


def _demand_counts(user_ids: set[int], scope_ids: list[int], since) -> dict[int, float]:
    """Bookings, failed attempts, waitlist entries and no-slot logs per user. Missing sources are skipped."""
    from iic_booking.equipment import models as em

    counts: dict[int, float] = {uid: 0.0 for uid in user_ids}
    if not user_ids:
        return counts
    sources = (
        ("Booking", lambda m: m.objects.filter(user_id__in=user_ids, equipment_id__in=scope_ids, created_at__gte=since).exclude(status__in=("CANCELLED", "REFUNDED"))),
        ("BookingAttemptLog", lambda m: m.objects.filter(user_id__in=user_ids, equipment_id__in=scope_ids, requested_at__gte=since, outcome="FAILED")),
        ("WaitlistEntry", lambda m: m.objects.filter(user_id__in=user_ids, equipment_id__in=scope_ids, created_at__gte=since)),
        ("NoSlotAllocationLog", lambda m: m.objects.filter(user_id__in=user_ids, equipment_id__in=scope_ids, requested_at__gte=since)),
    )
    for name, build in sources:
        model = getattr(em, name, None)
        if model is None:
            continue
        try:
            with transaction.atomic():
                for row in build(model).values("user_id").annotate(n=Count("pk")):
                    counts[row["user_id"]] = counts.get(row["user_id"], 0.0) + float(row["n"])
        except Exception:
            logger.warning("training demand source %s unavailable", name, exc_info=True)
    return counts


def department_gaps(scope_ids: list[int], group_id, nominations: list[TrainingNomination], since) -> dict[str, float]:
    """Share of demand minus share of active certified/trained users, per department key."""
    from iic_booking.equipment import models as em

    certified: dict[str, int] = {}
    for row in (
        CertificationAward.objects.filter(_scope_award_q(scope_ids, group_id), status__in=HELD_STATUSES)
        .values("user__department_id")
        .annotate(n=Count("user_id", distinct=True))
    ):
        certified[str(row["user__department_id"])] = row["n"]
    total_cert = sum(certified.values())
    if total_cert == 0:
        return {}
    demand: dict[str, float] = {}
    for name, field_name in (("Booking", "created_at"), ("BookingAttemptLog", "requested_at"), ("WaitlistEntry", "created_at"), ("NoSlotAllocationLog", "requested_at")):
        model = getattr(em, name, None)
        if model is None:
            continue
        try:
            with transaction.atomic():
                qs = model.objects.filter(equipment_id__in=scope_ids, **{f"{field_name}__gte": since})
                if name == "BookingAttemptLog":
                    qs = qs.filter(outcome="FAILED")
                for row in qs.values("user__department_id").annotate(n=Count("pk")):
                    key = str(row["user__department_id"])
                    demand[key] = demand.get(key, 0.0) + float(row["n"])
        except Exception:
            logger.warning("training department demand source %s unavailable", name, exc_info=True)
    if sum(demand.values()) == 0:
        for n in nominations:
            key = str(n.student.department_id)
            demand[key] = demand.get(key, 0.0) + 1.0
    total_demand = sum(demand.values()) or 1.0
    keys = set(demand) | set(certified)
    return {k: round(demand.get(k, 0.0) / total_demand - certified.get(k, 0) / total_cert, 4) for k in keys}


def build_candidates(call: NominationCall, policy_snap: dict) -> tuple[list[dict], dict]:
    """Eligibility gates and raw scoring factors for every non-withdrawn nomination."""
    equipment = call.equipment
    scope_ids = equipment_scope_ids(equipment)
    group_id = equipment.equipment_group_id
    target_level = call.event.level_awarded or CertificationLevel.objects.filter(code="TRAINED").first()
    target_rank = target_level.rank if target_level else 10
    now = timezone.now()
    since = now - timedelta(days=DEMAND_LOOKBACK_DAYS)
    last_end, sessions_known = last_session_end(call)
    tenure_anchor = last_end or call.deadline
    nominations = list(
        call.nominations.exclude(status=NominationStatus.WITHDRAWN)
        .select_related("student", "student__department", "nominator")
        .order_by("id")
    )
    student_ids = {n.student_id for n in nominations}
    group_members: dict[int, set[int]] = {}
    for n in nominations:
        if n.nominator_id not in group_members:
            group_members[n.nominator_id] = access.faculty_group_student_ids(n.nominator)
    all_group_ids = set().union(*group_members.values()) if group_members else set()
    demand = _demand_counts(student_ids | all_group_ids, scope_ids, since)

    awards = list(CertificationAward.objects.filter(user_id__in=student_ids | all_group_ids).select_related("level"))
    scope_q_ids = set(scope_ids)

    def in_scope(a):
        return (a.equipment_id in scope_q_ids) or (group_id and a.equipment_group_id == group_id)

    cooldown_since = now - relativedelta(months=int(policy_snap["cooldown_months"]))
    lookback = now - relativedelta(months=int(policy_snap["suspension_lookback_months"]))
    no_show_since = now - relativedelta(months=NO_SHOW_LOOKBACK_MONTHS)
    no_show_users = set(
        Registration.objects.filter(user_id__in=student_ids, status=RegistrationStatus.NO_SHOW, created_at__gte=no_show_since).values_list("user_id", flat=True)
    ) | set(
        Registration.objects.filter(
            user_id__in=student_ids,
            attendance__status=AttendanceStatus.ABSENT,
            attendance__marked_at__gte=no_show_since,
        ).values_list("user_id", flat=True)
    )
    min_tenure = int(policy_snap["min_tenure_months_after_training"])
    candidates = []
    for n in nominations:
        s = n.student
        reasons: list[str] = []
        flags: list[str] = []
        if s.user_type not in access.STUDENT_TYPES:
            reasons.append("Not an IIT Roorkee student")
        if not s.is_active or getattr(s, "force_inactive", False):
            reasons.append("Account inactive")
        if getattr(s, "access_on_hold", False):
            reasons.append("Account access on hold")
        if not access.is_valid_nominator(n.nominator, s):
            reasons.append("Nominator is no longer the supervisor or wallet faculty")
        if not n.student_confirmed_at:
            reasons.append("Student has not confirmed interest")
        tenure_months = None
        if s.program_end_date:
            anchor_date = timezone.localtime(tenure_anchor).date()
            tenure_months = round((s.program_end_date - anchor_date).days / 30.44, 2)
            if s.program_end_date < anchor_date + relativedelta(months=min_tenure):
                reasons.append(f"Programme ends less than {min_tenure} months after the last session")
        else:
            flags.append("Programme end date unknown")
        if not sessions_known:
            flags.append("Sessions not scheduled yet; tenure checked against the call deadline")
        own = [a for a in awards if a.user_id == s.id]
        for a in own:
            if not in_scope(a):
                continue
            if a.status == AwardStatus.SUSPENDED:
                reasons.append("Active suspension on this equipment")
            elif a.status == AwardStatus.REVOKED and (a.revoked_at or a.awarded_at) >= lookback:
                reasons.append("Certification revoked on this equipment in the lookback period")
            elif a.suspended_at and a.suspended_at >= lookback and a.status != AwardStatus.ACTIVE:
                reasons.append("Suspended on this equipment in the lookback period")
            if a.status in HELD_STATUSES and a.level.rank >= target_rank:
                reasons.append(f"Already holds {a.level.name} on this equipment")
        need_points = max(0, min(3, NEED_CATEGORY_POINTS.get(n.need_category, 0) + n.need_adjustment))
        if n.need_adjustment:
            flags.append(f"Research need adjusted by OIC ({n.need_adjustment:+d}): {n.need_adjust_reason}")
        group = group_members.get(n.nominator_id, set())
        group_metric = sum(demand.get(uid, 0.0) for uid in group if uid != s.id)
        group_has_certified = any(a.user_id in group and in_scope(a) and a.status in HELD_STATUSES for a in awards)
        factors = {
            "first_time_equipment": not any(in_scope(a) and a.status != AwardStatus.REVOKED for a in own),
            "never_trained_anywhere": not any(a.status != AwardStatus.REVOKED for a in own),
            "need_points": need_points,
            "demand_metric": round(demand.get(s.id, 0.0) + 0.5 * group_metric, 2),
            "demand_own": demand.get(s.id, 0.0),
            "demand_group_others": group_metric,
            "tenure_months": tenure_months,
            "group_has_certified": group_has_certified,
            "cooldown": any(a.awarded_at >= cooldown_since and a.status != AwardStatus.REVOKED for a in own),
            "no_show": s.id in no_show_users,
        }
        candidates.append(
            {
                "nomination_id": n.id,
                "student_id": s.id,
                "student_name": _snapshot_name(s),
                "department_key": str(s.department_id),
                "department_name": getattr(s.department, "name", "") or "—",
                "faculty_id": n.nominator_id,
                "faculty_name": _snapshot_name(n.nominator),
                "need_category": n.need_category,
                "eligible": not reasons,
                "ineligible_reasons": sorted(set(reasons)),
                "flags": flags,
                "factors": factors,
            }
        )
    gaps = department_gaps(scope_ids, group_id, nominations, since)
    override = [str(x) for x in (policy_snap.get("underrepresented_override_department_ids") or [])]
    underrep = sorted({k for k, g in gaps.items() if g >= UNDERREP_MIN_GAP} | set(override))
    context = {
        "weights": policy_snap["scoring_weights"],
        "department_gaps": gaps,
        "underrepresented_override": override,
        "underrepresented_departments": underrep,
        "scope_equipment_ids": scope_ids,
        "tenure_anchor": tenure_anchor.isoformat() if tenure_anchor else None,
    }
    return candidates, context


# ---------------------------------------------------------------------------
# Shortlist runs
# ---------------------------------------------------------------------------
def _inputs(call: NominationCall, candidates: list[dict], context: dict, seed: str, overrides: dict) -> dict:
    caps = call.caps_snapshot or {}
    return {
        "call_id": call.pk,
        "seats": call.seats,
        "per_faculty_cap": caps.get("per_faculty_cap", 1),
        "per_department_pct": caps.get("per_department_pct", 40),
        "reserved_pct": caps.get("reserved_pct", 20),
        "tie_window": float(context["weights"].get("tie_window", 1)),
        "seed": seed,
        "overrides": overrides,
        "underrepresented_departments": context["underrepresented_departments"],
        "scoring_context": context,
        "candidates": candidates,
    }


def run_preview(call: NominationCall, actor, *, public_input: str = "") -> ShortlistRun:
    _require_manager(call, actor)
    if call.status not in (CallStatus.OPEN, CallStatus.CLOSED):
        raise TrainingError("A shortlist can be previewed only before the selection is published.")
    policy_snap = (call.caps_snapshot or {}).get("policy") or policy_snapshot(effective_policy(call.equipment))
    candidates, context = build_candidates(call, policy_snap)
    previous = call.shortlist_runs.filter(status=RunStatus.DRAFT).first()
    overrides = (previous.inputs_snapshot.get("overrides") if previous else None) or {}
    live_ids = {str(c["nomination_id"]) for c in candidates if c["eligible"]}
    overrides = {k: v for k, v in overrides.items() if k in live_ids}
    now = timezone.now()
    timestamp = now.isoformat()
    public_input = (public_input or (previous.seed_public_input if previous else "") or "").strip()[:64]
    seed = scoring.make_seed(call.pk, timestamp, public_input)
    with transaction.atomic():
        call.shortlist_runs.filter(status=RunStatus.DRAFT).update(status=RunStatus.SUPERSEDED)
        run = ShortlistRun.objects.create(
            call=call,
            policy_snapshot=policy_snap,
            inputs_snapshot=_inputs(call, candidates, context, seed, overrides),
            seed=seed,
            seed_timestamp=timestamp,
            seed_public_input=public_input,
            status=RunStatus.DRAFT,
            run_by=actor,
        )
        _materialize(run)
        for c in candidates if not call_accepts_nominations(call) else []:
            TrainingNomination.objects.filter(pk=c["nomination_id"], status__in=(NominationStatus.SUBMITTED, NominationStatus.ELIGIBLE, NominationStatus.INELIGIBLE)).update(
                status=NominationStatus.ELIGIBLE if c["eligible"] else NominationStatus.INELIGIBLE,
                eligibility_flags={"flags": c["flags"], "reasons": c["ineligible_reasons"]},
                ineligible_reason="; ".join(c["ineligible_reasons"]),
            )
        audit(actor, "shortlist.preview", run, after={"seed": seed, "candidates": len(candidates)})
    return run


def _materialize(run: ShortlistRun) -> list[ShortlistEntry]:
    placements = scoring.select(run.inputs_snapshot)
    overrides = run.inputs_snapshot.get("overrides") or {}
    run.entries.all().delete()
    rows = []
    for p in placements:
        o = overrides.get(str(p.nomination_id)) or {}
        rows.append(
            ShortlistEntry(
                run=run,
                nomination_id=p.nomination_id,
                score_total=p.score,
                score_breakdown=p.breakdown,
                rank=p.rank,
                tie_group=p.tie_group,
                lottery_key=p.lottery_key,
                outcome=p.outcome,
                seat_type=p.seat_type,
                waitlist_position=p.waitlist_position,
                constraint_note=p.note[:255],
                overridden=bool(o),
                override_outcome=o.get("outcome", ""),
                override_reason=o.get("reason", ""),
                override_by_id=o.get("by"),
                override_at=parse_datetime(o["at"]) if o.get("at") else None,
            )
        )
    return ShortlistEntry.objects.bulk_create(rows)


def override_entry(entry: ShortlistEntry, actor, *, outcome: str, reason: str) -> ShortlistRun:
    run = entry.run
    _require_manager(run.call, actor)
    if run.status != RunStatus.DRAFT:
        raise TrainingError("Overrides are made on the preview, before publishing.")
    reason = (reason or "").strip()
    outcome = (outcome or "").upper()
    overrides = dict(run.inputs_snapshot.get("overrides") or {})
    key = str(entry.nomination_id)
    if outcome in ("", "CLEAR", "NONE"):
        overrides.pop(key, None)
    else:
        if outcome not in (EntryOutcome.SELECTED, EntryOutcome.WAITLISTED, EntryOutcome.NOT_SELECTED):
            raise TrainingError("Outcome must be SELECTED, WAITLISTED or NOT_SELECTED.")
        if entry.outcome == EntryOutcome.INELIGIBLE:
            raise TrainingError("Ineligible nominations cannot be overridden; they may appeal after publication.")
        if not reason:
            raise TrainingError("A reason is required for an override.", code="reason_required")
        overrides[key] = {"outcome": outcome, "reason": reason, "by": actor.id, "at": timezone.now().isoformat()}
    with transaction.atomic():
        inputs = dict(run.inputs_snapshot)
        inputs["overrides"] = overrides
        run.inputs_snapshot = inputs
        run.save(update_fields=["inputs_snapshot"])
        _materialize(run)
        audit(actor, "shortlist.override", entry.nomination, after={"outcome": outcome or "cleared"}, note=reason)
    return run


def verify_run(run: ShortlistRun) -> dict:
    """Recompute outcomes from the stored inputs and compare with the stored entries."""
    placements = {p.nomination_id: p for p in scoring.select(run.inputs_snapshot)}
    stored = {e.nomination_id: e for e in run.entries.all()}
    mismatches = []
    for nid, entry in stored.items():
        p = placements.get(nid)
        if p is None or p.outcome != entry.outcome or p.rank != entry.rank or p.waitlist_position != entry.waitlist_position:
            mismatches.append(nid)
    recomputed_seed = scoring.make_seed(run.call_id, run.seed_timestamp, run.seed_public_input)
    return {
        "reproducible": not mismatches and recomputed_seed == run.seed and len(placements) == len(stored),
        "seed_matches": recomputed_seed == run.seed,
        "mismatched_nomination_ids": mismatches,
    }


def publish(run: ShortlistRun, actor, *, public_input: str = "") -> ShortlistRun:
    call = run.call
    _require_manager(call, actor)
    if run.status != RunStatus.DRAFT:
        raise TrainingError("Only the current preview can be published.")
    if call_accepts_nominations(call):
        raise TrainingError("Close the call (or wait for the deadline) before publishing.", code="call_open")
    cutoff = min(call.deadline, call.closed_at) if call.closed_at else call.deadline
    if run.run_at < cutoff:
        raise TrainingError("This preview was run while nominations were open. Run the preview again.", code="stale_preview")
    public_input = (public_input or run.seed_public_input or "").strip()[:64]
    if not public_input:
        raise TrainingError("Enter the public number used for the tie-break lottery.", code="public_input_required")
    policy = run.policy_snapshot
    with transaction.atomic():
        run = ShortlistRun.objects.select_for_update().get(pk=run.pk)
        call = NominationCall.objects.select_for_update().get(pk=call.pk)
        if run.status != RunStatus.DRAFT or call.status not in (CallStatus.OPEN, CallStatus.CLOSED):
            raise TrainingError("This call has already been published.")
        now = timezone.now()
        timestamp = now.isoformat()
        seed = scoring.make_seed(call.pk, timestamp, public_input)
        inputs = dict(run.inputs_snapshot)
        withdrawn = set(
            TrainingNomination.objects.filter(call=call, status=NominationStatus.WITHDRAWN).values_list("id", flat=True)
        )
        inputs["candidates"] = [c for c in inputs["candidates"] if c["nomination_id"] not in withdrawn]
        inputs["overrides"] = {k: v for k, v in (inputs.get("overrides") or {}).items() if int(k) not in withdrawn}
        inputs["seed"] = seed
        run.inputs_snapshot = inputs
        run.seed, run.seed_timestamp, run.seed_public_input = seed, timestamp, public_input
        run.status = RunStatus.PUBLISHED
        run.published_at, run.published_by = now, actor
        run.appeal_deadline = add_working_days(now, int(policy.get("appeal_working_days", 3)))
        run.save()
        entries = _materialize(run)
        confirm_hours = int(policy.get("seat_confirm_hours", 48))
        status_map = {
            EntryOutcome.SELECTED: NominationStatus.SELECTED,
            EntryOutcome.WAITLISTED: NominationStatus.WAITLISTED,
            EntryOutcome.NOT_SELECTED: NominationStatus.NOT_SELECTED,
            EntryOutcome.INELIGIBLE: NominationStatus.INELIGIBLE,
        }
        for e in entries:
            fields = {"status": status_map[e.outcome]}
            if e.outcome == EntryOutcome.SELECTED:
                fields.update(selected_at=now, confirm_deadline=now + timedelta(hours=confirm_hours))
            TrainingNomination.objects.filter(pk=e.nomination_id).update(**fields)
        call.status = CallStatus.PUBLISHED
        call.closed_at = call.closed_at or now
        call.save(update_fields=["status", "closed_at"])
        TrainingEvent.objects.filter(pk=call.event_id).update(status=EventStatus.SELECTION_PUBLISHED)
        audit(actor, "shortlist.published", run, after={"seed": seed, "timestamp": timestamp, "public_input": public_input})
    _notify_results(run, actor)
    return run


def published_run(call: NominationCall) -> ShortlistRun | None:
    return call.shortlist_runs.filter(status=RunStatus.PUBLISHED).first()


# ---------------------------------------------------------------------------
# Seats
# ---------------------------------------------------------------------------
def accept_seat(nomination: TrainingNomination, student, *, sop_acknowledged: bool = False) -> TrainingNomination:
    if nomination.student_id != student.id:
        raise TrainingError("This is not your nomination.", status=403, code="forbidden")
    with transaction.atomic():
        nomination = TrainingNomination.objects.select_for_update().select_related("call__event").get(pk=nomination.pk)
        if nomination.status != NominationStatus.SELECTED:
            raise TrainingError("There is no seat offer to accept.")
        if nomination.confirm_deadline and nomination.confirm_deadline < timezone.now():
            raise TrainingError("The confirmation deadline has passed.", code="deadline_passed")
        now = timezone.now()
        nomination.status = NominationStatus.CONFIRMED
        nomination.confirmed_at = now
        if sop_acknowledged:
            nomination.sop_acknowledged_at = now
        nomination.save()
        reg, created = Registration.objects.get_or_create(
            event=nomination.call.event,
            user=student,
            defaults={
                "source": RegistrationSource.NOMINATION,
                "nomination": nomination,
                "status": RegistrationStatus.CONFIRMED,
                "participant_snapshot": {"name": _snapshot_name(student), "department": getattr(student.department, "name", "")},
            },
        )
        if not created and reg.status in (RegistrationStatus.CANCELLED, RegistrationStatus.EXPIRED):
            reg.status = RegistrationStatus.CONFIRMED
            reg.nomination = nomination
            reg.save(update_fields=["status", "nomination", "updated_at"])
        audit(student, "seat.accepted", nomination)
    return nomination


def decline_seat(nomination: TrainingNomination, student) -> TrainingNomination:
    if nomination.student_id != student.id:
        raise TrainingError("This is not your nomination.", status=403, code="forbidden")
    with transaction.atomic():
        nomination = TrainingNomination.objects.select_for_update().get(pk=nomination.pk)
        if nomination.status not in (NominationStatus.SELECTED, NominationStatus.CONFIRMED):
            raise TrainingError("There is no seat to decline.")
        nomination.status = NominationStatus.DECLINED
        nomination.save(update_fields=["status", "updated_at"])
        Registration.objects.filter(nomination=nomination).exclude(status=RegistrationStatus.COMPLETED).update(
            status=RegistrationStatus.CANCELLED, cancelled_at=timezone.now()
        )
        audit(student, "seat.declined", nomination)
    promote_waitlist(nomination.call)
    return nomination


def expire_unconfirmed(now=None) -> int:
    now = now or timezone.now()
    expired_calls = set()
    count = 0
    for nomination in TrainingNomination.objects.filter(status=NominationStatus.SELECTED, confirm_deadline__lt=now):
        updated = TrainingNomination.objects.filter(pk=nomination.pk, status=NominationStatus.SELECTED).update(
            status=NominationStatus.EXPIRED, updated_at=now
        )
        if updated:
            count += 1
            audit(None, "seat.expired", nomination)
            expired_calls.add(nomination.call_id)
    for call in NominationCall.objects.filter(pk__in=expired_calls):
        promote_waitlist(call)
    return count


def _candidate_meta(run: ShortlistRun) -> dict[int, dict]:
    return {int(c["nomination_id"]): c for c in run.inputs_snapshot.get("candidates", [])}


def promote_waitlist(call: NominationCall) -> list[TrainingNomination]:
    run = published_run(call)
    if run is None:
        return []
    caps = call.caps_snapshot or {}
    meta = _candidate_meta(run)
    underrep = {str(x) for x in run.inputs_snapshot.get("underrepresented_departments", [])}
    promoted = []
    confirm_hours = int((run.policy_snapshot or {}).get("seat_confirm_hours", 48))
    with transaction.atomic():
        NominationCall.objects.select_for_update().filter(pk=call.pk).first()
        while True:
            occupied_ids = list(
                TrainingNomination.objects.filter(
                    call=call, status__in=(NominationStatus.SELECTED, NominationStatus.CONFIRMED)
                ).values_list("id", flat=True)
            )
            waiting_ids = set(
                TrainingNomination.objects.filter(call=call, status=NominationStatus.WAITLISTED).values_list("id", flat=True)
            )
            waitlist = [
                {"nomination_id": e.nomination_id, **_cap_keys(meta, e.nomination_id)}
                for e in run.entries.filter(outcome=EntryOutcome.WAITLISTED).order_by("waitlist_position")
                if e.nomination_id in waiting_ids
            ]
            occupied = [{"nomination_id": i, **_cap_keys(meta, i)} for i in occupied_ids]
            pick = scoring.pick_promotion(
                waitlist,
                occupied,
                seats=call.seats,
                per_faculty_cap=int(caps.get("per_faculty_cap", 1)),
                per_department_pct=int(caps.get("per_department_pct", 40)),
                reserved_pct=int(caps.get("reserved_pct", 20)),
                underrep=underrep,
            )
            if pick is None:
                break
            now = timezone.now()
            TrainingNomination.objects.filter(pk=pick["nomination_id"]).update(
                status=NominationStatus.SELECTED,
                selected_at=now,
                confirm_deadline=now + timedelta(hours=confirm_hours),
                promoted_from_waitlist=True,
                updated_at=now,
            )
            nomination = TrainingNomination.objects.select_related("student", "nominator", "call", "call__equipment").get(pk=pick["nomination_id"])
            audit(None, "seat.promoted_from_waitlist", nomination)
            promoted.append(nomination)
    for nomination in promoted:
        notify.send(
            "training_waitlist_promoted_email",
            [nomination.student, nomination.nominator],
            context=_call_context(
                call,
                summary=f"A seat opened up in {call.title} and has been offered to {_name(nomination.student)}. Confirm it within {confirm_hours} hours.",
                deadline=notify.fmt_dt(nomination.confirm_deadline),
            ),
            title="Training seat offered",
            message=f"A seat in {call.title} is offered to {_name(nomination.student)}. Confirm within {confirm_hours} hours.",
            path="/my-trainings?tab=applications",
            event="training.seat.promoted",
        )
    return promoted


def _cap_keys(meta: dict[int, dict], nomination_id: int) -> dict:
    c = meta.get(int(nomination_id))
    if c is None:
        n = TrainingNomination.objects.select_related("student").get(pk=nomination_id)
        return {"faculty_id": n.nominator_id, "department_key": str(n.student.department_id)}
    return {"faculty_id": c.get("faculty_id"), "department_key": str(c.get("department_key"))}


# ---------------------------------------------------------------------------
# Appeals
# ---------------------------------------------------------------------------
def submit_appeal(entry: ShortlistEntry, user, reason: str) -> SelectionAppeal:
    nomination = entry.nomination
    if user.id not in (nomination.student_id, nomination.nominator_id):
        raise TrainingError("Only the nominated student or the nominating faculty can appeal.", status=403, code="forbidden")
    run = entry.run
    if run.status != RunStatus.PUBLISHED:
        raise TrainingError("Appeals are possible only on a published selection.")
    if run.appeal_deadline and run.appeal_deadline < timezone.now():
        raise TrainingError("The appeal window has closed.", code="appeal_closed")
    if entry.outcome == EntryOutcome.SELECTED:
        raise TrainingError("Selected candidates cannot appeal.")
    reason = (reason or "").strip()
    if len(reason) < 20:
        raise TrainingError("Explain the appeal (at least 20 characters).")
    if entry.appeals.filter(status=AppealStatus.PENDING).exists():
        raise TrainingError("An appeal is already pending for this nomination.")
    appeal = SelectionAppeal.objects.create(entry=entry, submitted_by=user, reason=reason)
    audit(user, "appeal.submitted", appeal, note=reason)
    from iic_booking.communication.in_app import equipment_oic_users, notify_in_app

    notify_in_app(
        equipment_oic_users(run.call.equipment),
        title="Training selection appeal",
        message=f"{_name(nomination.student)} appealed the selection for {run.call.title}.",
        link="/training/oic?tab=calls",
        event="training.appeal.submitted",
        created_by=user,
    )
    return appeal


def decide_appeal(appeal: SelectionAppeal, actor, *, decision: str, note: str) -> SelectionAppeal:
    entry = appeal.entry
    call = entry.run.call
    if not access.can_decide_appeal(actor, call.equipment_id):
        raise TrainingError("Only the OIC or the Department Administrator can decide appeals.", status=403, code="forbidden")
    decision = (decision or "").upper()
    if decision not in (AppealStatus.UPHELD, AppealStatus.OVERTURNED):
        raise TrainingError("Decision must be UPHELD or OVERTURNED.")
    note = (note or "").strip()
    if not note:
        raise TrainingError("A decision note is required.")
    with transaction.atomic():
        appeal = SelectionAppeal.objects.select_for_update().get(pk=appeal.pk)
        if appeal.status != AppealStatus.PENDING:
            raise TrainingError("This appeal is already decided.")
        appeal.status = decision
        appeal.decided_by = actor
        appeal.decided_at = timezone.now()
        appeal.decision_note = note
        appeal.save()
        if decision == AppealStatus.OVERTURNED:
            hours = int((entry.run.policy_snapshot or {}).get("seat_confirm_hours", 48))
            now = timezone.now()
            TrainingNomination.objects.filter(pk=entry.nomination_id).update(
                status=NominationStatus.SELECTED,
                selected_on_appeal=True,
                selected_at=now,
                confirm_deadline=now + timedelta(hours=hours),
                updated_at=now,
            )
        audit(actor, f"appeal.{decision.lower()}", appeal, note=note)
    nomination = entry.nomination
    summary = (
        f"The appeal for {_name(nomination.student)} was accepted and a seat is offered. Confirm it in My Trainings."
        if decision == AppealStatus.OVERTURNED
        else f"The appeal for {_name(nomination.student)} was reviewed; the published decision stands."
    )
    notify.send(
        "training_appeal_decided_email",
        [nomination.student, nomination.nominator],
        context=_call_context(call, summary=summary, remarks=note),
        title="Selection appeal decided",
        message=summary,
        path="/my-trainings?tab=applications",
        actor=actor,
        event="training.appeal.decided",
    )
    return appeal


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
BREAKDOWN_COLUMNS = (
    "first_time_equipment",
    "never_trained_anywhere",
    "research_need",
    "demand",
    "tenure",
    "department_underrepresentation",
    "group_no_certified",
    "cooldown",
    "prior_no_show",
)


def export_csv(run: ShortlistRun) -> str:
    meta = _candidate_meta(run)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["rank", "student", "email", "department", "nominating_faculty", "need_category", "score", *BREAKDOWN_COLUMNS,
         "outcome", "seat_type", "waitlist_position", "tie_group", "lottery_key", "note", "overridden", "override_reason", "flags"]
    )
    entries = run.entries.select_related("nomination__student", "nomination__nominator").order_by("rank", "id")
    for e in entries:
        c = meta.get(e.nomination_id, {})
        b = e.score_breakdown or {}
        writer.writerow(
            [e.rank or "", c.get("student_name") or _name(e.nomination.student), e.nomination.student.email,
             c.get("department_name", ""), _name(e.nomination.nominator) or c.get("faculty_name", ""),
             e.nomination.need_category,
             e.score_total if e.outcome != EntryOutcome.INELIGIBLE else "", *[b.get(k, "") for k in BREAKDOWN_COLUMNS],
             e.outcome, e.seat_type, e.waitlist_position or "", e.tie_group or "", e.lottery_key, e.constraint_note,
             "yes" if e.overridden else "", e.override_reason, " | ".join(c.get("flags", []))]
        )
    writer.writerow([])
    writer.writerow(["seed", run.seed, "timestamp", run.seed_timestamp, "public_input", run.seed_public_input, "status", run.status])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _require_manager(call: NominationCall, actor) -> None:
    if not access.can_manage_equipment(actor, call.equipment_id):
        raise TrainingError("Only the equipment's OIC can manage this call.", status=403, code="forbidden")


def _name(user) -> str:
    return get_user_display_name(user)


def _snapshot_name(user) -> str:
    """Stored name (no display prefix) for shortlist and participant snapshots."""
    return ((getattr(user, "name", "") or "").strip() or getattr(user, "email", "")) if user else ""


def _call_context(call: NominationCall, **extra) -> dict:
    ctx = {
        "reference": call.reference,
        "equipment_name": call.equipment.name,
        "equipment_code": call.equipment.code,
        "title": call.title,
        "participants": f"{call.seats} seats",
    }
    ctx.update(extra)
    return ctx


def _notify_call_open(call: NominationCall, actor) -> None:
    from iic_booking.users.models import User
    from iic_booking.users.models.department import DepartmentType

    faculty = User.objects.filter(
        user_type=UserType.FACULTY, is_active=True, department__department_type=DepartmentType.INTERNAL
    )
    notify.send(
        "training_call_open_faculty_email",
        list(faculty),
        context=_call_context(call, summary=f"Nominations are open for {call.title} ({call.seats} seats). Nominate students from your group before the deadline.", deadline=notify.fmt_dt(call.deadline)),
        title=f"Training nominations open: {call.equipment.name}",
        message=f"Nominate students for {call.title} before {notify.fmt_dt(call.deadline)}.",
        path="/training/nominations",
        actor=actor,
        event="training.call.opened",
    )


def _notify_results(run: ShortlistRun, actor) -> None:
    call = run.call
    for e in run.entries.select_related("nomination__student", "nomination__nominator"):
        n = e.nomination
        if e.outcome == EntryOutcome.SELECTED:
            summary = f"{_name(n.student)} has been selected for {call.title}. Confirm the seat within the deadline in My Trainings."
        elif e.outcome == EntryOutcome.WAITLISTED:
            summary = f"{_name(n.student)} is on the waitlist (position {e.waitlist_position}) for {call.title}. Seats freed by others are offered in waitlist order."
        elif e.outcome == EntryOutcome.INELIGIBLE:
            summary = f"{_name(n.student)} was not eligible for {call.title}: {e.constraint_note}."
        else:
            summary = f"{_name(n.student)} was not selected for {call.title}."
        if e.outcome != EntryOutcome.SELECTED:
            summary += f" Appeals are open until {notify.fmt_dt(run.appeal_deadline)}."
        notify.send(
            "training_selection_result_email",
            [n.student, n.nominator],
            context=_call_context(
                call,
                summary=summary,
                status=e.get_outcome_display(),
                deadline=notify.fmt_dt(n.confirm_deadline) if e.outcome == EntryOutcome.SELECTED else "",
            ),
            title=f"Training selection: {e.get_outcome_display()}",
            message=summary,
            path="/my-trainings?tab=applications",
            actor=actor,
            event="training.selection.published",
        )

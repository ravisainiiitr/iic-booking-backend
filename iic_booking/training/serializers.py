"""Plain dict builders for the training API (read side)."""

from __future__ import annotations

from decimal import Decimal

from django.utils import timezone

from . import access
from .models import (
    CallStatus,
    DemoRequest,
    DemoStatus,
    NominationCall,
    NominationStatus,
    RunStatus,
    ShortlistEntry,
    ShortlistRun,
    TrainingEvent,
    TrainingNomination,
    TrainingSession,
)


def iso(value):
    return value.isoformat() if value else None


def money(value):
    return str(value) if isinstance(value, Decimal) else value


def user_brief(user) -> dict | None:
    if user is None:
        return None
    return {
        "id": user.id,
        "name": (user.name or "").strip() or user.email,
        "email": user.email,
        "department": getattr(getattr(user, "department", None), "name", "") or "",
        "user_type": user.user_type,
    }


def equipment_brief(eq) -> dict | None:
    if eq is None:
        return None
    return {
        "equipment_id": eq.equipment_id,
        "code": eq.code,
        "name": eq.name,
        "department": getattr(getattr(eq, "internal_department", None), "name", "") or "",
    }


def demo_out(req: DemoRequest, viewer, *, detail: bool = False) -> dict:
    is_owner = req.requester_id == viewer.id
    manage = access.can_manage_equipment(viewer, req.equipment_id)
    attend = access.can_mark_attendance(viewer, req.equipment_id)
    now = timezone.now()
    data = {
        "id": req.pk,
        "reference": req.reference,
        "status": req.status,
        "status_label": req.get_status_display(),
        "requester": user_brief(req.requester),
        "equipment": equipment_brief(req.equipment),
        "purpose": req.purpose,
        "purpose_label": req.get_purpose_display(),
        "course_code": req.course_code,
        "course_name": req.course_name,
        "participants_requested": req.participants_requested,
        "requested_duration_minutes": req.requested_duration_minutes,
        "preferred_windows": req.preferred_windows,
        "notes": req.notes,
        "approved_duration_minutes": req.approved_duration_minutes,
        "approved_participants": req.approved_participants,
        "approved_start_at": iso(req.approved_start_at),
        "approved_end_at": iso(req.approved_end_at),
        "curtailed": req.curtailed,
        "curtail_reason_code": req.curtail_reason_code,
        "curtail_reason_label": req.get_curtail_reason_code_display() if req.curtail_reason_code else "",
        "oic_remarks": req.oic_remarks,
        "proposed_start_at": iso(req.proposed_start_at),
        "proposed_end_at": iso(req.proposed_end_at),
        "proposal_expires_at": iso(req.proposal_expires_at),
        "counter_used": req.counter_used,
        "faculty_response": req.faculty_response,
        "charge_mode": req.charge_mode,
        "rate_per_hour": money(req.rate_per_hour),
        "charge_amount": money(req.charge_amount),
        "charged": bool(req.wallet_txn_id),
        "refund_amount": money(req.refund_amount),
        "cancelled_by_side": req.cancelled_by_side,
        "cancel_reason": req.cancel_reason,
        "attended_count": req.attended_count,
        "submitted_at": iso(req.submitted_at),
        "decided_at": iso(req.decided_at),
        "decided_by": user_brief(req.decided_by) if req.decided_by_id else None,
        "sla_escalated": bool(req.sla_escalated_at),
        "event_id": req.event_id,
        "permissions": {
            "withdraw": is_owner and req.status in (DemoStatus.SUBMITTED, DemoStatus.UNDER_REVIEW),
            "respond": is_owner
            and req.status == DemoStatus.PROPOSED_ALTERNATIVE
            and (not req.proposal_expires_at or req.proposal_expires_at > now),
            "counter": is_owner and req.status == DemoStatus.PROPOSED_ALTERNATIVE and not req.counter_used,
            "decide": manage and req.status in (DemoStatus.SUBMITTED, DemoStatus.UNDER_REVIEW),
            "schedule": manage and req.status in (DemoStatus.APPROVED, DemoStatus.SCHEDULED),
            "cancel": (is_owner or manage) and req.status in (DemoStatus.APPROVED, DemoStatus.SCHEDULED, DemoStatus.PROPOSED_ALTERNATIVE),
            "attendance": attend and req.status in (DemoStatus.SCHEDULED, DemoStatus.COMPLETED),
            "complete": attend and req.status == DemoStatus.SCHEDULED,
        },
    }
    if detail:
        data["participants"] = [user_brief(u) for u in req.participant_users.select_related("department")]
        data["participant_list_text"] = req.participant_list_text
        data["revisions"] = [
            {
                "id": r.id,
                "action": r.action,
                "from_status": r.from_status,
                "to_status": r.to_status,
                "before": r.before,
                "after": r.after,
                "reason_code": r.reason_code,
                "reason": r.reason,
                "actor": user_brief(r.actor) if r.actor_id else None,
                "created_at": iso(r.created_at),
            }
            for r in req.revisions.select_related("actor").order_by("created_at", "id")
        ]
    return data


def session_out(s: TrainingSession) -> dict:
    return {
        "id": s.pk,
        "event_id": s.event_id,
        "seq": s.seq,
        "title": s.title,
        "session_type": s.session_type,
        "start_at": iso(s.start_at),
        "end_at": iso(s.end_at),
        "location": s.location,
        "status": s.status,
        "status_label": s.get_status_display(),
        "slots_reserved": s.slot_reservations.filter(released_at__isnull=True).exists(),
        "attendance_marked_at": iso(s.attendance_marked_at),
    }


def event_out(e: TrainingEvent, *, with_sessions: bool = True) -> dict:
    data = {
        "id": e.pk,
        "title": e.title,
        "kind": e.kind,
        "kind_label": e.get_kind_display(),
        "status": e.status,
        "status_label": e.get_status_display(),
        "equipment": equipment_brief(e.equipment),
        "level": e.level_awarded.code if e.level_awarded_id else "",
        "capacity": e.capacity,
        "venue": e.venue,
        "description": e.description,
        "registration_closes_at": iso(e.registration_closes_at),
        "completed_at": iso(e.completed_at),
        "cancelled_reason": e.cancelled_reason,
        "registrations": e.registrations.exclude(status__in=("CANCELLED", "EXPIRED")).count(),
    }
    if with_sessions:
        data["sessions"] = [session_out(s) for s in e.sessions.order_by("seq", "start_at")]
    return data


def call_out(call: NominationCall, viewer) -> dict:
    manage = access.can_manage_equipment(viewer, call.equipment_id)
    now = timezone.now()
    nominations = call.nominations.exclude(status=NominationStatus.WITHDRAWN)
    run = call.shortlist_runs.filter(status=RunStatus.PUBLISHED).first()
    return {
        "id": call.pk,
        "reference": call.reference,
        "title": call.title,
        "equipment": equipment_brief(call.equipment),
        "event_id": call.event_id,
        "seats": call.seats,
        "deadline": iso(call.deadline),
        "status": call.status,
        "status_label": call.get_status_display(),
        "accepting": call.status == CallStatus.OPEN and call.deadline > now,
        "notes": call.notes,
        "eligibility": call.eligibility,
        "caps": {k: v for k, v in (call.caps_snapshot or {}).items() if k != "policy"},
        "policy_version": call.policy_version,
        "nominations_count": nominations.count(),
        "confirmed_interest_count": nominations.filter(student_confirmed_at__isnull=False).count(),
        "my_nominations_count": nominations.filter(nominator=viewer).count() if access.is_faculty(viewer) else 0,
        "opened_at": iso(call.opened_at),
        "published_run_id": run.pk if run else None,
        "appeal_deadline": iso(run.appeal_deadline) if run else None,
        "can_manage": manage,
    }


def nomination_out(n: TrainingNomination, viewer) -> dict:
    entry = None
    run = n.call.shortlist_runs.filter(status=RunStatus.PUBLISHED).first()
    if run:
        entry = run.entries.filter(nomination=n).first()
    now = timezone.now()
    is_student = n.student_id == viewer.id
    is_nominator = n.nominator_id == viewer.id
    appeal_open = bool(
        entry
        and entry.outcome != "SELECTED"
        and run.appeal_deadline
        and run.appeal_deadline > now
        and not entry.appeals.filter(status="PENDING").exists()
    )
    return {
        "id": n.pk,
        "call": {
            "id": n.call_id,
            "reference": n.call.reference,
            "title": n.call.title,
            "deadline": iso(n.call.deadline),
            "status": n.call.status,
            "equipment": equipment_brief(n.call.equipment),
            "event_id": n.call.event_id,
        },
        "student": user_brief(n.student),
        "nominator": user_brief(n.nominator),
        "need_category": n.need_category,
        "need_category_label": n.get_need_category_display(),
        "justification": n.justification,
        "expected_hours_month": n.expected_hours_month,
        "status": n.status,
        "status_label": n.get_status_display(),
        "student_confirmed_at": iso(n.student_confirmed_at),
        "confirm_deadline": iso(n.confirm_deadline),
        "confirmed_at": iso(n.confirmed_at),
        "ineligible_reason": n.ineligible_reason,
        "flags": (n.eligibility_flags or {}).get("flags", []),
        "need_adjustment": n.need_adjustment,
        "need_adjust_reason": n.need_adjust_reason,
        "selected_on_appeal": n.selected_on_appeal,
        "promoted_from_waitlist": n.promoted_from_waitlist,
        "result": entry_public(entry) if entry else None,
        "permissions": {
            "withdraw": (is_student or is_nominator)
            and n.status in (NominationStatus.SUBMITTED, NominationStatus.ELIGIBLE, NominationStatus.INELIGIBLE)
            and n.call.status in (CallStatus.OPEN, CallStatus.CLOSED),
            "confirm_interest": is_student
            and n.status in (NominationStatus.SUBMITTED, NominationStatus.ELIGIBLE, NominationStatus.INELIGIBLE)
            and not n.student_confirmed_at
            and n.call.status == CallStatus.OPEN and n.call.deadline > now,
            "accept_seat": is_student and n.status == NominationStatus.SELECTED and (not n.confirm_deadline or n.confirm_deadline > now),
            "decline_seat": is_student and n.status in (NominationStatus.SELECTED, NominationStatus.CONFIRMED),
            "appeal": (is_student or is_nominator) and appeal_open,
        },
    }


def entry_public(e: ShortlistEntry) -> dict:
    return {
        "entry_id": e.pk,
        "outcome": e.outcome,
        "outcome_label": e.get_outcome_display(),
        "rank": e.rank,
        "score_total": float(e.score_total),
        "score_breakdown": e.score_breakdown,
        "waitlist_position": e.waitlist_position,
        "seat_type": e.seat_type,
        "note": e.constraint_note,
        "appeals": [
            {"id": a.pk, "status": a.status, "reason": a.reason, "decision_note": a.decision_note, "created_at": iso(a.created_at)}
            for a in e.appeals.order_by("created_at")
        ],
    }


def run_out(run: ShortlistRun, *, entries: bool = True) -> dict:
    meta = {int(c["nomination_id"]): c for c in (run.inputs_snapshot or {}).get("candidates", [])}
    data = {
        "id": run.pk,
        "call_id": run.call_id,
        "status": run.status,
        "seed": run.seed,
        "seed_timestamp": run.seed_timestamp,
        "seed_public_input": run.seed_public_input,
        "run_at": iso(run.run_at),
        "published_at": iso(run.published_at),
        "appeal_deadline": iso(run.appeal_deadline),
        "seats": (run.inputs_snapshot or {}).get("seats"),
        "caps": {
            k: (run.inputs_snapshot or {}).get(k)
            for k in ("per_faculty_cap", "per_department_pct", "reserved_pct", "tie_window")
        },
        "underrepresented_departments": (run.inputs_snapshot or {}).get("underrepresented_departments", []),
        "department_gaps": ((run.inputs_snapshot or {}).get("scoring_context") or {}).get("department_gaps", {}),
        "weights": (run.policy_snapshot or {}).get("scoring_weights", {}),
    }
    if entries:
        rows = []
        for e in run.entries.select_related("nomination__student", "nomination__nominator").order_by("rank", "id"):
            c = meta.get(e.nomination_id, {})
            row = entry_public(e)
            row.update(
                {
                    "nomination_id": e.nomination_id,
                    "student": user_brief(e.nomination.student),
                    "nominator": user_brief(e.nomination.nominator),
                    "department_key": c.get("department_key"),
                    "department_name": c.get("department_name", ""),
                    "need_category": e.nomination.need_category,
                    "justification": e.nomination.justification,
                    "flags": c.get("flags", []),
                    "ineligible_reasons": c.get("ineligible_reasons", []),
                    "factors": c.get("factors", {}),
                    "tie_group": e.tie_group,
                    "lottery_key": e.lottery_key,
                    "overridden": e.overridden,
                    "override_outcome": e.override_outcome,
                    "override_reason": e.override_reason,
                    "nomination_status": e.nomination.status,
                }
            )
            rows.append(row)
        data["entries"] = rows
    return data


def run_public(run: ShortlistRun) -> dict:
    """Published ranking visible to the call's students and nominators: names, departments, scores, outcomes."""
    meta = {int(c["nomination_id"]): c for c in (run.inputs_snapshot or {}).get("candidates", [])}
    rows = []
    for e in run.entries.select_related("nomination__student").order_by("rank", "id"):
        c = meta.get(e.nomination_id, {})
        rows.append(
            {
                "rank": e.rank,
                "student_name": c.get("student_name") or user_brief(e.nomination.student)["name"],
                "department_name": c.get("department_name", ""),
                "score_total": float(e.score_total) if e.outcome != "INELIGIBLE" else None,
                "outcome": e.outcome,
                "outcome_label": e.get_outcome_display(),
                "waitlist_position": e.waitlist_position,
                "seat_type": e.seat_type,
                "tie_group": e.tie_group,
                "overridden": e.overridden,
            }
        )
    return {
        "id": run.pk,
        "published_at": iso(run.published_at),
        "appeal_deadline": iso(run.appeal_deadline),
        "seed": run.seed,
        "seed_timestamp": run.seed_timestamp,
        "seed_public_input": run.seed_public_input,
        "entries": rows,
    }


def award_out(a) -> dict:
    return {
        "id": a.pk,
        "user": user_brief(a.user),
        "equipment": equipment_brief(a.equipment),
        "level": a.level.code,
        "level_name": a.level.name,
        "status": a.status,
        "awarded_at": iso(a.awarded_at),
        "valid_until": iso(a.valid_until),
        "source_event_id": a.source_event_id,
    }


def policy_out(p) -> dict:
    from .policy import POLICY_FIELDS

    data = {f: money(getattr(p, f)) for f in POLICY_FIELDS}
    data.update(
        {
            "id": p.pk,
            "scope": p.scope,
            "department_id": p.department_id,
            "department_name": getattr(p.department, "name", "") if p.department_id else "",
            "equipment": equipment_brief(p.equipment) if p.equipment_id else None,
            "version": p.version,
            "is_active": p.is_active,
            "published_at": iso(p.published_at),
            "created_by": user_brief(p.created_by) if p.created_by_id else None,
            "effective_weights": p.weights(),
        }
    )
    return data

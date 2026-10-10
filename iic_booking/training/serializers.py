"""Plain dict builders for the training API (read side)."""

from __future__ import annotations

from decimal import Decimal

from django.utils import timezone

from iic_booking.users.display import get_user_display_name

from . import access
from .models import (
    AwardStatus,
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
        "name": get_user_display_name(user),
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


def _demo_charge_text(req: DemoRequest) -> str:
    from .demo import charge_text

    return charge_text(req)


def _demo_waiver(req: DemoRequest) -> dict | None:
    from .demo import waiver_info

    return waiver_info(req)


def _can_waive(req: DemoRequest, manage: bool) -> bool:
    from .demo import WAIVABLE_STATUSES

    return manage and req.status in WAIVABLE_STATUSES and req.charge_mode == "WALLET" and req.charge_amount > 0


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
        "charge_text": _demo_charge_text(req),
        "charge_waiver": _demo_waiver(req),
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
            "waive": _can_waive(req, manage),
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
    from .certification import effective_status, verify_path

    status = effective_status(a)
    return {
        "id": a.pk,
        "user": user_brief(a.user),
        "equipment": equipment_brief(a.equipment),
        "level": a.level.code,
        "level_name": a.level.name,
        "level_rank": a.level.rank,
        "status": status,
        "status_label": AwardStatus(status).label if status in AwardStatus.values else status,
        "awarded_at": iso(a.awarded_at),
        "valid_until": iso(a.valid_until),
        "last_used_at": iso(a.last_used_at),
        "source_event_id": a.source_event_id,
        "certificate_no": a.certificate_no or "",
        "verify_path": verify_path(a),
        "suspended_until": iso(a.suspended_until),
        "suspend_reason": a.suspend_reason,
        "revoke_reason": a.revoke_reason,
    }


def assessment_out(a) -> dict:
    return {
        "id": a.pk,
        "user": user_brief(a.user),
        "equipment": equipment_brief(a.equipment),
        "event_id": a.event_id,
        "registration_id": a.registration_id,
        "target_level": a.target_level.code,
        "target_level_name": a.target_level.name,
        "assessor": user_brief(a.assessor) if a.assessor_id else None,
        "theory_score_pct": money(a.theory_score_pct),
        "practical_items": a.practical_items,
        "practical_score_pct": money(a.practical_score_pct),
        "result": a.result,
        "result_label": a.get_result_display(),
        "scope_note": a.scope_note,
        "remarks": a.remarks,
        "validity_months": a.validity_months,
        "prerequisite_waiver_reason": a.prerequisite_waiver_reason,
        "award_id": a.award_id,
        "certificate_no": (a.award.certificate_no or "") if a.award_id else "",
        "signed_off_by": user_brief(a.signed_off_by) if a.signed_off_by_id else None,
        "signed_off_at": iso(a.signed_off_at),
        "awaiting_sign_off": a.result == "PASS" and not a.award_id,
        "assessed_at": iso(a.assessed_at),
    }


def roster_out(e, *, basis: tuple[bool, str] | None = None) -> dict:
    from .roster import basis as roster_basis

    ok, why = basis or roster_basis(e)
    return {
        "id": e.pk,
        "equipment": equipment_brief(e.equipment),
        "user": user_brief(e.user),
        "source": e.source,
        "source_label": e.get_source_display(),
        "status": e.status,
        "status_label": e.get_status_display(),
        "status_reason": e.status_reason,
        "eligible": ok,
        "basis": why,
        "award": award_out(e.award) if e.award_id else None,
        "faculty": user_brief(e.faculty) if e.faculty_id else None,
        "department_name": getattr(e.department, "name", "") if e.department_id else "",
        "max_hours_week": e.max_hours_week,
        "note": e.note,
        "created_at": iso(e.created_at),
    }


def shift_out(sh) -> dict:
    return {
        "id": sh.pk,
        "allocation_id": sh.allocation_id,
        "start_at": iso(sh.start_at),
        "end_at": iso(sh.end_at),
        "planned_minutes": sh.planned_minutes,
        "daily_slot_ids": sh.daily_slot_ids,
        "status": sh.status,
        "status_label": sh.get_status_display(),
        "check_in_at": iso(sh.check_in_at),
        "check_out_at": iso(sh.check_out_at),
        "operated_minutes": sh.operated_minutes,
        "hours_source": sh.hours_source,
        "hours_source_label": sh.get_hours_source_display() if sh.hours_source else "",
        "verified_by": user_brief(sh.verified_by) if sh.verified_by_id else None,
        "verified_at": iso(sh.verified_at),
        "remarks": sh.remarks,
    }


def allocation_out(al, viewer=None, *, shifts: bool = True) -> dict:
    rows = list(al.shifts.order_by("start_at", "id")) if shifts else []
    can_manage = bool(viewer) and access.can_manage_equipment(viewer, al.equipment_id)
    data = {
        "id": al.pk,
        "reference": al.reference,
        "equipment": equipment_brief(al.equipment),
        "operator": user_brief(al.operator),
        "status": al.status,
        "status_label": al.get_status_display(),
        "requires_confirmation": al.requires_confirmation,
        "confirm_by": iso(al.confirm_by),
        "responded_at": iso(al.responded_at),
        "response_channel": al.response_channel,
        "decline_reason": al.decline_reason,
        "reminder_sent_at": iso(al.reminder_sent_at),
        "escalated_at": iso(al.escalated_at),
        "title": al.title,
        "note": al.note,
        "suggested_rank": al.suggested_rank,
        "override_reason": al.override_reason,
        "academic_year": al.academic_year,
        "hourly_rate": money(al.hourly_rate),
        "planned_minutes": al.planned_minutes,
        "allocated_by": user_brief(al.allocated_by) if al.allocated_by_id else None,
        "created_at": iso(al.created_at),
        "cancelled_at": iso(al.cancelled_at),
        "cancel_reason": al.cancel_reason,
        "completed_at": iso(al.completed_at),
        "first_start": iso(rows[0].start_at) if rows else None,
        "last_end": iso(rows[-1].end_at) if rows else None,
        "operated_minutes": sum(int(r.operated_minutes or 0) for r in rows if r.status == "COMPLETED"),
        "shift_count": len(rows),
        "can_manage": can_manage,
        "can_respond": bool(viewer) and viewer.pk == al.operator_id and al.status == "PENDING",
    }
    if shifts:
        data["shifts"] = [shift_out(r) for r in rows]
    if can_manage:
        data["fairness_snapshot"] = al.fairness_snapshot
    return data


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

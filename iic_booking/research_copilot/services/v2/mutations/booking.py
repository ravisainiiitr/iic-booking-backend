"""
Phase B booking mutation wrappers.

prepare_* always builds confirmation proposals (validation + estimate).
execute_* calls existing portal domain APIs and is gated by COPILOT_BOOKING_* flags.

Wallet recharge/credit are Phase C — never enabled here.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from iic_booking.research_copilot.services.v2.mutations import booking_mutation_allowed
from iic_booking.research_copilot.services.v2.mutations import domain_bridge
from iic_booking.research_copilot.services.v2.mutations import idempotency as idem
from iic_booking.research_copilot.services.v2.mutations import proposals as prop_store


def _flag(name: str, user=None) -> bool:
    """Booking flags honor global OR controlled E2E test-account allowlist."""
    if name.startswith("COPILOT_BOOKING_"):
        return booking_mutation_allowed(user, name)
    return bool(getattr(settings, name, False))


def _safe_error(code: str, message: str, **extra) -> dict[str, Any]:
    return {"ok": False, "error": code, "message": message, **extra}


def _audit(*, user, action: str, detail: dict[str, Any], message: str = "") -> None:
    try:
        from iic_booking.research_copilot.models import AuditAction
        from iic_booking.research_copilot.services import audit as audit_svc

        audit_svc.write_audit(
            action=AuditAction.TOOL_EXECUTED,
            message=message or action,
            user=user,
            detail={k: v for k, v in detail.items() if k not in {"confirmation_token", "token"}},
        )
    except Exception:  # noqa: BLE001
        pass


def _wallet_balance(user) -> Any:
    try:
        from iic_booking.research_copilot.services import tools as tools_svc

        result = tools_svc._get_wallet(arguments={}, user=user)
        data = (result or {}).get("data") or {}
        return data.get("balance")
    except Exception:  # noqa: BLE001
        return None


def _estimate_for_equipment(*, user, equipment_id: int) -> tuple[Any, str | None]:
    from iic_booking.research_copilot.services import tools as tools_svc

    result = tools_svc._estimate_booking_cost(arguments={"equipment_id": equipment_id}, user=user)
    if not result.get("ok"):
        return None, (result.get("message") or "estimate_failed")
    data = result.get("data") or {}
    return data.get("estimate"), data.get("equipment_name")


def _load_slot(*, slot_id: int, equipment_id: int | None = None):
    from iic_booking.equipment.models import DailySlot, SlotStatus

    qs = DailySlot.objects.select_related("slot_master", "slot_master__equipment").filter(pk=slot_id)
    slot = qs.first()
    if not slot:
        return None, "SLOT_NOT_FOUND"
    if equipment_id is not None and int(slot.slot_master.equipment_id) != int(equipment_id):
        return None, "SLOT_EQUIPMENT_MISMATCH"
    if slot.status != SlotStatus.AVAILABLE or slot.booking_id is not None:
        return None, "SLOT_UNAVAILABLE"
    return slot, None


def _booking_owned(*, user, booking_id: int):
    from iic_booking.equipment.models import Booking

    try:
        b = Booking.objects.select_related("equipment").prefetch_related("daily_slots").get(
            booking_id=int(booking_id), user=user
        )
        return b, None
    except Booking.DoesNotExist:
        return None, "BOOKING_NOT_FOUND"
    except (TypeError, ValueError):
        return None, "BOOKING_NOT_FOUND"


def prepare_booking_create(
    *,
    user,
    equipment_id: int | None = None,
    slot_id: int | None = None,
    slot_ids: list[int] | None = None,
    sample_count: int = 1,
    number_of_samples: int | None = None,
    text: str = "",
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate and build CREATE_BOOKING confirmation proposal (does not book)."""
    if user is None or not getattr(user, "is_authenticated", False):
        return _safe_error("AUTH_REQUIRED", "Sign in to prepare a booking.")

    from iic_booking.users.legacy_ledger.booking_lock import end_user_booking_is_locked

    locked, lock_message = end_user_booking_is_locked(user)
    if locked:
        return _safe_error("BOOKING_LOCKED", lock_message or "Booking is temporarily locked.")

    ctx = context or {}
    eid = equipment_id or ctx.get("equipment_id") or ctx.get("last_equipment_id")
    ids = list(slot_ids or [])
    if slot_id:
        ids = [int(slot_id)] + ids
    if not ids and ctx.get("slot_id"):
        ids = [int(ctx["slot_id"])]
    if not ids and ctx.get("earliest_slot_id"):
        ids = [int(ctx["earliest_slot_id"])]

    # Resolve equipment from text if needed
    if not eid:
        from iic_booking.research_copilot.services.v2.equipment_resolver import resolve_equipment

        resolved = resolve_equipment(text=text or "", user=user, context_equipment_id=ctx.get("last_equipment_id"))
        if resolved.confidence == "AMBIGUOUS":
            return {
                "ok": True,
                "action": "CREATE_BOOKING",
                "status": "CLARIFICATION",
                "requires_confirmation": False,
                "message": "Which equipment should I book?",
                "candidates": [{"id": c.id, "name": c.name, "url": c.url} for c in resolved.candidates],
            }
        if resolved.confidence == "NOT_FOUND" or not resolved.equipment_id:
            return _safe_error("EQUIPMENT_REQUIRED", "Specify equipment (e.g. FESEM) and an available slot.")
        eid = resolved.equipment_id

    eid = int(eid)
    from iic_booking.equipment.models import Equipment

    eq = Equipment.objects.filter(pk=eid).first()
    if not eq:
        return _safe_error("EQUIPMENT_NOT_FOUND", "Equipment not found.")

    if not ids:
        return {
            "ok": True,
            "action": "CREATE_BOOKING",
            "status": "NEEDS_SLOT",
            "requires_confirmation": False,
            "equipment_id": eid,
            "equipment_name": eq.name,
            "message": f"Choose an available slot for **{eq.name}** before confirming a booking.",
            "href": f"/book-equipment?equipment={eid}",
            "executable": False,
        }

    # Validate first slot (multi-slot: all must be available)
    slots = []
    for sid in ids:
        slot, err = _load_slot(slot_id=int(sid), equipment_id=eid)
        if err:
            return _safe_error(err, "That slot is no longer available. Search again for available slots.")
        slots.append(slot)

    samples = int(number_of_samples or sample_count or 1)
    if samples < 1:
        samples = 1

    estimate, _ename = _estimate_for_equipment(user=user, equipment_id=eid)
    balance = _wallet_balance(user)

    start = slots[0].start_datetime
    end = slots[-1].end_datetime
    duration_min = None
    if start and end:
        duration_min = int((end - start).total_seconds() // 60)

    payload = {
        "equipment_id": eid,
        "equipment_name": eq.name,
        "slot_ids": [int(s.pk) for s in slots],
        "date": slots[0].date.isoformat() if slots[0].date else None,
        "start_time": start.isoformat() if start else None,
        "end_time": end.isoformat() if end else None,
        "duration_minutes": duration_min,
        "sample_count": samples,
        "number_of_samples": samples,
        "estimated_amount": float(estimate) if estimate is not None else None,
        "wallet_balance": balance,
        "input_values": {"A": str(samples)},
    }
    if estimate is not None and balance is not None:
        try:
            rem = Decimal(str(balance)) - Decimal(str(estimate))
            payload["approx_balance_after"] = str(rem)
        except Exception:  # noqa: BLE001
            payload["approx_balance_after"] = None

    record = prop_store.create_proposal(user=user, action="CREATE_BOOKING", payload=payload)
    executable = _flag("COPILOT_BOOKING_CREATE", user=user)

    _audit(
        user=user,
        action="prepare_booking_create",
        detail={"proposal_id": record["proposal_id"], "equipment_id": eid, "slot_ids": payload["slot_ids"], "ok": True},
    )

    return {
        "ok": True,
        "action": "CREATE_BOOKING",
        "status": "READY_FOR_CONFIRMATION",
        "proposal_id": record["proposal_id"],
        "confirmation_token": record["confirmation_token"],
        "confirmation_required": True,
        "requires_confirmation": True,
        "executable": executable,
        "expires_at": record["expires_at"],
        "equipment_id": eid,
        "equipment_name": eq.name,
        "mode": getattr(eq, "profile_type", None) or "",
        "date": payload["date"],
        "start_time": payload["start_time"],
        "end_time": payload["end_time"],
        "duration_minutes": duration_min,
        "sample_count": samples,
        "estimated_amount": payload["estimated_amount"],
        "wallet_balance": balance,
        "approx_balance_after": payload.get("approx_balance_after"),
        "message": (
            "Review the booking summary and confirm. "
            + ("Copilot booking execute is enabled." if executable else "Execute is currently disabled (flag OFF) — Confirm will not create a booking until enablement.")
        ),
        "portal_href": f"/book-equipment?equipment={eid}",
    }


def execute_booking_create(
    *,
    user,
    proposal_id: str,
    confirmation_token: str,
    idempotency_key: str = "",
) -> dict[str, Any]:
    if not _flag("COPILOT_BOOKING_CREATE", user=user):
        return _safe_error(
            "COPILOT_BOOKING_CREATE_DISABLED",
            "Booking create via Copilot is disabled. Use the portal Book flow, or ask an administrator to enable COPILOT_BOOKING_CREATE after controlled E2E.",
            proposal_id=proposal_id,
        )

    key = idempotency_key or idem.make_idempotency_key(user=user, action="CREATE_BOOKING", proposal_id=proposal_id)
    cached = idem.get_cached_result(user=user, idempotency_key=key)
    if cached:
        return {**cached, "idempotent_replay": True}

    prop, err = prop_store.validate_proposal_for_user(
        user=user, proposal_id=proposal_id, confirmation_token=confirmation_token, expected_action="CREATE_BOOKING"
    )
    if err:
        return _safe_error(err, _human_proposal_error(err))

    payload = prop.get("payload") or {}
    eid = int(payload["equipment_id"])
    slot_ids = [int(x) for x in (payload.get("slot_ids") or [])]

    # Revalidate slots before execute
    for sid in slot_ids:
        _slot, serr = _load_slot(slot_id=sid, equipment_id=eid)
        if serr:
            _audit(user=user, action="execute_booking_create", detail={"ok": False, "error": serr, "proposal_id": proposal_id})
            return _safe_error(
                "SLOT_UNAVAILABLE",
                "That slot is no longer available. Search again for the next available slots.",
                proposal_id=proposal_id,
            )

    body = {
        "slot_ids": slot_ids,
        "number_of_samples": payload.get("number_of_samples") or payload.get("sample_count") or 1,
        "input_values": payload.get("input_values") or {"A": str(payload.get("sample_count") or 1)},
    }
    status_code, data = domain_bridge.call_book_equipment(user=user, equipment_id=eid, body=body)
    ok = 200 <= status_code < 300
    result = {
        "ok": ok,
        "action": "CREATE_BOOKING",
        "status_code": status_code,
        "proposal_id": proposal_id,
        "idempotency_key": key,
        "data": data if ok else None,
        "error": None if ok else (data.get("code") or data.get("error") or "BOOKING_FAILED"),
        "message": _friendly_book_message(ok, data),
    }
    if ok:
        prop_store.invalidate_proposal(proposal_id)
        idem.store_result(user=user, idempotency_key=key, result=result)
    _audit(
        user=user,
        action="execute_booking_create",
        detail={
            "ok": ok,
            "proposal_id": proposal_id,
            "idempotency_key": key,
            "equipment_id": eid,
            "booking_id": (data or {}).get("real_booking_id") or (data or {}).get("booking_id"),
            "error": result.get("error"),
        },
        message="create_booking",
    )
    return result


def prepare_cancellation(*, user, booking_id: int | None = None, text: str = "") -> dict[str, Any]:
    if user is None or not getattr(user, "is_authenticated", False):
        return _safe_error("AUTH_REQUIRED", "Sign in to cancel a booking.")

    bid = booking_id
    if not bid:
        m = re.search(r"\b(\d{6,})\b", text or "")
        if m:
            bid = int(m.group(1))
    if not bid and ("next" in (text or "").lower()):
        from iic_booking.research_copilot.services import tools as tools_svc

        nb = tools_svc._get_next_booking(arguments={}, user=user)
        data = (nb or {}).get("data") or {}
        bid = data.get("booking_id")

    if not bid:
        return _safe_error("BOOKING_REQUIRED", "Specify which booking to cancel (or say “cancel my next booking”).")

    booking, err = _booking_owned(user=user, booking_id=int(bid))
    if err:
        return _safe_error("BOOKING_FORBIDDEN", "Booking not found for your account.")

    slots = list(booking.daily_slots.all())
    start = slots[0].start_datetime if slots else None
    end = slots[-1].end_datetime if slots else None
    payload = {
        "booking_id": int(booking.booking_id),
        "equipment_id": int(booking.equipment_id) if booking.equipment_id else None,
        "equipment_name": getattr(booking.equipment, "name", None),
        "status": booking.status,
        "start_time": start.isoformat() if start else None,
        "end_time": end.isoformat() if end else None,
        "refund": True,
        "notes": "Cancelled via Research Copilot",
        "cancellation_policy_note": (
            "Cancellation follows the portal cancellation policy and refund rules for this booking."
        ),
    }
    record = prop_store.create_proposal(user=user, action="CANCEL_BOOKING", payload=payload)
    executable = _flag("COPILOT_BOOKING_CANCEL", user=user)
    _audit(user=user, action="prepare_cancellation", detail={"proposal_id": record["proposal_id"], "booking_id": bid, "ok": True})
    return {
        "ok": True,
        "action": "CANCEL_BOOKING",
        "status": "READY_FOR_CONFIRMATION",
        "proposal_id": record["proposal_id"],
        "confirmation_token": record["confirmation_token"],
        "confirmation_required": True,
        "requires_confirmation": True,
        "executable": executable,
        "expires_at": record["expires_at"],
        **payload,
        "message": "Review cancellation details. Confirm only if you want to cancel this booking.",
        "portal_href": f"/my-bookings?booking={booking.booking_id}&action=cancel",
    }


def execute_booking_cancel(
    *,
    user,
    proposal_id: str,
    confirmation_token: str,
    idempotency_key: str = "",
) -> dict[str, Any]:
    if not _flag("COPILOT_BOOKING_CANCEL", user=user):
        return _safe_error(
            "COPILOT_BOOKING_CANCEL_DISABLED",
            "Booking cancel via Copilot is disabled. Use My Bookings to cancel, or enable COPILOT_BOOKING_CANCEL after E2E.",
            proposal_id=proposal_id,
        )

    key = idempotency_key or idem.make_idempotency_key(user=user, action="CANCEL_BOOKING", proposal_id=proposal_id)
    cached = idem.get_cached_result(user=user, idempotency_key=key)
    if cached:
        return {**cached, "idempotent_replay": True}

    prop, err = prop_store.validate_proposal_for_user(
        user=user, proposal_id=proposal_id, confirmation_token=confirmation_token, expected_action="CANCEL_BOOKING"
    )
    if err:
        return _safe_error(err, _human_proposal_error(err))

    payload = prop.get("payload") or {}
    booking_id = int(payload["booking_id"])
    booking, berr = _booking_owned(user=user, booking_id=booking_id)
    if berr:
        return _safe_error("BOOKING_FORBIDDEN", "Booking not found for your account.")

    status_code, data = domain_bridge.call_user_cancel_booking(
        user=user,
        booking_id=booking_id,
        body={"refund": bool(payload.get("refund", True)), "notes": payload.get("notes") or "Cancelled via Research Copilot"},
    )
    ok = 200 <= status_code < 300
    result = {
        "ok": ok,
        "action": "CANCEL_BOOKING",
        "status_code": status_code,
        "proposal_id": proposal_id,
        "idempotency_key": key,
        "data": data if ok else None,
        "error": None if ok else (data.get("error") or "CANCEL_FAILED"),
        "message": "Booking cancelled." if ok else _user_facing_domain_error(data),
        "booking_id": booking_id,
    }
    if ok:
        prop_store.invalidate_proposal(proposal_id)
        idem.store_result(user=user, idempotency_key=key, result=result)
    _audit(
        user=user,
        action="execute_booking_cancel",
        detail={"ok": ok, "proposal_id": proposal_id, "idempotency_key": key, "booking_id": booking_id, "error": result.get("error")},
    )
    return result


def prepare_reschedule(
    *,
    user,
    booking_id: int | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    slot_id: int | None = None,
    text: str = "",
) -> dict[str, Any]:
    if user is None or not getattr(user, "is_authenticated", False):
        return _safe_error("AUTH_REQUIRED", "Sign in to reschedule a booking.")

    bid = booking_id
    if not bid and ("next" in (text or "").lower()):
        from iic_booking.research_copilot.services import tools as tools_svc

        nb = tools_svc._get_next_booking(arguments={}, user=user)
        data = (nb or {}).get("data") or {}
        bid = data.get("booking_id")
    if not bid:
        m = re.search(r"\b(\d{6,})\b", text or "")
        if m:
            bid = int(m.group(1))
    if not bid:
        return _safe_error("BOOKING_REQUIRED", "Specify which booking to reschedule.")

    booking, err = _booking_owned(user=user, booking_id=int(bid))
    if err:
        return _safe_error("BOOKING_FORBIDDEN", "Booking not found for your account.")

    start = start_time
    end = end_time
    if slot_id and (not start or not end):
        slot, serr = _load_slot(slot_id=int(slot_id), equipment_id=int(booking.equipment_id))
        if serr:
            # For reschedule target, slot must be AVAILABLE
            return _safe_error(serr, "Target slot is not available.")
        start = slot.start_datetime.isoformat() if slot.start_datetime else None
        end = slot.end_datetime.isoformat() if slot.end_datetime else None

    if not start or not end:
        return {
            "ok": True,
            "action": "RESCHEDULE_BOOKING",
            "status": "NEEDS_SLOT",
            "booking_id": int(booking.booking_id),
            "equipment_name": getattr(booking.equipment, "name", None),
            "requires_confirmation": False,
            "message": "Choose a new available slot to reschedule this booking.",
            "portal_href": f"/my-bookings?booking={booking.booking_id}",
        }

    payload = {
        "booking_id": int(booking.booking_id),
        "equipment_id": int(booking.equipment_id) if booking.equipment_id else None,
        "equipment_name": getattr(booking.equipment, "name", None),
        "start_time": start,
        "end_time": end,
        "slot_id": int(slot_id) if slot_id else None,
    }
    record = prop_store.create_proposal(user=user, action="RESCHEDULE_BOOKING", payload=payload)
    executable = _flag("COPILOT_BOOKING_RESCHEDULE", user=user)
    _audit(user=user, action="prepare_reschedule", detail={"proposal_id": record["proposal_id"], "booking_id": bid, "ok": True})
    return {
        "ok": True,
        "action": "RESCHEDULE_BOOKING",
        "status": "READY_FOR_CONFIRMATION",
        "proposal_id": record["proposal_id"],
        "confirmation_token": record["confirmation_token"],
        "confirmation_required": True,
        "requires_confirmation": True,
        "executable": executable,
        "expires_at": record["expires_at"],
        **payload,
        "message": "Confirm to reschedule using the portal reschedule rules.",
        "portal_href": f"/my-bookings?booking={booking.booking_id}",
    }


def execute_booking_reschedule(
    *,
    user,
    proposal_id: str,
    confirmation_token: str,
    idempotency_key: str = "",
) -> dict[str, Any]:
    if not _flag("COPILOT_BOOKING_RESCHEDULE", user=user):
        return _safe_error(
            "COPILOT_BOOKING_RESCHEDULE_DISABLED",
            "Reschedule via Copilot is disabled. Use My Bookings, or enable COPILOT_BOOKING_RESCHEDULE after E2E.",
            proposal_id=proposal_id,
        )

    key = idempotency_key or idem.make_idempotency_key(user=user, action="RESCHEDULE_BOOKING", proposal_id=proposal_id)
    cached = idem.get_cached_result(user=user, idempotency_key=key)
    if cached:
        return {**cached, "idempotent_replay": True}

    prop, err = prop_store.validate_proposal_for_user(
        user=user, proposal_id=proposal_id, confirmation_token=confirmation_token, expected_action="RESCHEDULE_BOOKING"
    )
    if err:
        return _safe_error(err, _human_proposal_error(err))

    payload = prop.get("payload") or {}
    booking_id = int(payload["booking_id"])
    booking, berr = _booking_owned(user=user, booking_id=booking_id)
    if berr:
        return _safe_error("BOOKING_FORBIDDEN", "Booking not found for your account.")

    start = payload.get("start_time")
    end = payload.get("end_time")
    if not start or not end:
        return _safe_error("INVALID_SLOT", "Reschedule proposal is missing start/end time.")

    status_code, data = domain_bridge.call_user_reschedule_booking(
        user=user, booking_id=booking_id, start_time=str(start), end_time=str(end)
    )
    ok = 200 <= status_code < 300
    result = {
        "ok": ok,
        "action": "RESCHEDULE_BOOKING",
        "status_code": status_code,
        "proposal_id": proposal_id,
        "idempotency_key": key,
        "data": data if ok else None,
        "error": None if ok else (data.get("error") or "RESCHEDULE_FAILED"),
        "message": "Booking rescheduled." if ok else _user_facing_domain_error(data),
        "booking_id": booking_id,
    }
    if ok:
        prop_store.invalidate_proposal(proposal_id)
        idem.store_result(user=user, idempotency_key=key, result=result)
    _audit(
        user=user,
        action="execute_booking_reschedule",
        detail={"ok": ok, "proposal_id": proposal_id, "idempotency_key": key, "booking_id": booking_id, "error": result.get("error")},
    )
    return result


def _human_proposal_error(code: str) -> str:
    return {
        "PROPOSAL_NOT_FOUND": "This booking proposal was not found. Prepare the action again.",
        "PROPOSAL_FORBIDDEN": "You cannot confirm this proposal.",
        "CONFIRMATION_INVALID": "Confirmation token is invalid. Prepare the action again.",
        "CONFIRMATION_REQUIRED": "Explicit confirmation is required.",
        "PROPOSAL_ACTION_MISMATCH": "This confirmation does not match the prepared action.",
        "PROPOSAL_EXPIRED": "This proposal expired. Prepare the booking again.",
    }.get(code, "Confirmation failed.")


def _user_facing_domain_error(data: dict) -> str:
    msg = (data or {}).get("error") or (data or {}).get("message") or "The portal could not complete that action."
    # Strip overly technical prefixes
    return str(msg)[:500]


def _friendly_book_message(ok: bool, data: dict) -> str:
    if not ok:
        return _user_facing_domain_error(data)
    bid = (data or {}).get("real_booking_id") or (data or {}).get("booking_id") or (data or {}).get("id")
    return f"Booking confirmed. Booking ID: {bid}" if bid else "Booking confirmed."


# Back-compat aliases used by earlier scaffold
def prepare_booking_create_legacy(*, user, payload: dict[str, Any]) -> dict[str, Any]:
    return prepare_booking_create(
        user=user,
        equipment_id=payload.get("equipment_id"),
        slot_id=payload.get("slot_id"),
        slot_ids=payload.get("slot_ids"),
        sample_count=int(payload.get("sample_count") or 1),
        text=payload.get("text") or "",
        context=payload.get("context"),
    )

"""Call lifecycle. Every change goes through ``_transition`` under a row lock, so simultaneous accept / decline / end
requests cannot corrupt a call: the first wins, the rest see the result (idempotent when they asked for the same).

ringing --accept--> accepted --end--> ended
ringing --decline--> declined | --cancel (caller)--> cancelled | --ring timeout--> missed
ringing/accepted --fail--> failed;   a callee already in a call -> busy at once.

Participants are told over the calls WebSocket group of each user (``calls.user.<id>``); the callee's apps are also
woken by push. No audio passes through the server and none is recorded.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from asgiref.sync import async_to_sync
from django.core import signing
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from . import conf
from .models import ACTIVE_STATUSES, CallContext, CallEvent, CallRole, CallSession, CallStatus
from .recipients import CallNotAllowed, calls_open_to, reachable, resolve_target, silent_block

logger = logging.getLogger(__name__)

ACTION_SALT = "iic_booking.calls.action"
MAX_CALL_HOURS = 4
CALLS_PER_10_MIN = 6
SAME_CALLEE_PER_10_MIN = 3
# Lab staff may need to call many booking users in a row: only a guard against runaway clients. Counted apart from their
# other calls; the limit per person still applies, so nobody is called over and over.
STAFF_CALLS_TO_USERS_PER_10_MIN = 60
PLATFORMS = {"web", "android", "ios"}


def group_for(user_id: int) -> str:
    return f"calls.user.{user_id}"


def _send(user_id: int, message: dict) -> None:
    try:
        from channels.layers import get_channel_layer

        layer = get_channel_layer()
        if layer is not None:
            async_to_sync(layer.group_send)(group_for(user_id), {"type": "call.message", "message": message})
    except Exception:  # noqa: BLE001 - signalling trouble must never break the request
        logger.warning("Calls: could not reach the WebSocket layer", exc_info=True)


def person_card(user) -> dict:
    """Who is calling / being called, for the call screens: name, photo, role and department."""
    from iic_booking.users.display import get_user_display_name
    from iic_booking.users.models.user_type import UserType

    photo = ""
    if getattr(user, "profile_picture", None) and getattr(user.profile_picture, "name", None):
        from iic_booking.equipment.equipment_qr import public_base_url
        from iic_booking.users.media_utils import profile_picture_proxy_path

        photo = public_base_url().rstrip("/") + profile_picture_proxy_path(user.pk)
    dept = getattr(user, "department", None)
    return {
        "id": user.pk,
        "name": get_user_display_name(user),
        "photo_url": photo,
        "user_type_label": str(dict(UserType.get_choices()).get(user.user_type, "")),
        "designation": (getattr(user, "designation", "") or "").strip()[:80],
        "department": (getattr(dept, "name", "") or "").strip(),
    }


def context_card(session: CallSession) -> dict:
    """The instrument the call is about (directly, or through the booking / invoice), and its department."""
    from iic_booking.equipment.models import Booking, Equipment

    equipment, booking_ref = None, ""
    if session.context_type == CallContext.EQUIPMENT:
        equipment = Equipment.objects.select_related("internal_department").filter(pk=session.context_id).first()
    else:
        booking = (Booking.objects.select_related("equipment", "equipment__internal_department")
                   .filter(pk=session.context_id).first())
        if booking is not None:
            equipment = booking.equipment
            booking_ref = booking.virtual_booking_id or f"{booking.equipment.code}-{booking.pk}"
    if equipment is None:
        return {"equipment_id": None, "equipment_name": "", "equipment_department": "", "booking_ref": booking_ref}
    dept = getattr(equipment, "internal_department", None)
    return {
        "equipment_id": equipment.pk, "equipment_name": equipment.name,
        "equipment_department": (getattr(dept, "name", "") or "").strip(), "booking_ref": booking_ref,
    }


def _role_label(session: CallSession, incoming: bool) -> str:
    """"For the Lab Operator" tells staff which role was called; a user called by staff is simply being called."""
    return "" if incoming and session.role == CallRole.USER else CallRole(session.role).label


def serialize(session: CallSession, viewer, *, with_context: bool = True) -> dict:
    """``with_context`` adds the instrument card (one query); the history list leaves it out."""
    incoming = session.callee_id == viewer.pk
    other = session.caller if incoming else session.callee
    return {
        "id": str(session.id),
        "status": session.status,
        "status_display": session.get_status_display(),
        "direction": "incoming" if incoming else "outgoing",
        "role": session.role,
        "role_label": _role_label(session, incoming),
        "other": person_card(other),
        "context": context_card(session) if with_context else None,
        "context_type": session.context_type,
        "context_id": session.context_id,
        "context_label": session.context_label,
        "reason": session.reason,
        "created_at": session.created_at.isoformat(),
        "answered_at": session.answered_at.isoformat() if session.answered_at else None,
        "ended_at": session.ended_at.isoformat() if session.ended_at else None,
        "duration_seconds": session.duration_seconds,
        "failure_code": session.failure_code,
        "ring_seconds": conf.ring_seconds(),
    }


def _notify(session: CallSession, kind: str) -> None:
    for user in (session.caller, session.callee):
        _send(user.pk, {"type": kind, "call": serialize(session, user)})


def action_token(session: CallSession) -> str:
    """Short-lived, call- and callee-bound token letting the native incoming-call screen decline without a session."""
    return signing.dumps({"c": str(session.id), "u": session.callee_id}, salt=ACTION_SALT)


def _rate_limited(caller, callee, role: str) -> bool:
    def bump(key, limit):
        if cache.add(key, 1, timeout=600):
            return False
        try:
            return cache.incr(key) > limit
        except ValueError:
            cache.set(key, 1, timeout=600)
            return False

    overall = (bump(f"calls:rate:{caller.pk}:users", STAFF_CALLS_TO_USERS_PER_10_MIN) if role == CallRole.USER
               else bump(f"calls:rate:{caller.pk}", CALLS_PER_10_MIN))
    return overall or bump(f"calls:rate:{caller.pk}:{callee.pk}", SAME_CALLEE_PER_10_MIN)


def expire_stale(queryset=None) -> int:
    """Ringing past the ring time -> missed; calls left open far too long -> ended (server restarts, lost clients)."""
    now = timezone.now()
    qs = queryset if queryset is not None else CallSession.objects.all()
    changed = 0
    for session in qs.filter(status=CallStatus.RINGING, created_at__lt=now - timedelta(seconds=conf.ring_seconds() + 5)):
        changed += bool(_transition(session.pk, None, CallStatus.MISSED, allowed_from=(CallStatus.RINGING,), event="timeout"))
    for session in qs.filter(status=CallStatus.ACCEPTED, answered_at__lt=now - timedelta(hours=MAX_CALL_HOURS)):
        changed += bool(_transition(session.pk, None, CallStatus.ENDED, allowed_from=(CallStatus.ACCEPTED,), event="stale"))
    return changed


def start_call(caller, *, context_type: str, context_id: int, role: str, callee_id: int | None = None,
               reason: str = "", platform: str = "web") -> CallSession:
    if not calls_open_to(caller):
        raise CallNotAllowed("unavailable", "Calling is not available for your account yet.")
    if role not in CallRole.values:
        raise CallNotAllowed("invalid", "Choose whom to call.", 400)
    target = resolve_target(caller, context_type, int(context_id))
    if role not in target.roles:
        raise CallNotAllowed("invalid", "This person cannot be called about this item.", 400)
    block = silent_block(target)
    if block is not None:
        raise CallNotAllowed("silent_hours", block["message"])
    candidates = target.candidates.get(role, [])
    if callee_id is not None:
        callee = next((p for p in candidates if p.pk == int(callee_id)), None)
        if callee is None:  # never trust a client-chosen person outside the resolved candidates
            raise CallNotAllowed("invalid", "This person cannot be called about this item.", 400)
    else:
        callee = next((p for p in candidates if reachable(p)), candidates[0] if candidates else None)
    if callee is None:
        raise CallNotAllowed("no_recipient", f"No {CallRole(role).label} is set up for this item. Raise a support ticket instead.", 404)
    if _rate_limited(caller, callee, role):
        raise CallNotAllowed("rate_limited", "Too many calls in a short time. Please wait a few minutes.", 429)
    expire_stale(CallSession.objects.filter(callee=callee))
    expire_stale(CallSession.objects.filter(caller=caller))
    with transaction.atomic():
        busy = CallSession.objects.select_for_update().filter(status__in=ACTIVE_STATUSES).filter(
            callee=callee).exists() or CallSession.objects.filter(status=CallStatus.ACCEPTED, caller=callee).exists()
        session = CallSession.objects.create(
            caller=caller, callee=callee, role=role, context_type=target.context_type, context_id=target.context_id,
            context_label=target.label[:200], reason=(reason or "")[:200],
            caller_platform=platform if platform in PLATFORMS else "web",
            status=CallStatus.BUSY if busy else CallStatus.RINGING, ended_at=timezone.now() if busy else None,
        )
        CallEvent.objects.create(session=session, kind="busy" if busy else "ringing", actor=caller)
    if session.status == CallStatus.RINGING:
        transaction.on_commit(lambda: _ring(session))
    return session


def _ring(session: CallSession) -> None:
    from iic_booking.users.display import get_user_display_name

    from .push import ring_devices

    _send(session.callee_id, {"type": "call.incoming", "call": serialize(session, session.callee)})
    _send(session.caller_id, {"type": "call.update", "call": serialize(session, session.caller)})
    try:
        caller, ctx = person_card(session.caller), context_card(session)
        ring_devices(session, {
            "type": "incoming_call", "call_id": str(session.id), "caller_name": get_user_display_name(session.caller),
            "role_label": _role_label(session, True), "context_label": session.context_label,
            "action_token": action_token(session), "ring_seconds": str(conf.ring_seconds()),
            "caller_photo_url": caller["photo_url"], "caller_department": caller["department"],
            "caller_type": caller["user_type_label"], "equipment_name": ctx["equipment_name"],
            "equipment_department": ctx["equipment_department"], "booking_ref": ctx["booking_ref"],
        })
    except Exception:  # noqa: BLE001
        logger.warning("Calls: push to the callee's apps failed", exc_info=True)


def _transition(call_id, actor, to: str, *, allowed_from, event: str, detail: str = "", platform: str = "",
                check=None) -> CallSession | None:
    with transaction.atomic():
        session = CallSession.objects.select_for_update().select_related("caller", "callee").filter(pk=call_id).first()
        if session is None:
            return None
        if check is not None:
            check(session)
        if session.status == to:
            return session  # repeated request: same result
        if session.status not in allowed_from:
            return session  # already decided by someone else
        now = timezone.now()
        was_ringing = session.status == CallStatus.RINGING
        session.status = to
        fields = ["status"]
        if to == CallStatus.ACCEPTED:
            session.answered_at = now
            session.callee_platform = platform if platform in PLATFORMS else session.callee_platform
            fields += ["answered_at", "callee_platform"]
        else:
            session.ended_at = now
            fields.append("ended_at")
        if to == CallStatus.FAILED:
            session.failure_code = (detail or "failed")[:50]
            fields.append("failure_code")
        session.save(update_fields=fields)
        CallEvent.objects.create(session=session, kind=event, actor=actor, detail=detail[:200])
    transaction.on_commit(lambda: _notify(session, "call.update"))
    if was_ringing:
        transaction.on_commit(lambda: _stop_ringing(session))
    return session


def _stop_ringing(session: CallSession) -> None:
    from .push import stop_ringing_devices

    try:
        stop_ringing_devices(session)
    except Exception:  # noqa: BLE001
        logger.warning("Calls: telling the callee's apps to stop ringing failed", exc_info=True)


def _participant(user, *, callee_only=False, caller_only=False):
    def check(session):
        allowed = {session.callee_id} if callee_only else {session.caller_id} if caller_only else {session.caller_id, session.callee_id}
        if user.pk not in allowed:
            raise CallNotAllowed("not_found", "Call not found.", 404)
    return check


def accept(user, call_id, platform: str = "web"):
    expire_stale(CallSession.objects.filter(pk=call_id))
    return _transition(call_id, user, CallStatus.ACCEPTED, allowed_from=(CallStatus.RINGING,), event="accepted",
                       platform=platform, check=_participant(user, callee_only=True))


def decline(user, call_id):
    return _transition(call_id, user, CallStatus.DECLINED, allowed_from=(CallStatus.RINGING,), event="declined",
                       check=_participant(user, callee_only=True))


def cancel(user, call_id, *, timeout: bool = False):
    to = CallStatus.MISSED if timeout else CallStatus.CANCELLED
    return _transition(call_id, user, to, allowed_from=(CallStatus.RINGING,), event="timeout" if timeout else "cancelled",
                       check=_participant(user, caller_only=True))


def end(user, call_id):
    session = CallSession.objects.filter(pk=call_id).first()
    if session is not None and session.status == CallStatus.RINGING and session.caller_id == user.pk:
        return cancel(user, call_id)
    return _transition(call_id, user, CallStatus.ENDED, allowed_from=(CallStatus.ACCEPTED,), event="ended",
                       check=_participant(user))


def fail(user, call_id, code: str):
    safe = "".join(ch for ch in (code or "failed") if ch.isalnum() or ch in "_-")[:50] or "failed"
    return _transition(call_id, user, CallStatus.FAILED, allowed_from=ACTIVE_STATUSES, event="failed", detail=safe,
                       check=_participant(user))


def decline_with_token(call_id, token: str):
    """Native incoming-call screen: decline with the action token from the push (no portal session)."""
    from django.contrib.auth import get_user_model

    try:
        data = signing.loads(token or "", salt=ACTION_SALT, max_age=conf.ring_seconds() + 60)
    except signing.BadSignature:
        raise CallNotAllowed("expired", "This call has ended.", 400)
    if data.get("c") != str(call_id):
        raise CallNotAllowed("expired", "This call has ended.", 400)
    user = get_user_model().objects.get(pk=data["u"])
    return decline(user, call_id)

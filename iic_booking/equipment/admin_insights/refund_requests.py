"""Refund requests: users giving up their own bookings for a refund before the slot (Cancellations page).

Every way a user asks for their money back, one row each:

- Cancelled by the user: the user (or their supervisor) cancelled the whole booking themselves — the cancellation
  log row with "Cancelled by" User / Supervisor and reason "User request". Refunded at once when they chose a refund.
- Request for admin approval: a cancellation request waiting for / decided by the administrator (pending, approved,
  rejected, withdrawn).
- Partial: the user released some slots of a booking; the refund for those slots is in the booking history.

Only requests made before the first booked slot started count. Window: the equipment's cancel / reschedule hours
(default 48 h); a request at least that long before the slot is within the stipulated window. The period filters
on when the request was made. Test data is excluded; a Department Administrator sees their department's
equipment, the Main Administrator optionally one department (``?dept=``). The wallet transaction is the refund credit naming the booking, posted after the request.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db.models import DateTimeField, Min, OuterRef, Q, Subquery
from django.utils import timezone

from iic_booking.equipment.admin_dashboard_summary import _Scope

from .common import bounds, iso, money, multi, page_meta, page_params, period, scope_for, scope_payload, share
from .users import CATEGORIES, category_case

SOURCES = {
    "self_service": "Cancelled by the user",
    "request": "Request for admin approval",
    "partial": "Partial (some slots)",
}
STATUSES = {
    "refunded": "Refunded",
    "no_refund": "Cancelled without refund",
    "pending": "Pending",
    "approved": "Approved",
    "rejected": "Rejected",
    "withdrawn": "Withdrawn",
}
REQUEST_STATUS = {"PENDING": "pending", "APPROVED": "approved", "REJECTED": "rejected", "CANCELLED": "withdrawn"}
WINDOW_LABELS = {"within": "Within the window", "outside": "Inside the cut-off", "unknown": "Slot time not known"}
DEFAULT_WINDOW_HOURS = 48
PARTIAL_PREFIX = "Partial cancellation by user"
FULL_REFUND_PREFIX = "Refund for cancelled Booking "
PARTIAL_REFUND_PREFIX = "Partial refund for cancelled slot(s) on Booking "
REPEAT_MIN = 2
MAX_ROWS = 20000
TOP = 15
SORTS = {"requested_at", "lead", "refund", "slot"}
_AMOUNT = re.compile(r"₹\s*([\d,]+(?:\.\d+)?)\s+refunded to wallet")
_SLOT = re.compile(r"\((\d{4}-\d{2}-\d{2} \d{2}:\d{2})")


def _first_slot_start():
    from iic_booking.equipment.models import BookingSlotRange, DailySlot

    held = (
        DailySlot.objects.filter(booking_id=OuterRef("booking_id"))
        .order_by()
        .values("booking_id")
        .annotate(s=Min("start_datetime"))
        .values("s")[:1]
    )
    released = BookingSlotRange.objects.filter(booking_id=OuterRef("booking_id")).values("start_datetime")[:1]
    return Subquery(held, output_field=DateTimeField()), Subquery(released, output_field=DateTimeField())


def _scoped(qs, user, params):
    """``qs`` (rows with a ``booking``) on the user's scope (``?dept=``) with the page's booking filters."""
    from .cancellations import _apply_filters

    bookings = scope_for(user, params).bookings()
    case, code = category_case("booking__user__")
    qs = (
        qs.filter(booking_id__in=bookings.order_by().values("pk"))
        .order_by()
        .annotate(user_type_code=code)
        .annotate(category=case)
    )
    qs = _apply_filters(qs, params)
    users = [int(u) for u in multi(params, "user") if u.isdigit()]
    if users:
        qs = qs.filter(booking__user_id__in=users)
    search = str(params.get("search") or "").strip()
    if search:
        q = (
            Q(booking__virtual_booking_id__icontains=search)
            | Q(booking__user__name__icontains=search)
            | Q(booking__user__email__icontains=search)
            | Q(booking__equipment__name__icontains=search)
            | Q(booking__equipment__code__icontains=search)
        )
        if search.isdigit():
            q |= Q(booking_id=int(search))
        qs = qs.filter(q)
    return qs.select_related("booking", "booking__user", "booking__user__department", "booking__equipment")


def _base(source: str, key: str, item, booking, requested_at, slot_start, status: str, refund, note: str, resolved_at):
    return {
        "key": key,
        "source": source,
        "booking": booking,
        "category": item.category,
        "requested_at": requested_at,
        "slot_start": slot_start,
        "status": status,
        "refund": refund,
        "note": (note or "").strip()[:500],
        "resolved_at": resolved_at,
    }


def _self_service(user, params, start_at, end_at) -> list[dict]:
    from iic_booking.equipment.models import BookingCancellation, CancellationActorRole, CancellationReason

    qs = BookingCancellation.objects.filter(
        actor_role__in=[CancellationActorRole.USER, CancellationActorRole.SUPERVISOR],
        reason=CancellationReason.USER_REQUEST,
        cancelled_at__gte=start_at,
        cancelled_at__lt=end_at,
    )
    rows = []
    for c in _scoped(qs, user, params)[:MAX_ROWS]:
        refunded = c.refund_amount is not None and c.refund_amount > 0
        rows.append(
            _base(
                "self_service", f"c{c.pk}", c, c.booking, c.cancelled_at, c.slot_start,
                "refunded" if refunded else "no_refund", c.refund_amount, c.note, c.cancelled_at,
            )
        )
    return rows


def _requests(user, params, start_at, end_at) -> list[dict]:
    from iic_booking.equipment.models import BookingCancellation, BookingCancellationRequest

    held, released = _first_slot_start()
    logged = BookingCancellation.objects.filter(booking_id=OuterRef("booking_id"))
    qs = BookingCancellationRequest.objects.filter(requested_at__gte=start_at, requested_at__lt=end_at).annotate(
        held_start=held,
        released_start=released,
        logged_start=Subquery(logged.values("slot_start")[:1]),
        logged_refund=Subquery(logged.values("refund_amount")[:1]),
    )
    rows = []
    for r in _scoped(qs, user, params)[:MAX_ROWS]:
        status = REQUEST_STATUS.get(r.status, "pending")
        rows.append(
            _base(
                "request", f"r{r.pk}", r, r.booking, r.requested_at,
                r.held_start or r.logged_start or r.released_start, status,
                r.logged_refund if status == "approved" else None, r.notes or "", r.responded_at,
            )
        )
    return rows


def _partial_slot(comment: str):
    m = _SLOT.search(comment or "")
    if not m:
        return None
    try:
        return timezone.make_aware(datetime.strptime(m.group(1), "%Y-%m-%d %H:%M"))
    except ValueError:
        return None


def _partial_amount(comment: str) -> Decimal | None:
    m = _AMOUNT.search(comment or "")
    if not m:
        return None
    try:
        return Decimal(m.group(1).replace(",", ""))
    except InvalidOperation:
        return None


def _partials(user, params, start_at, end_at) -> list[dict]:
    from iic_booking.equipment.models import BookingEvent

    held, released = _first_slot_start()
    qs = BookingEvent.objects.filter(
        comment__startswith=PARTIAL_PREFIX, created_at__gte=start_at, created_at__lt=end_at
    ).annotate(held_start=held, released_start=released)
    rows = []
    for e in _scoped(qs, user, params)[:MAX_ROWS]:
        amount = _partial_amount(e.comment)
        rows.append(
            _base(
                "partial", f"p{e.pk}", e, e.booking, e.created_at,
                _partial_slot(e.comment) or e.held_start or e.released_start,
                "refunded" if amount else "no_refund", amount, "", e.created_at,
            )
        )
    return rows


def _complete(row: dict) -> dict:
    equipment = row["booking"].equipment
    hours = int(getattr(equipment, "reschedule_hours_threshold", None) or DEFAULT_WINDOW_HOURS)
    lead = (
        int((row["slot_start"] - row["requested_at"]).total_seconds() // 60)
        if row["slot_start"] and row["requested_at"]
        else None
    )
    row["lead_minutes"] = lead
    row["window_hours"] = hours
    row["window"] = "unknown" if lead is None else ("within" if lead >= hours * 60 else "outside")
    return row


def collect(user, params, start, end) -> list[dict]:
    """Refund requests made between the local dates ``start`` and ``end`` (inclusive), before the slot started."""
    start_at, end_at = bounds(start, end)
    sources = [s for s in multi(params, "source") if s in SOURCES] or list(SOURCES)
    gather = {"self_service": _self_service, "request": _requests, "partial": _partials}
    rows = []
    for source in sources:
        rows += [_complete(r) for r in gather[source](user, params, start_at, end_at)]
    rows = [r for r in rows if r["lead_minutes"] is None or r["lead_minutes"] > 0]
    statuses = [s for s in multi(params, "status") if s in STATUSES]
    if statuses:
        rows = [r for r in rows if r["status"] in statuses]
    windows = [w for w in multi(params, "window") if w in WINDOW_LABELS]
    if windows:
        rows = [r for r in rows if r["window"] in windows]
    return rows


def _user_info(u, category: str) -> dict[str, Any]:
    from iic_booking.users.display import get_user_display_name

    return {
        "id": u.pk,
        "name": get_user_display_name(u),
        "email": u.email or "",
        "category": category,
        "category_display": CATEGORIES.get(category, "Other"),
        "department": u.department.name if u.department_id else "",
    }


def _counts(rows: list[dict], key, labels: dict) -> list[dict]:
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        counts[key(r)] += 1
    return [{"key": k, "label": label, "count": counts.get(k, 0)} for k, label in labels.items()]


def _repeaters(rows: list[dict]) -> list[dict]:
    by_user: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        by_user[r["booking"].user_id].append(r)
    out = []
    for items in by_user.values():
        if len(items) < REPEAT_MIN:
            continue
        first = items[0]
        out.append(
            {
                "user": _user_info(first["booking"].user, first["category"]),
                "count": len(items),
                "within_window": sum(1 for r in items if r["window"] == "within"),
                "bookings": len({r["booking"].pk for r in items}),
                "refund_total": money(sum((Decimal(r["refund"] or 0) for r in items), Decimal("0"))),
                "last_requested_at": iso(max(r["requested_at"] for r in items)),
            }
        )
    out.sort(key=lambda x: x["last_requested_at"] or "", reverse=True)
    out.sort(key=lambda x: x["count"], reverse=True)
    return out


def summarize(rows: list[dict], *, bookings_created: int) -> dict[str, Any]:
    within = [r for r in rows if r["window"] == "within"]
    refund = sum((Decimal(r["refund"] or 0) for r in rows), Decimal("0"))
    by_equipment: dict[int, dict] = {}
    for r in rows:
        eq = r["booking"].equipment
        item = by_equipment.setdefault(eq.pk, {"id": eq.pk, "label": eq.name, "code": eq.code, "count": 0})
        item["count"] += 1
    repeaters = _repeaters(rows)
    return {
        "total": len(rows),
        "bookings_created": bookings_created,
        "rate": share(len(rows), bookings_created),
        "within_window": len(within),
        "unique_users": len({r["booking"].user_id for r in rows}),
        "unique_users_within_window": len({r["booking"].user_id for r in within}),
        "repeat_refunders": len(repeaters),
        "refund_total": money(refund),
        "by_source": _counts(rows, lambda r: r["source"], SOURCES),
        "by_status": _counts(rows, lambda r: r["status"], STATUSES),
        "by_window": _counts(rows, lambda r: r["window"], WINDOW_LABELS),
        "by_equipment": sorted(by_equipment.values(), key=lambda x: (-x["count"], x["label"] or ""))[:TOP],
        "repeaters": repeaters,
    }


def _refund_reference(description: str) -> tuple[str, str] | None:
    for kind, prefix in (("full", FULL_REFUND_PREFIX), ("partial", PARTIAL_REFUND_PREFIX)):
        if description.startswith(prefix):
            return kind, description[len(prefix) :].split("- ", 1)[0].strip()
    return None


def attach_wallet_transactions(rows: list[dict], *, with_owner: bool) -> dict[str, dict]:
    """Row key -> the refund credit for it (closest credit naming the booking, posted after the request)."""
    from iic_booking.communication.utils import booking_display_id_for_email
    from iic_booking.users.models.wallet import SubWalletTransaction

    if not rows:
        return {}
    since = min(r["requested_at"] for r in rows) - timedelta(minutes=5)
    txns = SubWalletTransaction.objects.filter(
        transaction_type=SubWalletTransaction.TransactionType.CREDIT,
        related_user_id__in={r["booking"].user_id for r in rows},
        created_at__gte=since,
    ).filter(Q(description__startswith=FULL_REFUND_PREFIX) | Q(description__startswith=PARTIAL_REFUND_PREFIX))
    candidates: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in txns.values("id", "amount", "created_at", "description", "sub_wallet__wallet__user_id"):
        ref = _refund_reference(t["description"] or "")
        if ref:
            candidates[ref].append(t)
    used: set[int] = set()
    out = {}
    for r in sorted(rows, key=lambda x: x["requested_at"]):
        kind = "partial" if r["source"] == "partial" else "full"
        anchor = r["resolved_at"] or r["requested_at"]
        options = [
            t
            for t in candidates.get((kind, booking_display_id_for_email(r["booking"])), [])
            if t["id"] not in used and t["created_at"] >= r["requested_at"] - timedelta(minutes=5)
        ]
        if not options or r["status"] in ("pending", "rejected", "withdrawn", "no_refund"):
            continue
        best = min(options, key=lambda t: abs((t["created_at"] - anchor).total_seconds()))
        used.add(best["id"])
        out[r["key"]] = {
            "id": best["id"],
            "amount": money(best["amount"]),
            "created_at": iso(best["created_at"]),
            "description": best["description"],
            "wallet_owner_id": best["sub_wallet__wallet__user_id"] if with_owner else None,
        }
    return out


def serialize(r: dict, txn: dict | None) -> dict[str, Any]:
    booking = r["booking"]
    equipment = booking.equipment
    refund = r["refund"] if r["refund"] is not None else (txn or {}).get("amount")
    return {
        "id": r["key"],
        "source": r["source"],
        "source_display": SOURCES[r["source"]],
        "booking": {
            "pk": booking.pk,
            "display_id": booking.virtual_booking_id or str(booking.pk),
            "status": booking.status,
            "status_display": booking.get_status_display(),
        },
        "user": _user_info(booking.user, r["category"]),
        "equipment": {"id": equipment.pk, "name": equipment.name, "code": equipment.code},
        "requested_at": iso(r["requested_at"]),
        "slot_start": iso(r["slot_start"]),
        "lead_minutes": r["lead_minutes"],
        "window_hours": r["window_hours"],
        "within_window": None if r["window"] == "unknown" else r["window"] == "within",
        "status": r["status"],
        "status_display": STATUSES[r["status"]],
        "responded_at": iso(r["resolved_at"]) if r["source"] == "request" else None,
        "refund": money(refund) if refund is not None else None,
        "wallet_transaction": txn,
        "note": r["note"],
    }


def _sort(rows: list[dict], sort: str) -> list[dict]:
    key = sort.lstrip("-") if sort.lstrip("-") in SORTS else "requested_at"
    desc = sort.startswith("-") or sort.lstrip("-") not in SORTS
    getters = {
        "requested_at": lambda r: r["requested_at"],
        "lead": lambda r: r["lead_minutes"],
        "refund": lambda r: r["refund"],
        "slot": lambda r: r["slot_start"],
    }
    known = [r for r in rows if getters[key](r) is not None]
    unknown = [r for r in rows if getters[key](r) is None]
    known.sort(key=lambda r: (getters[key](r), r["requested_at"]), reverse=desc)
    return known + unknown


def build_refund_request_insights(user, params) -> dict[str, Any]:
    from iic_booking.users.admin_wallet_ledger import is_main_admin

    from .cancellations import _bookings_created

    scope = scope_for(user, params)
    now = timezone.now()
    start, end = period(params, now=now)
    rows = collect(user, params, start, end)
    span = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=span - 1)
    previous = collect(user, params, prev_start, prev_end)
    summary = summarize(rows, bookings_created=_bookings_created(scope, params, start, end))
    summary["previous"] = {
        "date_from": prev_start.isoformat(),
        "date_to": prev_end.isoformat(),
        "total": len(previous),
        "unique_users": len({r["booking"].user_id for r in previous}),
    }
    summary["change"] = len(rows) - len(previous)

    ordered = _sort(rows, str(params.get("sort") or "-requested_at").strip())
    offset, size = page_params(params)
    page = ordered[offset : offset + size]
    txns = attach_wallet_transactions(page, with_owner=is_main_admin(user))
    payload = {
        **scope_payload(scope),
        "generated_at": now.isoformat(),
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "default_window_hours": DEFAULT_WINDOW_HOURS,
        "repeat_min": REPEAT_MIN,
        "summary": summary,
        "results": [serialize(r, txns.get(r["key"])) for r in page],
        **page_meta(len(rows), offset, size),
    }
    if params.get("with_options"):
        from .cancellations import _options

        base = _options(scope)
        payload["options"] = {
            "equipment": base["equipment"],
            "categories": base["categories"],
            "departments": base["departments"],
            "sources": [{"value": k, "label": v} for k, v in SOURCES.items()],
            "statuses": [{"value": k, "label": v} for k, v in STATUSES.items()],
            "windows": [{"value": k, "label": v} for k, v in WINDOW_LABELS.items() if k != "unknown"],
        }
    return payload


def refund_request_card(scope: _Scope, start, end) -> dict[str, Any]:
    rows = collect(scope.user, {"dept": scope.department_id if scope.selected_department else None}, start, end)
    users: dict[int, int] = defaultdict(int)
    for r in rows:
        users[r["booking"].user_id] += 1
    return {
        "refund_requests": len(rows),
        "refund_users": len(users),
        "refund_users_within_window": len({r["booking"].user_id for r in rows if r["window"] == "within"}),
        "repeat_refunders": sum(1 for n in users.values() if n >= REPEAT_MIN),
    }

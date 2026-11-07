"""Operations audit for the Main Administrator: waitlist, slot exchange, urgent requests (Type A and B) and refunds, with who did it.

Urgent requests, exchanges and refunds are read from their own records, so their history goes back as far as those records.
Waitlist actions and deleted urgent requests have no lasting row of their own, so they are written to ``OperationsAuditEvent``
when they happen; their history starts with this release. Writing an audit row never stops the action being audited.
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime, time, timedelta

from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .operations_audit_models import OperationsAuditEvent

logger = logging.getLogger(__name__)

Action = OperationsAuditEvent.Action
WAITLIST, EXCHANGE, URGENT, REFUND = "waitlist", "slot_exchange", "urgent", "refund"
CATEGORY_LABELS = {WAITLIST: "Waitlist", EXCHANGE: "Slot exchange", URGENT: "Urgent request", REFUND: "Refund"}
URGENT_TYPE_LABELS = {"NO_SLOT": "Type A", "REVIEWER_URGENT": "Type B"}
SYSTEM_ROLE = "System (automatic)"
DEFAULT_DAYS = 90
PAGE_SIZE_DEFAULT, PAGE_SIZE_MAX = 25, 100
CSV_MAX_ROWS = 20000
CSV_COLUMNS = (("Date and time", "at"), ("Category", "category_label"), ("Action", "action"), ("Done by", "actor_name"), ("Role", "actor_role"),
               ("Equipment", "equipment"), ("User affected", "affected_user"), ("Booking", "booking_ref"), ("Amount", "amount"), ("Details", "detail"))


def _name(user) -> str:
    if user is None:
        return ""
    from iic_booking.users.display import get_user_display_name

    return get_user_display_name(user) or str(getattr(user, "email", "") or "")


def _role(user) -> str:
    from iic_booking.users.models.user_type import UserType

    return {UserType.ADMIN: "Main Administrator", UserType.DEPT_ADMIN: "Department Administrator", UserType.MANAGER: "Officer In Charge",
            UserType.OPERATOR: "Lab Operator", UserType.FINANCE: "Accounts In Charge"}.get(getattr(user, "user_type", None), "User")


def record(action: str, *, equipment=None, subject=None, actor=None, booking=None, detail: str = "") -> None:
    """One audit row. Never raises into the caller, and a failure here cannot break the caller's transaction."""
    try:
        from iic_booking.communication.utils import booking_display_id_for_email

        with transaction.atomic():
            OperationsAuditEvent.objects.create(
                action=action, is_system=actor is None, actor_id=getattr(actor, "pk", None), actor_name=_name(actor)[:255],
                actor_role=SYSTEM_ROLE if actor is None else _role(actor), equipment_id=getattr(equipment, "pk", None),
                equipment_name=str(getattr(equipment, "name", "") or "")[:255], subject_user_id=getattr(subject, "pk", None),
                subject_user_name=_name(subject)[:255], booking_ref=str(booking_display_id_for_email(booking))[:64] if booking is not None else "",
                detail=(detail or "")[:4000],
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Operations audit row %s not stored: %s", action, type(exc).__name__)


def record_waitlist_entries(entries, action: str, *, actor=None, detail: str = "") -> None:
    """One row per entry, for the person whose place is affected. Call before the entries are deleted."""
    for entry in entries:
        record(action, equipment=entry.equipment, subject=entry.user, actor=actor, detail=detail)


def _row(at, category, action, *, actor_name="", actor_role="", equipment="", affected_user="", booking_ref="", amount=None, detail="") -> dict:
    return {"at": at, "category": category, "category_label": CATEGORY_LABELS[category], "action": str(action), "actor_name": actor_name or ("System" if actor_role == SYSTEM_ROLE else ""),
            "actor_role": actor_role, "equipment": equipment or "", "affected_user": affected_user or "", "booking_ref": booking_ref or "",
            "amount": str(amount) if amount is not None else "", "detail": (detail or "").strip()}


def _in_range(field: str, start, end) -> dict:
    return {f"{field}__gte": start, f"{field}__lt": end}


def _logged_rows(start, end) -> list[dict]:
    rows = []
    for e in OperationsAuditEvent.objects.filter(**_in_range("created_at", start, end)):
        category = URGENT if e.action == Action.URGENT_DELETED else WAITLIST
        rows.append(_row(e.created_at, category, e.get_action_display(), actor_name=e.actor_name, actor_role=e.actor_role, equipment=e.equipment_name,
                         affected_user=e.subject_user_name, booking_ref=e.booking_ref, detail=e.detail))
    return rows


def _urgent_rows(start, end) -> list[dict]:
    from .models import UrgentBookingRequest

    rows = []
    query = Q(**_in_range("requested_at", start, end)) | Q(**_in_range("decided_at", start, end)) | Q(**_in_range("wallet_approved_at", start, end))
    for u in UrgentBookingRequest.objects.filter(query).select_related("user", "equipment", "decided_by", "wallet_approved_by"):
        kind = URGENT_TYPE_LABELS.get(u.request_type, u.get_request_type_display())
        common = {"equipment": getattr(u.equipment, "name", ""), "affected_user": _name(u.user)}
        if start <= u.requested_at < end:
            rows.append(_row(u.requested_at, URGENT, f"{kind}: requested", actor_name=_name(u.user), actor_role="User", detail=u.get_request_type_display(), **common))
        if u.wallet_approved_at and start <= u.wallet_approved_at < end:
            rows.append(_row(u.wallet_approved_at, URGENT, f"{kind}: approved by the wallet owner", actor_name=_name(u.wallet_approved_by), actor_role="Wallet owner", **common))
        if u.decided_at and start <= u.decided_at < end:
            by = u.decided_by
            rows.append(_row(u.decided_at, URGENT, f"{kind}: {u.get_status_display().lower()}", actor_name=_name(by), actor_role=_role(by) if by else SYSTEM_ROLE,
                             detail=" ".join(x for x in (u.reviewer_comment, u.admin_notes) if x), **common))
    return rows


def _exchange_rows(start, end) -> list[dict]:
    from .slot_exchange_models import SlotExchangeRequest, SlotExchangeStatus as S
    from iic_booking.communication.utils import booking_display_id_for_email as ref

    rows = []
    query = Q(**_in_range("created_at", start, end)) | Q(**_in_range("responded_at", start, end))
    for x in SlotExchangeRequest.objects.filter(query).select_related("requester", "responder", "requester_booking__equipment", "responder_booking"):
        booking = x.requester_booking
        common = {"equipment": getattr(getattr(booking, "equipment", None), "name", ""), "booking_ref": " / ".join(ref(b) for b in (booking, x.responder_booking) if b is not None)}
        if start <= x.created_at < end:
            rows.append(_row(x.created_at, EXCHANGE, "Requested", actor_name=_name(x.requester), actor_role="User", affected_user=_name(x.responder), detail=x.message, **common))
        if x.responded_at and start <= x.responded_at < end:
            actor = {S.ACCEPTED: x.responder, S.DECLINED: x.responder, S.CANCELLED: x.requester}.get(x.status)
            rows.append(_row(x.responded_at, EXCHANGE, x.get_status_display(), actor_name=_name(actor), actor_role="User" if actor else SYSTEM_ROLE,
                             affected_user=_name(x.requester if actor == x.responder else x.responder), detail=" ".join(y for y in (x.response_note, x.failure_reason) if y), **common))
    return rows


def _refund_rows(start, end) -> list[dict]:
    from iic_booking.communication.utils import booking_display_id_for_email as ref
    from iic_booking.external_services.models import ExternalRefund
    from iic_booking.users.models.wallet_admin_adjustment import WalletAdminAdjustment, WalletAdminAdjustmentReason

    from .booking_audit import resolve_event_audit
    from .models import BookingEvent, BookingEventType

    rows, cache = [], {}
    events = (BookingEvent.objects.filter(**_in_range("created_at", start, end)).filter(Q(event_type=BookingEventType.REFUNDED) | Q(metadata__has_key="refund_amount"))
              .select_related("booking__user", "booking__equipment", "booking__cancellation_record", "created_by"))
    for e in events:
        audit = resolve_event_audit(e, cache)
        amount = (e.metadata or {}).get("refund_amount")
        if amount in (None, ""):
            amount = getattr(getattr(e.booking, "cancellation_record", None), "refund_amount", None)
        if amount in (None, "") or float(amount) <= 0:
            if e.event_type != BookingEventType.REFUNDED:
                continue
        system = audit.get("actor_kind") == "system"
        rows.append(_row(e.created_at, REFUND, "Refund to wallet", actor_name=audit.get("actor_name") or _name(e.created_by), actor_role=SYSTEM_ROLE if system else (audit.get("actor_role_label") or ""),
                         equipment=getattr(e.booking.equipment, "name", ""), affected_user=_name(e.booking.user), booking_ref=ref(e.booking), amount=amount,
                         detail=audit.get("reason_label") or e.comment))
    for r in ExternalRefund.objects.filter(destination="bank", **_in_range("created_at", start, end)).select_related("booking__equipment", "user", "created_by"):
        by = r.created_by
        rows.append(_row(r.created_at, REFUND, f"Refund to bank: {r.get_status_display().lower()}", actor_name=_name(by) or _name(r.user), actor_role=_role(by) if by else "User",
                         equipment=getattr(r.booking.equipment, "name", ""), affected_user=_name(r.user), booking_ref=ref(r.booking), amount=r.refund_amount, detail=r.reason))
    for r in ExternalRefund.objects.filter(closed_at__isnull=False, **_in_range("closed_at", start, end)).select_related("booking__equipment", "user", "closed_by"):
        rows.append(_row(r.closed_at, REFUND, "Bank refund closed", actor_name=_name(r.closed_by), actor_role=_role(r.closed_by) if r.closed_by else "",
                         equipment=getattr(r.booking.equipment, "name", ""), affected_user=_name(r.user), booking_ref=ref(r.booking), amount=r.refund_amount,
                         detail=" ".join(x for x in (f"UTR {r.utr}" if r.utr else "", r.close_note) if x)))
    adjustments = WalletAdminAdjustment.objects.filter(reason=WalletAdminAdjustmentReason.REFUND_OUTSIDE_SYSTEM, **_in_range("created_at", start, end)).select_related("performed_by", "wallet__user")
    for a in adjustments:
        rows.append(_row(a.created_at, REFUND, f"Manual wallet {a.direction}: refund outside the system", actor_name=_name(a.performed_by), actor_role=_role(a.performed_by),
                         affected_user=_name(a.wallet.user), amount=a.amount, detail=" ".join(x for x in (a.reference, a.remarks) if x)))
    return rows


READERS = {REFUND: (_refund_rows,), URGENT: (_urgent_rows, _logged_rows), EXCHANGE: (_exchange_rows,), WAITLIST: (_logged_rows,)}


def _bounds(params) -> tuple[datetime, datetime]:
    tz = timezone.get_current_timezone()
    first, last = parse_date(params.get("date_from") or ""), parse_date(params.get("date_to") or "")
    last = last or timezone.localdate()
    first = first or (last - timedelta(days=DEFAULT_DAYS))
    return timezone.make_aware(datetime.combine(first, time.min), tz), timezone.make_aware(datetime.combine(last + timedelta(days=1), time.min), tz)


def build_rows(params) -> list[dict]:
    start, end = _bounds(params)
    wanted = params.get("category") or ""
    categories = [wanted] if wanted in READERS else list(READERS)
    readers = {reader for category in categories for reader in READERS[category]}
    rows = [row for reader in readers for row in reader(start, end)]
    if wanted in READERS:
        rows = [row for row in rows if row["category"] == wanted]
    q = (params.get("q") or "").strip().lower()
    if q:
        rows = [row for row in rows if q in " ".join(str(row[k]) for k in ("action", "actor_name", "actor_role", "equipment", "affected_user", "booking_ref", "detail")).lower()]
    rows.sort(key=lambda row: row["at"], reverse=True)
    return rows


def _shown(row: dict) -> dict:
    return {**row, "at": timezone.localtime(row["at"]).isoformat()}


def _csv_cell(value) -> str:
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def operations_audit(request):
    """Main Administrator. Filters: category, date_from, date_to (YYYY-MM-DD, last 90 days by default), q; page, page_size; export=csv."""
    from iic_booking.users.admin_wallet_ledger import is_main_admin

    if not is_main_admin(request.user):
        return Response({"error": "The operations audit is available to the Main Administrator."}, status=status.HTTP_403_FORBIDDEN)
    params = request.query_params
    rows = build_rows(params)
    if (params.get("export") or "").lower() == "csv":
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="operations-audit.csv"'
        response.write("﻿")
        writer = csv.writer(response)
        writer.writerow([label for label, _ in CSV_COLUMNS])
        for row in rows[:CSV_MAX_ROWS]:
            shown = _shown(row)
            writer.writerow([_csv_cell(shown[key]) for _, key in CSV_COLUMNS])
        return response
    size = min(max(_int(params.get("page_size"), PAGE_SIZE_DEFAULT), 1), PAGE_SIZE_MAX)
    page = max(_int(params.get("page"), 1), 1)
    first = (page - 1) * size
    return Response({"count": len(rows), "page": page, "page_size": size, "num_pages": max((len(rows) + size - 1) // size, 1),
                     "categories": [{"value": k, "label": v} for k, v in CATEGORY_LABELS.items()], "results": [_shown(row) for row in rows[first:first + size]]})

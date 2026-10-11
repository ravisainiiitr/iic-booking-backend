"""User card opened from the Users overview: ID card, recent bookings and, for wallet owners, linked users' bookings.

Who can be opened: the Users overview population (a Department Administrator: users of their department and the
supervisors of those users). Bookings follow the viewer's Reports scope (a Department Administrator sees bookings on
their department's equipment). Wallet balance and the ledger link are for the Main Administrator only, as on the
Wallet Ledger.

Linked users of a wallet: the owner and every user whose request to join the owner's wallet is approved. Charged =
the booking charge of bookings that were not cancelled / refunded / stopped by the lab (no-shows stay charged).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.contrib.auth import get_user_model
from django.db.models import Count, DateTimeField, Max, Min, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce

from iic_booking.equipment.admin_dashboard_summary import _Scope

from .common import bounds, int_values, iso, money, multi, page_meta, page_params, parse_date
from .supervisors import resolve_supervisors
from .users import CATEGORIES, PROGRAMMES, _annotated, _classification_table, category_case, programme_for

RECENT = 10
CERTIFICATIONS = 25


class CardError(Exception):
    def __init__(self, message: str, status: int = 404):
        super().__init__(message)
        self.message, self.status = message, status


class _Everyone:
    is_institute = True

    def by_department(self, qs, field):
        return qs


def _visible(scope: _Scope, user_id: int):
    """The user if the viewer may open their card (Users overview population or a supervisor of it)."""
    from iic_booking.users.models import RegistrationApproval
    from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus

    found = _annotated(scope).filter(pk=user_id).select_related("department").first()
    if found is None and not scope.is_institute and scope.department_id:
        members = get_user_model().objects.filter(department_id=scope.department_id, is_test_account=False)
        joined = WalletJoinRequest.objects.filter(
            Q(wallet__user_id=user_id) | Q(faculty_id=user_id),
            status__in=[WalletJoinRequestStatus.APPROVED, WalletJoinRequestStatus.PENDING],
            student__in=members,
        )
        supervises = (
            members.filter(supervisor_id=user_id).exists()
            or joined.exists()
            or RegistrationApproval.objects.filter(faculty_id=user_id, user__in=members).exists()
        )
        if supervises:
            found = _annotated(_Everyone()).filter(pk=user_id).select_related("department").first()
    if found is None:
        raise CardError("User not found.")
    return found


def _bookings_scope(viewer):
    from iic_booking.equipment.booking_report_metrics import report_bookings_scope

    bookings, _ = report_bookings_scope(viewer)
    return bookings.order_by()


def _slot_annotations():
    from iic_booking.equipment.models import BookingSlotRange, DailySlot

    held = DailySlot.objects.filter(booking_id=OuterRef("pk")).order_by().values("booking_id")
    released = BookingSlotRange.objects.filter(booking_id=OuterRef("pk"))
    return {
        "slot_start": Coalesce(
            Subquery(held.annotate(s=Min("start_datetime")).values("s")[:1], output_field=DateTimeField()),
            Subquery(released.values("start_datetime")[:1], output_field=DateTimeField()),
        ),
        "slot_end": Coalesce(
            Subquery(held.annotate(e=Max("end_datetime")).values("e")[:1], output_field=DateTimeField()),
            Subquery(released.values("end_datetime")[:1], output_field=DateTimeField()),
        ),
    }


def _booking_row(b) -> dict[str, Any]:
    from iic_booking.users.display import get_user_display_name

    return {
        "pk": b.pk,
        "display_id": b.virtual_booking_id or str(b.pk),
        "equipment": {"id": b.equipment_id, "name": b.equipment.name, "code": b.equipment.code},
        "user": {"id": b.user_id, "name": get_user_display_name(b.user)},
        "slot_start": iso(getattr(b, "slot_start", None)),
        "status": b.status,
        "status_display": b.get_status_display(),
        "charge": money(b.total_charge),
        "created_at": iso(b.created_at),
    }


def _charged_q() -> Q:
    from iic_booking.equipment.booking_cancellation_log import CANCELLATION_STATUSES

    return ~Q(status__in=sorted(CANCELLATION_STATUSES))


def _wallet(user) -> tuple[Any, list[int]]:
    """``(wallet owned by user or None, linked user ids incl. the owner)``."""
    from iic_booking.users.models.wallet import Wallet, WalletJoinRequest, WalletJoinRequestStatus

    wallet = Wallet.objects.filter(user_id=user.pk).first()
    if wallet is None:
        return None, []
    students = (
        WalletJoinRequest.objects.filter(Q(wallet=wallet) | Q(faculty_id=user.pk), status=WalletJoinRequestStatus.APPROVED)
        .values_list("student_id", flat=True)
        .distinct()
    )
    return wallet, [user.pk, *[s for s in students if s and s != user.pk]]


def _wallet_info(user, is_main: bool) -> dict[str, Any] | None:
    """Wallet the user books from (own or the supervisor's they joined) — Main Administrator only."""
    from iic_booking.users.models.wallet import SubWallet

    if not is_main:
        return None
    try:
        wallet = user.get_accessible_wallet()
    except Exception:
        wallet = None
    if wallet is None:
        return None
    balance = SubWallet.objects.filter(wallet=wallet).aggregate(s=Sum("balance"))["s"] or Decimal("0")
    return {
        "owner_id": wallet.user_id,
        "is_owner": wallet.user_id == user.pk,
        "balance": money(balance),
    }


def _certifications(user) -> list[dict[str, Any]]:
    try:
        from iic_booking.training.models import CertificationAward
    except Exception:  # training app not installed
        return []
    rows = CertificationAward.objects.filter(user=user).select_related("equipment", "level").order_by("-awarded_at")
    return [
        {
            "id": a.pk,
            "equipment": a.equipment.name if a.equipment_id else "",
            "level": a.level.name,
            "status": a.status,
            "status_display": a.get_status_display(),
            "awarded_at": iso(a.awarded_at),
            "valid_until": iso(a.valid_until),
            "certificate_no": a.certificate_no or "",
        }
        for a in rows[:CERTIFICATIONS]
    ]


def build_user_card(viewer, user_id: int, request=None) -> dict[str, Any]:
    from iic_booking.users.admin_wallet_ledger import is_main_admin

    person = _visible(_Scope(viewer), user_id)
    return _card_payload(viewer, person, request, is_main=is_main_admin(viewer), bookings=_bookings_scope(viewer))


def _any_person(user_id: int, *, include_test: bool = False):
    """``user_id`` annotated like the Users overview, outside any department scope (test accounts only for self)."""
    if not include_test:
        return _annotated(_Everyone()).filter(pk=user_id).select_related("department").first()
    from django.db.models import CharField, Value
    from django.db.models.functions import NullIf

    case, code = category_case()
    degree = Coalesce(
        NullIf("channel_i_identity__student_degree_name", Value("")),
        NullIf("degree_name", Value("")),
        Value(""),
        output_field=CharField(),
    )
    return (
        get_user_model()
        .objects.filter(pk=user_id)
        .annotate(user_type_code=code, degree_key=degree)
        .annotate(category=case)
        .select_related("department")
        .first()
    )


def _scoped_bookings(viewer):
    """Bookings the viewer may see: their Reports scope; an Accounts In Charge sees their department's bookings."""
    from iic_booking.users.models.user_type import UserType

    if getattr(viewer, "user_type", None) == UserType.FINANCE:
        from iic_booking.users.finance_reports import _department_scoped_bookings_qs

        dept_id = getattr(viewer, "department_id", None)
        if not dept_id:
            from iic_booking.equipment.models import Booking

            return Booking.objects.none()
        return _department_scoped_bookings_qs(dept_id).order_by()
    return _bookings_scope(viewer)


def _within_scope(viewer, user_id: int, bookings) -> bool:
    """The user booked within the viewer's scope, is the viewer's linked student, asked their equipment for help, or
    is an OIC / Lab Operator of equipment the viewer booked or looks after."""
    from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus

    if bookings.filter(user_id=user_id).exists():
        return True
    if WalletJoinRequest.objects.filter(
        faculty=viewer, student_id=user_id, status=WalletJoinRequestStatus.APPROVED
    ).exists():
        return True
    from iic_booking.equipment.api_views import _get_equipment_ids_for_log_access, check_operator_permission
    from iic_booking.equipment.models import EquipmentManager, EquipmentOperator, UrgentBookingRequest

    booked_equipment = bookings.values("equipment_id")
    if (
        EquipmentManager.objects.filter(manager_id=user_id, equipment_id__in=booked_equipment).exists()
        or EquipmentOperator.objects.filter(operator_id=user_id, equipment_id__in=booked_equipment).exists()
    ):
        return True
    if not check_operator_permission(viewer):
        return False
    equipment_ids = _get_equipment_ids_for_log_access(viewer)
    if not equipment_ids:
        return False
    return (
        UrgentBookingRequest.objects.filter(user_id=user_id, equipment_id__in=equipment_ids).exists()
        or EquipmentManager.objects.filter(manager_id=user_id, equipment_id__in=equipment_ids).exists()
        or EquipmentOperator.objects.filter(operator_id=user_id, equipment_id__in=equipment_ids).exists()
    )


def build_scoped_user_card(viewer, user_id: int, request=None) -> dict[str, Any]:
    """The ID card any signed-in user may open, limited to what their role allows.

    - Main / Department Administrators: the full card (as on the Users overview) for users in their population.
    - Anyone: their own card.
    - OIC / Lab Operators: users who booked (or sent an urgent request for) equipment they look after; Department
      Administrators also users who booked their department's equipment; Accounts In Charge users who booked their
      department's equipment; supervisors their approved linked students.

    Outside the full card: no wallet balance, no linked users' bookings, and bookings limited to the viewer's scope.
    """
    from iic_booking.equipment.admin_dashboard_summary import can_view_admin_dashboard
    from iic_booking.users.admin_wallet_ledger import is_main_admin

    if can_view_admin_dashboard(viewer):
        try:
            card = build_user_card(viewer, user_id, request)
        except CardError:
            pass
        else:
            return {**card, "access": "full"}
    if getattr(viewer, "pk", None) == user_id:
        person = _any_person(user_id, include_test=True)
        if person is None:
            raise CardError("User not found.")
        bookings = _bookings_scope(viewer)
        return {**_card_payload(viewer, person, request, is_main=False, bookings=bookings, scoped=True), "access": "self"}
    bookings = _scoped_bookings(viewer)
    if not _within_scope(viewer, user_id, bookings):
        raise CardError("User not found.")
    person = _any_person(user_id)
    if person is None:
        raise CardError("User not found.")
    payload = _card_payload(viewer, person, request, is_main=is_main_admin(viewer), bookings=bookings, scoped=True)
    return {**payload, "access": "scoped"}


def _card_payload(viewer, person, request, *, is_main: bool, bookings, scoped: bool = False) -> dict[str, Any]:
    from iic_booking.users.display import get_user_display_name
    from iic_booking.users.models.user_type import UserType

    type_labels = {code.lower(): str(label) for code, label in UserType.get_choices()}
    programme = (
        programme_for(person.user_type_alias, person.degree_key, _classification_table())
        if person.category == "iitr_student"
        else None
    )
    supervisor = resolve_supervisors([person.pk]).get(person.pk)
    phones = [p for p in ((person.phone_number or "").strip(), (person.secondary_phone_number or "").strip()) if p]

    bookings = bookings.filter(user_id=person.pk)
    stats = bookings.aggregate(
        total=Count("pk"),
        charged=Sum("total_charge", filter=_charged_q()),
        cancelled=Count("pk", filter=~_charged_q()),
    )
    recent = (
        bookings.select_related("equipment", "user").annotate(**_slot_annotations()).order_by("-created_at", "-pk")[:RECENT]
    )
    wallet, linked = _wallet(person)
    return {
        "profile": {
            "id": person.pk,
            "name": get_user_display_name(person),
            "email": person.email or "",
            "phone": " · ".join(phones),
            "profile_picture_url": person.get_profile_picture_url_or_none(request=request),
            "user_type_display": person.user_type_alias
            or type_labels.get(str(person.user_type or "").lower(), person.user_type or ""),
            "category": person.category,
            "category_display": CATEGORIES.get(person.category, "Other"),
            "programme_display": PROGRAMMES[programme] if programme else "",
            "employee_id": person.emp_id or "",
            "designation": getattr(person, "designation", "") or "",
            "degree_name": person.degree_key or "",
            "department": (
                {
                    "id": person.department_id,
                    "name": person.department.name,
                    "type": person.department.department_type or "",
                }
                if person.department_id
                else None
            ),
            "supervisor": supervisor,
            "is_active": bool(person.is_active),
            "is_test_account": bool(person.is_test_account),
            "date_joined": iso(person.date_joined),
            "last_login": iso(person.last_login),
        },
        "wallet": None if scoped else _wallet_info(person, is_main),
        "linked_wallet": (
            {"owner_id": person.pk, "linked_users": len(linked)} if wallet is not None and not scoped else None
        ),
        "certifications": _certifications(person),
        "bookings": {
            "total": stats["total"] or 0,
            "charged": money(stats["charged"]),
            "cancelled": stats["cancelled"] or 0,
            "recent": [_booking_row(b) for b in recent],
        },
    }


def _member_options(ids: list[int]) -> list[dict[str, Any]]:
    from iic_booking.users.display import get_user_display_name

    people = {u.pk: u for u in get_user_model().objects.filter(pk__in=ids)}
    return [
        {"id": pk, "name": get_user_display_name(people[pk]), "email": people[pk].email or "", "is_owner": i == 0}
        for i, pk in enumerate(ids)
        if pk in people
    ]


def build_wallet_bookings(viewer, owner_id: int, params) -> dict[str, Any]:
    """Bookings by every user linked to ``owner_id``'s wallet (owner included), filtered and paged, with totals."""
    from iic_booking.equipment.models import BookingStatus, Equipment

    scope = _Scope(viewer)
    owner = _visible(scope, owner_id)
    wallet, linked = _wallet(owner)
    if wallet is None:
        raise CardError("This user does not own a wallet.")
    members = _member_options(linked)

    base = _bookings_scope(viewer).filter(user_id__in=linked)
    qs = base
    chosen = [m for m in int_values(multi(params, "member")) if m in linked]
    if chosen:
        qs = qs.filter(user_id__in=chosen)
    equipment = int_values(multi(params, "equipment"))
    if equipment:
        qs = qs.filter(equipment_id__in=equipment)
    statuses = [s.upper() for s in multi(params, "status")]
    if statuses:
        qs = qs.filter(status__in=statuses)
    start, end = parse_date(params.get("date_from")), parse_date(params.get("date_to"))
    if start or end:
        start_at, end_at = bounds(start or end, end or start)
        qs = qs.filter(created_at__gte=start_at, created_at__lt=end_at)
    search = str(params.get("search") or "").strip()
    if search:
        q = Q(virtual_booking_id__icontains=search) | Q(equipment__name__icontains=search) | Q(
            equipment__code__icontains=search
        )
        if search.isdigit():
            q |= Q(booking_id=int(search))
        qs = qs.filter(q)

    charged = _charged_q()
    per_member = {
        r["user_id"]: r
        for r in qs.values("user_id").annotate(
            bookings=Count("pk"),
            charged=Sum("total_charge", filter=charged),
            cancelled=Count("pk", filter=~charged),
        )
    }
    totals_by_member = [
        {
            **m,
            "bookings": (per_member.get(m["id"]) or {}).get("bookings", 0),
            "charged": money((per_member.get(m["id"]) or {}).get("charged")),
            "cancelled": (per_member.get(m["id"]) or {}).get("cancelled", 0),
        }
        for m in members
    ]
    total = sum(m["bookings"] for m in totals_by_member)
    offset, size = page_params(params)
    page = list(
        qs.select_related("equipment", "user")
        .annotate(**_slot_annotations())
        .order_by("-created_at", "-pk")[offset : offset + size]
    )
    payload = {
        "owner": {"id": owner.pk, "name": members[0]["name"] if members else ""},
        "summary": {
            "linked_users": len(linked),
            "bookings": total,
            "charged": money(sum((Decimal(str(m["charged"])) for m in totals_by_member), Decimal("0"))),
            "cancelled": sum(m["cancelled"] for m in totals_by_member),
            "by_member": totals_by_member,
        },
        "results": [_booking_row(b) for b in page],
        **page_meta(total, offset, size),
    }
    if params.get("with_options"):
        equipment_ids = base.values("equipment_id").distinct()
        payload["options"] = {
            "members": members,
            "equipment": [
                {"id": e["equipment_id"], "name": e["name"], "code": e["code"]}
                for e in Equipment.objects.filter(pk__in=equipment_ids).order_by("name").values(
                    "equipment_id", "name", "code"
                )
            ],
            "statuses": [{"value": v, "label": str(label)} for v, label in BookingStatus.choices],
        }
    return payload

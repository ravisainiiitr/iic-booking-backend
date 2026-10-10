"""Filters shared by the members table, CSV export, department breakdown and email recipients.

Internal / external: the user type decides (IITR students / faculty / IITR startups are internal; educational
institutes, R&D, industry, external startups and "other" are external). Staff types fall back to their
department's type. Department is the IITR department for internal users and the organisation (an external
department row) for external users.

The booking date range uses when the booking was made (``created_at``). For supervisor-only members it uses their
latest supervised booking, as individual supervised bookings are not stored per member.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable, Optional

from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone
from django.utils.dateparse import parse_date

from iic_booking.users.models import User, UserType
from iic_booking.users.models.department import DepartmentType

from .membership import bookings_scope_q, counted_bookings
from .models import FacilityUserGroup, FacilityUserGroupMember, GroupKind

INTERNAL_CODES = sorted(UserType.get_internal_user_codes())
EXTERNAL_CODES = sorted(UserType.get_external_user_codes())
NO_DEPARTMENT = 0

BOOKING_USER_TYPES = [
    UserType.STUDENT,
    UserType.INDIVIDUAL_STUDENT,
    UserType.FACULTY,
    UserType.STARTUP_INCUBATED_IITR,
    UserType.EXTERNAL,
    UserType.RND,
    UserType.INSTITUTE,
    UserType.EXTERNAL_STARTUP_MSME,
    UserType.OTHER,
]


class FilterError(ValueError):
    def __init__(self, message: str, field_name: str = ""):
        super().__init__(message)
        self.field = field_name


def _list(data: Any, name: str) -> list[str]:
    if data is None:
        return []
    raw: Any
    if hasattr(data, "getlist"):
        raw = data.getlist(name) or data.getlist(f"{name}[]")
        if len(raw) == 1:
            raw = raw[0]
    else:
        raw = data.get(name)
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, (list, tuple)):
        raw = [raw]
    return [str(v).strip() for v in raw if str(v).strip()]


def _bool(data: Any, name: str, default: bool = False) -> bool:
    value = data.get(name) if data is not None else None
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _date(data: Any, name: str) -> Optional[date]:
    value = data.get(name) if data is not None else None
    if not value:
        return None
    if isinstance(value, date):
        return value
    parsed = parse_date(str(value).strip()[:10])
    if parsed is None:
        raise FilterError(f"{name} must be a date (YYYY-MM-DD).", name)
    return parsed


@dataclass
class AudienceFilters:
    department_ids: list[int] = field(default_factory=list)
    user_types: list[str] = field(default_factory=list)
    audience: str = ""
    booked_from: Optional[date] = None
    booked_to: Optional[date] = None
    include_supervisors: bool = False
    include_inactive: bool = False
    include_test_accounts: bool = False
    search: str = ""

    @classmethod
    def from_data(cls, data: Any) -> "AudienceFilters":
        data = data if data is not None else {}
        try:
            departments = [int(v) for v in _list(data, "department_ids") or _list(data, "department")]
        except ValueError as exc:
            raise FilterError("Departments must be ids.", "department_ids") from exc
        audience = str(data.get("audience") or "").strip().lower()
        if audience not in {"", "all", "internal", "external"}:
            raise FilterError("Audience must be internal or external.", "audience")
        valid_types = {code for code, _ in UserType.get_choices()}
        user_types = _list(data, "user_types") or _list(data, "user_type")
        unknown = [t for t in user_types if t not in valid_types]
        if unknown:
            raise FilterError(f"Unknown user type: {unknown[0]}.", "user_types")
        f = cls(
            department_ids=departments,
            user_types=user_types,
            audience="" if audience == "all" else audience,
            booked_from=_date(data, "booked_from"),
            booked_to=_date(data, "booked_to"),
            include_supervisors=_bool(data, "include_supervisors"),
            include_inactive=_bool(data, "include_inactive"),
            include_test_accounts=_bool(data, "include_test_accounts"),
            search=str(data.get("search") or data.get("q") or "").strip()[:100],
        )
        if f.booked_from and f.booked_to and f.booked_from > f.booked_to:
            raise FilterError("The booking date range starts after it ends.", "booked_from")
        return f

    def to_json(self) -> dict:
        out = asdict(self)
        out["booked_from"] = self.booked_from.isoformat() if self.booked_from else None
        out["booked_to"] = self.booked_to.isoformat() if self.booked_to else None
        return out

    def range_bounds(self) -> tuple[Optional[datetime], Optional[datetime]]:
        tz = timezone.get_current_timezone()
        start = timezone.make_aware(datetime.combine(self.booked_from, time.min), tz) if self.booked_from else None
        end = (
            timezone.make_aware(datetime.combine(self.booked_to + timedelta(days=1), time.min), tz)
            if self.booked_to
            else None
        )
        return start, end


def external_q(prefix: str = "") -> Q:
    return Q(**{f"{prefix}user_type__in": EXTERNAL_CODES}) | (
        ~Q(**{f"{prefix}user_type__in": INTERNAL_CODES})
        & Q(**{f"{prefix}department__department_type": DepartmentType.EXTERNAL})
    )


def audience_of(user: User) -> str:
    if user.user_type in EXTERNAL_CODES:
        return "external"
    if user.user_type in INTERNAL_CODES:
        return "internal"
    department = getattr(user, "department", None)
    if department is not None and department.department_type == DepartmentType.EXTERNAL:
        return "external"
    return "internal"


def user_q(f: AudienceFilters, prefix: str = "") -> Q:
    q = Q()
    if f.department_ids:
        ids = [i for i in f.department_ids if i != NO_DEPARTMENT]
        dq = Q(**{f"{prefix}department_id__in": ids}) if ids else Q(pk__in=[])
        if NO_DEPARTMENT in f.department_ids:
            dq |= Q(**{f"{prefix}department__isnull": True})
        q &= dq
    if f.user_types:
        q &= Q(**{f"{prefix}user_type__in": f.user_types})
    if f.audience == "external":
        q &= external_q(prefix)
    elif f.audience == "internal":
        q &= ~external_q(prefix)
    if not f.include_inactive:
        q &= Q(**{f"{prefix}is_active": True})
    if not f.include_test_accounts:
        q &= Q(**{f"{prefix}is_test_account": False})
    if f.search:
        s = f.search
        q &= (
            Q(**{f"{prefix}name__icontains": s})
            | Q(**{f"{prefix}email__icontains": s})
            | Q(**{f"{prefix}emp_id__icontains": s})
            | Q(**{f"{prefix}phone_number__icontains": s})
            | Q(**{f"{prefix}department__name__icontains": s})
        )
    return q


def _booking_range_q(group: FacilityUserGroup, f: AudienceFilters) -> Q:
    start, end = f.range_bounds()
    if start is None and end is None:
        return Q()
    bookings = counted_bookings().filter(user_id=OuterRef("user_id"))
    if group.kind != GroupKind.CUSTOM:
        bookings = bookings.filter(bookings_scope_q(group))
    if start is not None:
        bookings = bookings.filter(created_at__gte=start)
    if end is not None:
        bookings = bookings.filter(created_at__lt=end)
    q = Q(Exists(bookings))
    if f.include_supervisors:
        sup = Q(supervised_booking_count__gt=0)
        if start is not None:
            sup &= Q(last_supervised_at__gte=start)
        if end is not None:
            sup &= Q(last_supervised_at__lt=end)
        q |= sup
    return q


def members_qs(group: FacilityUserGroup, f: AudienceFilters):
    qs = FacilityUserGroupMember.objects.filter(group=group).filter(user_q(f, "user__"))
    if not f.include_supervisors:
        qs = qs.filter(Q(booking_count__gt=0) | Q(added_manually=True))
    qs = qs.filter(_booking_range_q(group, f))
    return qs.select_related("user", "user__department")


def role_of(member: FacilityUserGroupMember) -> str:
    if member.booking_count:
        return "booker"
    if member.supervised_booking_count:
        return "supervisor"
    return "manual"


def equipment_booked(group: FacilityUserGroup, user_ids: Iterable[int], f: Optional[AudienceFilters] = None) -> dict[int, list[dict]]:
    ids = list(user_ids)
    if not ids:
        return {}
    qs = counted_bookings().filter(user_id__in=ids)
    if group.kind != GroupKind.CUSTOM:
        qs = qs.filter(bookings_scope_q(group))
    if f is not None:
        start, end = f.range_bounds()
        if start is not None:
            qs = qs.filter(created_at__gte=start)
        if end is not None:
            qs = qs.filter(created_at__lt=end)
    out: dict[int, list[dict]] = defaultdict(list)
    for row in (
        qs.values("user_id", "equipment_id", "equipment__name", "equipment__code")
        .annotate(n=Count("pk"))
        .order_by("user_id", "-n", "equipment__name")
    ):
        out[row["user_id"]].append(
            {"id": row["equipment_id"], "name": row["equipment__name"], "code": row["equipment__code"], "count": row["n"]}
        )
    return out


def department_breakdown(member_rows_qs) -> dict:
    """Counts per department / organisation plus internal / external totals for a members queryset."""
    rows = (
        member_rows_qs.order_by()
        .values("user__department_id", "user__department__name", "user__department__department_type")
        .annotate(
            total=Count("user_id", distinct=True),
            external=Count("user_id", distinct=True, filter=external_q("user__")),
        )
    )
    departments = []
    internal_total = external_total = 0
    for row in rows:
        external = row["external"]
        internal = row["total"] - external
        internal_total += internal
        external_total += external
        departments.append(
            {
                "department_id": row["user__department_id"] or NO_DEPARTMENT,
                "department_name": row["user__department__name"] or "No department",
                "department_type": row["user__department__department_type"] or "",
                "total": row["total"],
                "internal": internal,
                "external": external,
            }
        )
    departments.sort(key=lambda d: (-d["total"], d["department_name"].lower()))
    return {
        "total": internal_total + external_total,
        "internal": internal_total,
        "external": external_total,
        "departments": departments,
    }


def recipient_user_ids(groups: Iterable[FacilityUserGroup], f: AudienceFilters) -> set[int]:
    ids: set[int] = set()
    for group in groups:
        ids |= set(members_qs(group, f).values_list("user_id", flat=True))
    return ids


def user_type_label(code: Optional[str]) -> str:
    if not code:
        return ""
    return str(dict(UserType.get_choices()).get(code, code))

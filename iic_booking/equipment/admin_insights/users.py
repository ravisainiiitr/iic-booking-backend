"""Users overview behind the dashboard's Active Users card.

Population: every account except test accounts, limited to the department for a Department Administrator — the
card's definition (``_users``). Active = the account is enabled (``is_active``); the page opens on active users so
its total matches the card. "Booked in period" means the user created at least one booking in that date range.

Categories come from the account type: IITR Faculty; IITR Students (IITR Student and Individual Student, split by
programme); IITR Startups; IITR Staff (administrators, OICs, lab operators, accounts and stores staff, HoDs);
External Educational; Industry; Govt R&D; External Startup / MSME; Other. Programme: Post-doc / Research Associate
from the registration alias, otherwise the Channel i degree (Degree classifications table, then the degree name).
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import timedelta
from typing import Any

from django.contrib.auth import get_user_model
from django.db.models import (
    Case,
    CharField,
    Count,
    Exists,
    F,
    IntegerField,
    Max,
    OuterRef,
    Q,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Coalesce, Lower, NullIf, TruncMonth, TruncWeek
from django.utils import timezone

from iic_booking.equipment.admin_dashboard_summary import _Scope, _users
from iic_booking.users.models.user_type import UserType

from .common import bounds, flag, int_values, iso, multi, page_meta, page_params, parse_date, scope_for, scope_payload
from .supervisors import resolve_supervisors

CATEGORIES = {
    "iitr_faculty": "IITR Faculty",
    "iitr_student": "IITR Students",
    "iitr_startup": "IITR Startups",
    "iitr_staff": "IITR Staff",
    "external_academic": "External — Educational",
    "external_industry": "Industry",
    "government_rnd": "Govt R&D",
    "external_startup": "External Startup / MSME",
    "other": "Other",
}
INTERNAL_CATEGORIES = ("iitr_faculty", "iitr_student", "iitr_startup", "iitr_staff")
EXTERNAL_CATEGORIES = ("external_academic", "external_industry", "government_rnd", "external_startup", "other")
STAFF_TYPES = (
    UserType.ADMIN,
    UserType.DEPT_ADMIN,
    UserType.MANAGER,
    UserType.OPERATOR,
    UserType.FINANCE,
    UserType.OC_STORES,
    UserType.HOD,
    UserType.EXTERNAL_RELATIONS,
)
STUDENT_TYPES = (UserType.STUDENT, UserType.INDIVIDUAL_STUDENT)

PROGRAMMES = {
    "ug": "Undergraduate",
    "pg": "Postgraduate",
    "phd": "PhD / research",
    "postdoc": "Post-doc / Research Associate",
    "unknown": "Not known",
}
POSTDOC_ALIASES = ("iitr post doctoral fellows", "iitr research associates in projects")
_CLASSIFICATION_PROGRAMME = {"UNDERGRADUATE": "ug", "POSTGRADUATE": "pg", "RESEARCH": "phd"}
_PHD = re.compile(r"\bph\.?\s*d\b|doctor|\bresearch\b", re.IGNORECASE)
_PG = re.compile(
    r"\bm\.?\s*(tech|sc|e|arch|des|plan|ba|phil|ca|s)\b|master|\bmba\b|\bmca\b|post\s*graduate|\bpg\b", re.IGNORECASE
)
_UG = re.compile(
    r"\bb\.?\s*(tech|sc|e|arch|des|s|ba)\b|bachelor|under\s*graduate|\bug\b|integrated|dual", re.IGNORECASE
)

TREND_PERIODS = {"week": 16, "month": 12}
SORTS = {
    "joined": "date_joined",
    "name": "name",
    "bookings": "bookings_count",
    "last_booking": "last_booking_at",
    "email": "email",
}
STATE_LABELS: dict[str, str] = {}


def category_case(user_path: str = "") -> tuple[Case, Lower]:
    """``(category, user_type_code)`` expressions; annotate ``user_type_code`` first, the category reads it."""
    code = Lower(Coalesce(f"{user_path}user_type", Value("")))
    return Case(
        When(user_type_code=UserType.FACULTY, then=Value("iitr_faculty")),
        When(user_type_code__in=STUDENT_TYPES, then=Value("iitr_student")),
        When(user_type_code=UserType.STARTUP_INCUBATED_IITR, then=Value("iitr_startup")),
        When(user_type_code__in=STAFF_TYPES, then=Value("iitr_staff")),
        When(user_type_code=UserType.EXTERNAL, then=Value("external_academic")),
        When(user_type_code=UserType.INSTITUTE.lower(), then=Value("external_industry")),
        When(user_type_code=UserType.RND.lower(), then=Value("government_rnd")),
        When(user_type_code=UserType.EXTERNAL_STARTUP_MSME, then=Value("external_startup")),
        default=Value("other"),
        output_field=CharField(),
    ), code


def _normalize(value) -> str:
    return " ".join(str(value or "").strip().split()).casefold()


def _classification_table() -> dict[str, str]:
    from iic_booking.users.models.channel_i_identity import StudentDegreeClassification

    return {
        row["channel_i_degree_name_normalized"]: row["classification"]
        for row in StudentDegreeClassification.objects.filter(active=True).values(
            "channel_i_degree_name_normalized", "classification"
        )
    }


def programme_for(alias: str | None, degree: str | None, table: dict[str, str]) -> str:
    if _normalize(alias) in POSTDOC_ALIASES:
        return "postdoc"
    name = _normalize(degree)
    if not name:
        return "unknown"
    classified = _CLASSIFICATION_PROGRAMME.get(table.get(name, ""))
    if classified:
        return classified
    if _PHD.search(name):
        return "phd"
    if _PG.search(name):
        return "pg"
    if _UG.search(name):
        return "ug"
    return "unknown"


def _annotated(scope: _Scope):
    case, code = category_case()
    qs = get_user_model().objects.filter(is_test_account=False)
    qs = scope.by_department(qs, "department_id").order_by()
    return qs.annotate(
        user_type_code=code,
        degree_key=Coalesce(
            NullIf("channel_i_identity__student_degree_name", Value("")),
            NullIf("degree_name", Value("")),
            Value(""),
            output_field=CharField(),
        ),
    ).annotate(category=case)


def _programme_q(qs, wanted: list[str]) -> Q:
    """Students whose programme is one of ``wanted`` (classified per distinct degree / alias)."""
    table = _classification_table()
    q = Q(pk__in=[])
    rows = qs.filter(category="iitr_student").values("user_type_alias", "degree_key").distinct()
    for row in rows:
        if programme_for(row["user_type_alias"], row["degree_key"], table) in wanted:
            q |= Q(user_type_alias=row["user_type_alias"], degree_key=row["degree_key"])
    return Q(category="iitr_student") & q


def _filtered(scope: _Scope, params, now):
    qs = _annotated(scope)
    status = str(params.get("status") or "active").strip().lower()
    if status == "active":
        qs = qs.filter(is_active=True)
    elif status == "inactive":
        qs = qs.filter(is_active=False)
    segment = str(params.get("segment") or "").strip().lower()
    if segment == "internal":
        qs = qs.filter(category__in=INTERNAL_CATEGORIES)
    elif segment == "external":
        qs = qs.filter(category__in=EXTERNAL_CATEGORIES)
    categories = [c for c in multi(params, "category") if c in CATEGORIES]
    if categories:
        qs = qs.filter(category__in=categories)
    departments = multi(params, "department")
    if departments:
        q = Q(department_id__in=int_values(departments))
        if "none" in departments:
            q |= Q(department__isnull=True)
        qs = qs.filter(q)
    organisation = str(params.get("organisation") or "").strip()
    if organisation:
        qs = qs.filter(department__name__icontains=organisation)
    states = multi(params, "state")
    if states:
        qs = qs.filter(department__state__in=states)
    joined_from, joined_to = parse_date(params.get("joined_from")), parse_date(params.get("joined_to"))
    if joined_from or joined_to:
        start, end = bounds(joined_from or joined_to, joined_to or joined_from)
        qs = qs.filter(date_joined__gte=start, date_joined__lt=end)
    booked_from, booked_to = parse_date(params.get("booked_from")), parse_date(params.get("booked_to"))
    if booked_from or booked_to:
        from iic_booking.equipment.models import Booking

        start, end = bounds(booked_from or booked_to, booked_to or booked_from)
        booked = Booking.objects.filter(user_id=OuterRef("pk"), created_at__gte=start, created_at__lt=end)
        qs = qs.filter(Exists(booked)) if not flag(params, "not_booked") else qs.exclude(Exists(booked))
    search = str(params.get("search") or "").strip()
    if search:
        qs = qs.filter(
            Q(name__icontains=search)
            | Q(email__icontains=search)
            | Q(phone_number__icontains=search)
            | Q(emp_id__icontains=search)
            | Q(internal_id__icontains=search)
        )
    programmes = [p for p in multi(params, "programme") if p in PROGRAMMES]
    if programmes:
        qs = qs.filter(_programme_q(qs, programmes))
    return qs


def _state_label(code: str | None) -> str:
    if not STATE_LABELS:
        from iic_booking.users.models.department import IndianState

        STATE_LABELS.update({k: str(v) for k, v in IndianState.get_choices()})
    return STATE_LABELS.get(code or "", "") or "State not set"


def _summary(qs, params, now) -> dict[str, Any]:
    from iic_booking.users.models.department import ExternalDepartmentSubcategory

    totals = qs.aggregate(
        total=Count("pk"),
        active=Count("pk", filter=Q(is_active=True)),
        internal=Count("pk", filter=Q(category__in=INTERNAL_CATEGORIES)),
        external=Count("pk", filter=Q(category__in=EXTERNAL_CATEGORIES)),
        new_30=Count("pk", filter=Q(date_joined__gte=now - timedelta(days=30))),
    )
    by_category = {r["category"]: r["n"] for r in qs.values("category").annotate(n=Count("pk"))}

    table = _classification_table()
    programmes: dict[str, int] = defaultdict(int)
    for r in qs.filter(category="iitr_student").values("user_type_alias", "degree_key").annotate(n=Count("pk")):
        programmes[programme_for(r["user_type_alias"], r["degree_key"], table)] += r["n"]

    internal_departments = [
        {
            "id": r["department_id"],
            "name": r["department__name"] or "No department",
            "faculty": r["faculty"],
            "students": r["students"],
            "staff": r["staff"],
            "startups": r["startups"],
            "total": r["total"],
        }
        for r in qs.filter(category__in=INTERNAL_CATEGORIES)
        .values("department_id", "department__name")
        .annotate(
            total=Count("pk"),
            faculty=Count("pk", filter=Q(category="iitr_faculty")),
            students=Count("pk", filter=Q(category="iitr_student")),
            staff=Count("pk", filter=Q(category="iitr_staff")),
            startups=Count("pk", filter=Q(category="iitr_startup")),
        )
        .order_by("-total", "department__name")
    ]
    external = qs.filter(category__in=EXTERNAL_CATEGORIES)
    subcategory_labels = {k: str(v) for k, v in ExternalDepartmentSubcategory.get_choices()}
    organisations = [
        {
            "id": r["department_id"],
            "name": r["department__name"] or "No organisation",
            "type": subcategory_labels.get(r["department__external_subcategory"] or "", ""),
            "state": _state_label(r["department__state"]) if r["department_id"] else "",
            "count": r["n"],
        }
        for r in external.values(
            "department_id", "department__name", "department__external_subcategory", "department__state"
        )
        .annotate(n=Count("pk"))
        .order_by("-n", "department__name")[:50]
    ]
    states = [
        {"key": r["department__state"] or "none", "label": _state_label(r["department__state"]), "count": r["n"]}
        for r in external.values("department__state").annotate(n=Count("pk")).order_by("-n")
    ]
    return {
        "total": totals["total"] or 0,
        "active": totals["active"] or 0,
        "inactive": (totals["total"] or 0) - (totals["active"] or 0),
        "internal": totals["internal"] or 0,
        "external": totals["external"] or 0,
        "new_last_30_days": totals["new_30"] or 0,
        "by_category": [
            {
                "key": key,
                "label": label,
                "segment": "internal" if key in INTERNAL_CATEGORIES else "external",
                "count": by_category.get(key, 0),
            }
            for key, label in CATEGORIES.items()
        ],
        "by_programme": [{"key": k, "label": v, "count": programmes.get(k, 0)} for k, v in PROGRAMMES.items()],
        "internal_by_department": internal_departments,
        "external_by_organisation": organisations,
        "external_by_state": states,
        "trend": _trend(qs, params, now),
    }


def _trend(qs, params, now) -> dict[str, Any]:
    granularity = "week" if str(params.get("trend") or "").strip().lower() == "week" else "month"
    periods = TREND_PERIODS[granularity]
    today = timezone.localdate(now)
    if granularity == "week":
        first = today - timedelta(days=today.weekday()) - timedelta(weeks=periods - 1)
        keys = [first + timedelta(weeks=i) for i in range(periods)]
        trunc = TruncWeek("date_joined", tzinfo=timezone.get_current_timezone())
    else:
        first = today.replace(day=1)
        for _ in range(periods - 1):
            first = (first - timedelta(days=1)).replace(day=1)
        keys = []
        cursor = first
        for _ in range(periods):
            keys.append(cursor)
            cursor = (cursor + timedelta(days=32)).replace(day=1)
        trunc = TruncMonth("date_joined", tzinfo=timezone.get_current_timezone())
    start, _ = bounds(first, first)
    rows = (
        qs.filter(date_joined__gte=start)
        .annotate(bucket=trunc)
        .values("bucket")
        .annotate(
            internal=Count("pk", filter=Q(category__in=INTERNAL_CATEGORIES)),
            external=Count("pk", filter=Q(category__in=EXTERNAL_CATEGORIES)),
        )
    )
    found = {}
    for r in rows:
        bucket = r["bucket"]
        day = timezone.localtime(bucket).date() if hasattr(bucket, "hour") else bucket
        found[day] = r
    series = []
    for key in keys:
        r = found.get(key) or {}
        internal, external = r.get("internal") or 0, r.get("external") or 0
        series.append({"period": key.isoformat(), "internal": internal, "external": external, "total": internal + external})
    return {"granularity": granularity, "series": series}


def _wallet_owners(rows: list[dict], supervisors: dict[int, dict]) -> dict[int, int]:
    """User id -> wallet ledger owner id (their own wallet, else the supervisor's wallet they joined)."""
    from iic_booking.users.models.wallet import Wallet

    joined = {uid: s["id"] for uid, s in supervisors.items() if s["source"] == "wallet" and s["id"]}
    candidates = {r["id"] for r in rows} | {r["supervisor_id"] for r in rows if r["supervisor_id"]} | set(joined.values())
    with_wallet = set(Wallet.objects.filter(user_id__in=candidates).values_list("user_id", flat=True))
    out = {}
    for r in rows:
        if r["id"] in with_wallet:
            out[r["id"]] = r["id"]
        elif joined.get(r["id"]) in with_wallet:
            out[r["id"]] = joined[r["id"]]
        elif r["category"] == "iitr_student" and r["supervisor_id"] in with_wallet:
            out[r["id"]] = r["supervisor_id"]
    return out


def _options(scope: _Scope) -> dict[str, Any]:
    qs = _annotated(scope)
    departments = (
        qs.exclude(department__isnull=True)
        .values("department_id", "department__name", "department__department_type")
        .annotate(n=Count("pk"))
        .order_by("department__name")
    )
    return {
        "categories": [
            {"value": k, "label": v, "segment": "internal" if k in INTERNAL_CATEGORIES else "external"}
            for k, v in CATEGORIES.items()
        ],
        "programmes": [{"value": k, "label": v} for k, v in PROGRAMMES.items()],
        "departments": [
            {"id": d["department_id"], "name": d["department__name"], "type": d["department__department_type"] or ""}
            for d in departments
        ],
    }


def build_user_insights(user, params) -> dict[str, Any]:
    from iic_booking.equipment.models import Booking
    from iic_booking.users.admin_wallet_ledger import is_main_admin

    scope = scope_for(user, params)
    now = timezone.now()
    qs = _filtered(scope, params, now)
    summary = _summary(qs, params, now)

    bookings = Booking.objects.filter(user_id=OuterRef("pk")).order_by().values("user_id")
    listed = qs.annotate(
        bookings_count=Coalesce(
            Subquery(bookings.annotate(n=Count("pk")).values("n")[:1], output_field=IntegerField()), Value(0)
        ),
        last_booking_at=Subquery(bookings.annotate(at=Max("created_at")).values("at")[:1]),
    )
    sort = str(params.get("sort") or "-joined").strip()
    column = F(SORTS.get(sort.lstrip("-"), "date_joined"))
    listed = listed.order_by(column.desc(nulls_last=True) if sort.startswith("-") else column.asc(nulls_last=True), "-pk")
    offset, size = page_params(params)
    total = summary["total"]
    page = list(
        listed.values(
            "id",
            "name",
            "email",
            "phone_number",
            "user_type",
            "user_type_alias",
            "category",
            "degree_key",
            "department_id",
            "department__name",
            "department__department_type",
            "supervisor_id",
            "date_joined",
            "is_active",
            "bookings_count",
            "last_booking_at",
        )[offset : offset + size]
    )
    type_labels = {code.lower(): str(label) for code, label in UserType.get_choices()}
    table = _classification_table()
    supervisors = resolve_supervisors(r["id"] for r in page)
    wallets = _wallet_owners(page, supervisors) if is_main_admin(user) else {}
    results = []
    for r in page:
        programme = (
            programme_for(r["user_type_alias"], r["degree_key"], table) if r["category"] == "iitr_student" else None
        )
        results.append(
            {
                "id": r["id"],
                "name": r["name"] or "",
                "email": r["email"] or "",
                "phone": r["phone_number"] or "",
                "category": r["category"],
                "category_display": CATEGORIES.get(r["category"], "Other"),
                "user_type_display": r["user_type_alias"]
                or type_labels.get(str(r["user_type"] or "").lower(), r["user_type"] or ""),
                "programme": programme,
                "programme_display": PROGRAMMES[programme] if programme else "",
                "department": (
                    {
                        "id": r["department_id"],
                        "name": r["department__name"],
                        "type": r["department__department_type"] or "",
                    }
                    if r["department_id"]
                    else None
                ),
                "date_joined": iso(r["date_joined"]),
                "is_active": bool(r["is_active"]),
                "bookings_count": r["bookings_count"] or 0,
                "last_booking_at": iso(r["last_booking_at"]),
                "wallet_owner_id": wallets.get(r["id"]),
                "supervisor": supervisors.get(r["id"]),
            }
        )
    payload = {
        **scope_payload(scope),
        "generated_at": now.isoformat(),
        "card": _users(scope, now),
        "definitions": {
            "active": "Account enabled (can sign in). Test accounts are never counted.",
            "booked_in_period": "Created at least one booking (any status) in the chosen dates.",
            "programme": (
                "Post-doc / Research Associate from the registration type; otherwise the Channel i degree, "
                "using the Degree classifications table and then the degree name."
            ),
        },
        "summary": summary,
        "results": results,
        **page_meta(total, offset, size),
    }
    if params.get("with_options"):
        payload["options"] = _options(scope)
    return payload

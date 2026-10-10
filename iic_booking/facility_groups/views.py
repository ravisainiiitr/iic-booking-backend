"""Facility user groups API (Main Administrator only), mounted at /api/v1/admin/facility-groups/."""

from __future__ import annotations

import csv
import json
import logging
from collections import defaultdict
from functools import wraps
from io import StringIO

from django.db import OperationalError, ProgrammingError, transaction
from django.db.models import Count, Max, Q
from django.http import HttpResponse
from django.utils import timezone
from django.utils.text import slugify
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.display import get_user_display_name
from iic_booking.users.models import Department, User, UserType

from . import group_email
from .audience import (
    BOOKING_USER_TYPES,
    AudienceFilters,
    FilterError,
    audience_of,
    department_breakdown,
    equipment_booked,
    members_qs,
    role_of,
    user_q,
    user_type_label,
)
from .group_email import GroupEmailError
from .models import (
    CampaignStatus,
    CcMode,
    FacilityUserGroup,
    FacilityUserGroupMember,
    GroupEmailCampaign,
    GroupKind,
    RecipientStatus,
)

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 500
MAX_ADD = 5000
NOT_MIGRATED = "User groups are not available until the facility_groups database migration is applied."


class GroupError(Exception):
    def __init__(self, message: str, *, status: int = 400, field_name: str = ""):
        super().__init__(message)
        self.message = message
        self.status = status
        self.field = field_name


def is_main_admin(user) -> bool:
    return bool(getattr(user, "is_authenticated", False)) and getattr(user, "user_type", None) == UserType.ADMIN


def _error(message: str, status: int, *, code: str = "invalid", field: str = "", extra: dict | None = None):
    body = {"detail": message, "code": code}
    if field:
        body["field"] = field
    if extra:
        body.update(extra)
    return Response(body, status=status)


def fg_api(methods, *, multipart: bool = False):
    def deco(fn):
        @wraps(fn)
        def inner(request, *args, **kwargs):
            if not is_main_admin(request.user):
                return _error("Only the Main Administrator can manage user groups.", 403, code="forbidden")
            try:
                with transaction.atomic():
                    return fn(request, *args, **kwargs)
            except GroupError as exc:
                return _error(exc.message, exc.status, field=exc.field)
            except GroupEmailError as exc:
                return _error(str(exc), exc.status, field=exc.field, extra=exc.extra)
            except FilterError as exc:
                return _error(str(exc), 400, field=exc.field)
            except (ProgrammingError, OperationalError):
                logger.exception("facility groups API database error")
                return _error(NOT_MIGRATED, 503, code="unavailable")

        view = permission_classes([IsAuthenticated])(inner)
        if multipart:
            view = parser_classes([JSONParser, MultiPartParser, FormParser])(view)
        return api_view(methods)(view)

    return deco


def _iso(value):
    return value.isoformat() if value else None


def _get_group(group_id: int) -> FacilityUserGroup:
    group = FacilityUserGroup.objects.select_related("equipment", "category", "equipment_group", "lab").filter(pk=group_id).first()
    if group is None:
        raise GroupError("Group not found.", status=404)
    return group


def _scope(group: FacilityUserGroup) -> dict:
    if group.kind == GroupKind.EQUIPMENT and group.equipment_id:
        return {"type": "equipment", "id": group.equipment_id, "code": getattr(group.equipment, "code", "")}
    if group.kind == GroupKind.CATEGORY and group.category_id:
        return {"type": "category", "id": group.category_id}
    if group.kind == GroupKind.EQUIPMENT_GROUP and group.equipment_group_id:
        return {"type": "equipment_group", "id": group.equipment_group_id}
    if group.kind == GroupKind.LAB and group.lab_id:
        return {"type": "lab", "id": group.lab_id, "code": getattr(group.lab, "code", "")}
    return {"type": group.kind}


def _counted_members() -> Q:
    return Q(memberships__user__is_active=True, memberships__user__is_test_account=False)


def _group_row(group: FacilityUserGroup) -> dict:
    return {
        "id": group.pk,
        "name": group.name,
        "kind": group.kind,
        "kind_label": str(GroupKind(group.kind).label),
        "description": group.description,
        "is_automatic": group.is_automatic,
        "is_archived": group.is_archived,
        "scope": _scope(group),
        "member_count": getattr(group, "member_count", None),
        "supervisor_count": getattr(group, "supervisor_count", None),
        "last_booked_at": _iso(getattr(group, "last_booked", None)),
        "created_by": get_user_display_name(group.created_by) if group.created_by_id else None,
        "created_at": _iso(group.created_at),
        "updated_at": _iso(group.updated_at),
    }


def _annotated_groups():
    active = _counted_members()
    return FacilityUserGroup.objects.select_related("equipment", "lab", "created_by").annotate(
        member_count=Count(
            "memberships",
            filter=active & (Q(memberships__booking_count__gt=0) | Q(memberships__added_manually=True)),
            distinct=True,
        ),
        supervisor_count=Count(
            "memberships",
            filter=active
            & Q(memberships__booking_count=0, memberships__supervised_booking_count__gt=0, memberships__added_manually=False),
            distinct=True,
        ),
        last_booked=Max("memberships__last_booked_at"),
    )


def _member_row(member: FacilityUserGroupMember, equipment_map: dict) -> dict:
    user = member.user
    department = user.department
    return {
        "user_id": user.pk,
        "name": get_user_display_name(user),
        "email": user.email or "",
        "mobile": user.phone_number or "",
        "emp_id": user.emp_id or "",
        "user_type": user.user_type or "",
        "user_type_label": user.get_user_type_display_label() or "",
        "audience": audience_of(user),
        "department_id": department.pk if department else None,
        "department_name": department.name if department else "",
        "department_type": department.department_type if department else "",
        "is_active": user.is_active,
        "role": role_of(member),
        "added_manually": member.added_manually,
        "booking_count": member.booking_count,
        "supervised_booking_count": member.supervised_booking_count,
        "first_booked_at": _iso(member.first_booked_at),
        "last_booked_at": _iso(member.last_booked_at),
        "last_supervised_at": _iso(member.last_supervised_at),
        "equipment": equipment_map.get(user.pk, []),
    }


MEMBER_ORDERING = {
    "name": ("user__name", "user__email"),
    "-name": ("-user__name", "-user__email"),
    "last_booked": ("last_booked_at", "user__name"),
    "-last_booked": ("-last_booked_at", "user__name"),
    "bookings": ("booking_count", "user__name"),
    "-bookings": ("-booking_count", "user__name"),
    "department": ("user__department__name", "user__name"),
    "-department": ("-user__department__name", "user__name"),
}


def _int(value, default: int, *, low: int = 1, high: int | None = None) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    number = max(low, number)
    return min(high, number) if high else number


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------


@fg_api(["GET", "POST"])
def groups(request):
    if request.method == "POST":
        data = request.data if isinstance(request.data, dict) else {}
        name = str(data.get("name") or "").strip()
        if not name:
            raise GroupError("Enter a group name.", field_name="name")
        if len(name) > 255:
            raise GroupError("Keep the name under 255 characters.", field_name="name")
        if FacilityUserGroup.objects.filter(kind=GroupKind.CUSTOM, name__iexact=name).exists():
            raise GroupError("A custom group with this name already exists.", field_name="name")
        group = FacilityUserGroup.objects.create(
            name=name,
            kind=GroupKind.CUSTOM,
            description=str(data.get("description") or "").strip()[:2000],
            created_by=request.user,
        )
        return Response(_group_row(_annotated_groups().get(pk=group.pk)), status=201)

    params = request.query_params
    qs = _annotated_groups()
    kind = params.get("kind")
    if kind:
        if kind not in GroupKind.values:
            raise GroupError("Unknown group kind.", field_name="kind")
        qs = qs.filter(kind=kind)
    if params.get("include_archived") not in {"1", "true"}:
        qs = qs.filter(is_archived=False)
    q = (params.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(equipment__code__icontains=q) | Q(description__icontains=q))
    kind_order = {k: i for i, k in enumerate(GroupKind.values)}
    rows = sorted(qs, key=lambda g: (kind_order.get(g.kind, 99), g.name.lower()))
    return Response(
        {
            "results": [_group_row(g) for g in rows],
            "kinds": [{"value": k.value, "label": str(k.label)} for k in GroupKind],
        }
    )


@fg_api(["GET", "PATCH", "DELETE"])
def group_detail(request, group_id: int):
    group = _get_group(group_id)
    if request.method == "DELETE":
        if group.is_automatic:
            raise GroupError("Automatic groups cannot be deleted; archive them instead.", status=409)
        if group.email_campaigns.exists():
            group.is_archived = True
            group.save(update_fields=["is_archived", "updated_at"])
            return Response({"archived": True, "detail": "The group was used in sent emails, so it was archived."})
        group.delete()
        return Response(status=204)
    if request.method == "PATCH":
        data = request.data if isinstance(request.data, dict) else {}
        fields = []
        if "name" in data:
            if group.is_automatic:
                raise GroupError("Automatic groups are named after their equipment, category or lab.", field_name="name")
            name = str(data.get("name") or "").strip()
            if not name or len(name) > 255:
                raise GroupError("Enter a group name under 255 characters.", field_name="name")
            if FacilityUserGroup.objects.filter(kind=GroupKind.CUSTOM, name__iexact=name).exclude(pk=group.pk).exists():
                raise GroupError("A custom group with this name already exists.", field_name="name")
            group.name = name
            fields.append("name")
        if "description" in data:
            group.description = str(data.get("description") or "").strip()[:2000]
            fields.append("description")
        if "is_archived" in data:
            group.is_archived = bool(data.get("is_archived"))
            fields.append("is_archived")
        if fields:
            group.save(update_fields=[*fields, "updated_at"])
    return Response(_group_row(_annotated_groups().get(pk=group.pk)))


@fg_api(["GET"])
def options(request):
    departments = Department.objects.order_by("department_type", "name").values("id", "name", "code", "department_type")
    return Response(
        {
            "departments": list(departments),
            "user_types": [{"value": code, "label": user_type_label(code)} for code in BOOKING_USER_TYPES],
            "all_user_types": [{"value": code, "label": str(label)} for code, label in UserType.get_choices()],
            "kinds": [{"value": k.value, "label": str(k.label)} for k in GroupKind],
            "cc_modes": [{"value": m.value, "label": str(m.label)} for m in CcMode],
            "limits": {
                "max_recipients": group_email.max_recipients(),
                "each_mode_max_recipients": group_email.EACH_MODE_MAX_RECIPIENTS,
                "max_cc": group_email.MAX_CC,
                "max_attachments": group_email.MAX_ATTACHMENTS,
                "max_attachment_mb": group_email.MAX_ATTACHMENT_BYTES // (1024 * 1024),
                "max_total_attachment_mb": group_email.MAX_TOTAL_ATTACHMENT_BYTES // (1024 * 1024),
            },
        }
    )


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


@fg_api(["GET"])
def members(request, group_id: int):
    group = _get_group(group_id)
    params = request.query_params
    f = AudienceFilters.from_data(params)
    qs = members_qs(group, f)
    ordering = MEMBER_ORDERING.get(params.get("ordering") or "-last_booked", MEMBER_ORDERING["-last_booked"])
    total = qs.count()
    page_size = _int(params.get("page_size"), 50, high=MAX_PAGE_SIZE)
    page = _int(params.get("page"), 1)
    start = (page - 1) * page_size
    page_rows = list(qs.order_by(*ordering)[start : start + page_size])
    equipment_map = equipment_booked(group, [m.user_id for m in page_rows], f)
    return Response(
        {
            "group": _group_row(_annotated_groups().get(pk=group.pk)),
            "count": total,
            "page": page,
            "page_size": page_size,
            "results": [_member_row(m, equipment_map) for m in page_rows],
            "filters": f.to_json(),
        }
    )


CSV_HEADER = [
    "Name", "Email", "Mobile", "Employee / Enrolment ID", "User type", "Internal / External",
    "Department / Organisation", "Department type", "Role in group", "Equipment booked", "Bookings",
    "Supervised bookings", "First booked", "Last booked", "Active",
]


def _local(value) -> str:
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M") if value else ""


@fg_api(["GET"])
def members_export(request, group_id: int):
    group = _get_group(group_id)
    f = AudienceFilters.from_data(request.query_params)
    rows = list(members_qs(group, f).order_by("user__department__name", "user__name"))
    equipment_map = equipment_booked(group, [m.user_id for m in rows], f)
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_HEADER)
    for member in rows:
        row = _member_row(member, equipment_map)
        writer.writerow(
            [
                row["name"], row["email"], row["mobile"], row["emp_id"], row["user_type_label"],
                row["audience"].title(), row["department_name"], row["department_type"].title(), row["role"].title(),
                "; ".join(f"{e['name']} ({e['count']})" for e in row["equipment"]),
                row["booking_count"], row["supervised_booking_count"], _local(member.first_booked_at),
                _local(member.last_booked_at), "Yes" if row["is_active"] else "No",
            ]
        )
    filename = f"{slugify(group.name)[:60] or 'group'}-members-{timezone.localdate().isoformat()}.csv"
    response = HttpResponse("\ufeff" + buffer.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@fg_api(["GET"])
def departments(request, group_id: int):
    group = _get_group(group_id)
    f = AudienceFilters.from_data(request.query_params)
    return Response(department_breakdown(members_qs(group, f)))


def _require_custom(group: FacilityUserGroup) -> None:
    if group.is_automatic:
        raise GroupError(
            "Members of automatic groups come from bookings; create a custom group to add or remove people.",
            status=409,
        )


@fg_api(["POST"])
def members_add(request, group_id: int):
    group = _get_group(group_id)
    _require_custom(group)
    data = request.data if isinstance(request.data, dict) else {}
    user_ids = data.get("user_ids")
    if user_ids:
        try:
            ids = {int(v) for v in user_ids}
        except (TypeError, ValueError) as exc:
            raise GroupError("Users must be ids.", field_name="user_ids") from exc
        candidates = set(User.objects.filter(pk__in=ids).values_list("pk", flat=True))
    else:
        f = AudienceFilters.from_data(data.get("filters") or {})
        source_ids = data.get("source_group_ids") or []
        if source_ids:
            sources = list(FacilityUserGroup.objects.filter(pk__in=[int(v) for v in source_ids]))
            if not sources:
                raise GroupError("Source groups not found.", status=404, field_name="source_group_ids")
            candidates = set()
            for source in sources:
                candidates |= set(members_qs(source, f).values_list("user_id", flat=True))
        else:
            if not (f.department_ids or f.user_types or f.audience or f.search):
                raise GroupError("Choose at least one filter (department, user type, internal / external or search).")
            candidates = set(User.objects.filter(user_q(f)).values_list("pk", flat=True))
    if len(candidates) > MAX_ADD:
        raise GroupError(f"{len(candidates)} people match; add at most {MAX_ADD} at a time.")
    existing = set(FacilityUserGroupMember.objects.filter(group=group, user_id__in=candidates).values_list("user_id", flat=True))
    new_ids = sorted(candidates - existing)
    FacilityUserGroupMember.objects.bulk_create(
        [FacilityUserGroupMember(group=group, user_id=pk, added_manually=True, added_by=request.user) for pk in new_ids],
        batch_size=500,
        ignore_conflicts=True,
    )
    FacilityUserGroupMember.objects.filter(group=group, user_id__in=existing, added_manually=False).update(added_manually=True)
    group.save(update_fields=["updated_at"])
    return Response({"added": len(new_ids), "already_members": len(existing)})


@fg_api(["POST"])
def members_remove(request, group_id: int):
    group = _get_group(group_id)
    _require_custom(group)
    data = request.data if isinstance(request.data, dict) else {}
    try:
        ids = [int(v) for v in data.get("user_ids") or []]
    except (TypeError, ValueError) as exc:
        raise GroupError("Users must be ids.", field_name="user_ids") from exc
    if not ids:
        raise GroupError("Select people to remove.", field_name="user_ids")
    removed, _ = FacilityUserGroupMember.objects.filter(group=group, user_id__in=ids).delete()
    group.save(update_fields=["updated_at"])
    return Response({"removed": removed})


@fg_api(["GET"])
def user_search(request):
    q = (request.query_params.get("q") or "").strip()
    if len(q) < 2:
        return Response({"results": []})
    users = (
        User.objects.filter(
            Q(name__icontains=q) | Q(email__icontains=q) | Q(emp_id__icontains=q) | Q(phone_number__icontains=q)
        )
        .select_related("department")
        .order_by("name")[:20]
    )
    return Response(
        {
            "results": [
                {
                    "id": u.pk,
                    "name": get_user_display_name(u),
                    "email": u.email,
                    "user_type_label": u.get_user_type_display_label() or "",
                    "department_name": u.department.name if u.department else "",
                    "is_active": u.is_active,
                }
                for u in users
            ]
        }
    )


# ---------------------------------------------------------------------------
# Group email
# ---------------------------------------------------------------------------


def _payload(request) -> tuple[dict, list]:
    content_type = (request.content_type or "").lower()
    if content_type.startswith("multipart/") or content_type.startswith("application/x-www-form-urlencoded"):
        raw = request.data.get("payload") or "{}"
        try:
            data = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise GroupError("The form could not be read.", field_name="payload") from exc
        files = request.FILES.getlist("attachments")
    else:
        data = request.data if isinstance(request.data, dict) else {}
        files = []
    if not isinstance(data, dict):
        raise GroupError("The form could not be read.", field_name="payload")
    return data, files


@fg_api(["POST"])
def email_preview(request):
    data = request.data if isinstance(request.data, dict) else {}
    groups_ = group_email.load_groups(group_email.parse_group_ids(data.get("group_ids")))
    f = AudienceFilters.from_data(data.get("filters") or {})
    preview = group_email.resolve_recipients(groups_, f)
    by_department: dict[tuple, dict] = {}
    internal = external = 0
    for row in preview.recipients:
        key = (row["department_id"], row["department_name"])
        entry = by_department.setdefault(
            key,
            {"department_id": row["department_id"] or 0, "department_name": row["department_name"] or "No department", "total": 0, "internal": 0, "external": 0},
        )
        entry["total"] += 1
        entry[row["audience"]] += 1
        if row["audience"] == "external":
            external += 1
        else:
            internal += 1
    sample_size = _int(data.get("sample_size"), 200, high=1000)
    return Response(
        {
            "total": preview.total,
            "without_email": preview.without_email,
            "internal": internal,
            "external": external,
            "departments": sorted(by_department.values(), key=lambda d: (-d["total"], d["department_name"].lower())),
            "recipients": preview.recipients[:sample_size],
            "groups": [{"id": g.pk, "name": g.name, "kind": g.kind} for g in groups_],
            "max_recipients": group_email.max_recipients(),
        }
    )


@fg_api(["POST"])
def email_render(request):
    data = request.data if isinstance(request.data, dict) else {}
    draft = group_email.parse_draft(data, require_body=False)
    _, _, html = group_email.render(draft, name="Recipient Name", email="recipient@example.com", department="Department")
    return Response({"subject": draft.subject, "html": html})


@fg_api(["POST"], multipart=True)
def email_test(request):
    data, files = _payload(request)
    sent_to = group_email.send_test(request.user, data, files)
    return Response({"sent_to": sent_to})


@fg_api(["POST"], multipart=True)
def email_send(request):
    data, files = _payload(request)
    campaign, created = group_email.create_campaign(request.user, data, files)
    return Response({"campaign": _campaign_row(campaign), "created": created}, status=201 if created else 200)


def _campaign_row(c: GroupEmailCampaign) -> dict:
    return {
        "id": c.pk,
        "subject": c.subject,
        "group_names": c.group_names,
        "status": c.status,
        "status_label": str(CampaignStatus(c.status).label),
        "total_recipients": c.total_recipients,
        "sent_count": c.sent_count,
        "failed_count": c.failed_count,
        "skipped_count": c.skipped_count,
        "cc": c.cc,
        "bcc": c.bcc,
        "cc_mode": c.cc_mode,
        "reply_to": c.reply_to,
        "summary_sent_at": _iso(c.summary_sent_at),
        "last_error": c.last_error,
        "created_by": get_user_display_name(c.created_by) if c.created_by_id else None,
        "created_at": _iso(c.created_at),
        "started_at": _iso(c.started_at),
        "finished_at": _iso(c.finished_at),
    }


@fg_api(["GET"])
def campaigns(request):
    params = request.query_params
    qs = GroupEmailCampaign.objects.select_related("created_by").order_by("-created_at")
    status = params.get("status")
    if status:
        qs = qs.filter(status=status)
    q = (params.get("q") or "").strip()
    if q:
        qs = qs.filter(subject__icontains=q)
    total = qs.count()
    page_size = _int(params.get("page_size"), 25, high=200)
    page = _int(params.get("page"), 1)
    start = (page - 1) * page_size
    return Response(
        {"count": total, "page": page, "page_size": page_size, "results": [_campaign_row(c) for c in qs[start : start + page_size]]}
    )


def _get_campaign(campaign_id: int) -> GroupEmailCampaign:
    campaign = GroupEmailCampaign.objects.select_related("created_by").filter(pk=campaign_id).first()
    if campaign is None:
        raise GroupError("Email not found.", status=404)
    return campaign


@fg_api(["GET"])
def campaign_detail(request, campaign_id: int):
    campaign = _get_campaign(campaign_id)
    params = request.query_params
    recipients = campaign.recipients.order_by("pk")
    status = params.get("status")
    if status:
        recipients = recipients.filter(status=status)
    q = (params.get("q") or "").strip()
    if q:
        recipients = recipients.filter(Q(email__icontains=q) | Q(name__icontains=q))
    total = recipients.count()
    page_size = _int(params.get("page_size"), 100, high=500)
    page = _int(params.get("page"), 1)
    start = (page - 1) * page_size
    counts = defaultdict(int, campaign.recipients.values_list("status").annotate(n=Count("pk")).order_by())
    row = _campaign_row(campaign)
    row.update(
        {
            "body_html": campaign.body_html,
            "filters": campaign.filters,
            "groups": [{"id": g.pk, "name": g.name} for g in campaign.groups.all()],
            "attachments": [
                {"id": a.pk, "filename": a.filename, "size": a.size, "content_type": a.content_type}
                for a in campaign.attachments.all()
            ],
            "status_counts": {s.value: counts[s.value] for s in RecipientStatus},
            "recipients": {
                "count": total,
                "page": page,
                "page_size": page_size,
                "results": [
                    {
                        "id": r.pk,
                        "name": r.name,
                        "email": r.email,
                        "department_name": r.department_name,
                        "status": r.status,
                        "error": r.error,
                        "attempts": r.attempts,
                        "sent_at": _iso(r.sent_at),
                    }
                    for r in recipients[start : start + page_size]
                ],
            },
        }
    )
    return Response(row)


@fg_api(["POST"])
def campaign_resume(request, campaign_id: int):
    campaign = _get_campaign(campaign_id)
    data = request.data if isinstance(request.data, dict) else {}
    campaign = group_email.resume(campaign, retry_failed=bool(data.get("retry_failed", True)))
    return Response(_campaign_row(campaign))


@fg_api(["POST"])
def campaign_cancel(request, campaign_id: int):
    campaign = group_email.cancel(_get_campaign(campaign_id))
    return Response(_campaign_row(campaign))

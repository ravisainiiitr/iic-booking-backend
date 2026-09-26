"""
Research Groups API (mounted under /api/v1/my-research/groups/ and siblings).

Every endpoint checks: groups feature flags -> IITR eligibility -> group role. Groups, activities,
update requests and attachments the caller has no role in answer 404 so ids cannot be probed.
Owners and faculty managers manage a group; members only see and update their own work.
Group roles never grant workspace access: linked workspaces are shown by name and owner only, and
opening one still goes through the existing workspace authorization.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Max, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.equipment.models import Booking, Equipment, EquipmentPublicationClaim, EquipmentPublicationClaimStatus

from . import file_policy, storage
from .access import NOT_ELIGIBLE_CODE, NOT_ELIGIBLE_MESSAGE, eligible_users, is_eligible
from .group_access import (
    GROUPS_DISABLED_CODE,
    GroupAccess,
    can_create_group,
    groups_enabled,
    is_group_faculty,
    managed_group_ids,
    resolve_group_access,
    visible_group_ids,
)
from .group_models import (
    AttachmentStatus,
    GroupActivityPriority,
    GroupActivityStatus,
    GroupEventAction,
    GroupMemberStatus,
    GroupMemberType,
    GroupRole,
    GroupStatus,
    ResearchGroup,
    ResearchGroupActivity,
    ResearchGroupActivityAssignee,
    ResearchGroupCategory,
    ResearchGroupEvent,
    ResearchGroupMember,
    ResearchGroupPublication,
    ResearchGroupWorkspace,
    ResearchUpdate,
    ResearchUpdateAttachment,
    ResearchUpdateRequest,
    UpdateRecurrence,
    UpdateRequestStatus,
)
from .group_services import (
    ASSIGNEE_STATUSES,
    OPEN_ACTIVITY_STATUSES,
    OPEN_REQUEST_STATUSES,
    activity_queryset,
    annotate_member_workload,
    build_attachment_key,
    group_counts,
    mark_attachment_failed,
    member_work_counts,
    notify_activity_assigned,
    notify_member_added,
    notify_member_removed,
    notify_update_requested,
    notify_update_reviewed,
    notify_update_submitted,
    public_user,
    record_event,
    request_queryset,
    serialize_activity,
    serialize_attachment,
    serialize_booking_ref,
    serialize_category,
    serialize_event,
    serialize_group_card,
    serialize_group_publications,
    serialize_linked_workspace,
    serialize_member,
    serialize_request,
    today,
    viewer_workspace_ids,
    visible_events,
)
from .models import ResearchWorkspace, WorkspaceStatus
from .services import serialize_publication

logger = logging.getLogger(__name__)

MAX_TEXT = 5000
MAX_BULK = 50


# ---------------------------------------------------------------- helpers


def _error(message: str, http_status: int, code: str | None = None, **extra) -> Response:
    payload = {"error": message, **extra}
    if code:
        payload["code"] = code
    return Response(payload, status=http_status)


def _not_found(what: str) -> Response:
    return _error(f"{what} not found.", status.HTTP_404_NOT_FOUND, "not_found")


def _gate(request) -> Response | None:
    if not groups_enabled():
        return _error("Research Groups are not available.", status.HTTP_404_NOT_FOUND, GROUPS_DISABLED_CODE)
    if not is_eligible(request.user):
        return _error(NOT_ELIGIBLE_MESSAGE, status.HTTP_403_FORBIDDEN, NOT_ELIGIBLE_CODE)
    return None


def _forbidden_manage() -> Response:
    return _error("Only the group's faculty can do this.", status.HTTP_403_FORBIDDEN, "group_manager_only")


def _archived() -> Response:
    return _error("This research group is archived and is read-only.", status.HTTP_409_CONFLICT, "group_archived")


def _group(request, group_id, *, manage: bool = False, owner: bool = False):
    """(access, None) or (None, error). Non-members get 404."""
    denied = _gate(request)
    if denied:
        return None, denied
    access = resolve_group_access(request.user, group_id)
    if access is None:
        return None, _not_found("Research group")
    if owner and not access.is_owner:
        return None, _error("Only the group owner can do this.", status.HTTP_403_FORBIDDEN, "group_owner_only")
    if (manage or owner) and not access.is_manager:
        return None, _forbidden_manage()
    if (manage or owner) and access.group.is_archived:
        return None, _archived()
    return access, None


def _text(raw, limit: int, *, multiline: bool = False) -> str:
    return file_policy.strip_control_chars(raw, multiline=multiline).strip()[:limit]


def _parse_date(raw):
    """date, None (empty) or False (invalid)."""
    if raw in (None, ""):
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return False


def _parse_percent(raw):
    if raw in (None, ""):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return False
    return value if 0 <= value <= 100 else False


def _int_or_none(raw):
    return int(raw) if raw is not None and str(raw).isdigit() else None


def _page(request, default: int = 30, maximum: int = 100) -> tuple[int, int]:
    try:
        page = max(int(request.query_params.get("page", 1)), 1)
    except (TypeError, ValueError):
        page = 1
    try:
        size = min(max(int(request.query_params.get("page_size", default)), 1), maximum)
    except (TypeError, ValueError):
        size = default
    return page, size


def _active_member_user_ids(group: ResearchGroup) -> set:
    ids = set(
        ResearchGroupMember.objects.filter(group=group, status=GroupMemberStatus.ACTIVE).values_list("user_id", flat=True)
    )
    ids.add(group.owner_id)
    return ids


def _group_permissions(access: GroupAccess) -> dict:
    return {
        "can_manage": access.can_manage,
        "is_owner": access.is_owner,
        "can_archive": access.is_owner and not access.group.is_archived,
        "can_manage_managers": access.is_owner and not access.group.is_archived,
    }


def _group_card(access: GroupAccess) -> dict:
    counts = group_counts([access.group.pk])[access.group.pk]
    return serialize_group_card(access.group, access.role, counts, access.membership)


# ---------------------------------------------------------------- home / groups


def _needs_attention(user, managed_ids) -> dict:
    active_ids = list(ResearchGroup.objects.filter(pk__in=managed_ids, status=GroupStatus.ACTIVE).values_list("pk", flat=True))
    counts = group_counts(active_ids)
    totals = {k: sum(c[k] for c in counts.values()) for k in
              ("pending_updates", "overdue_updates", "awaiting_review", "activities_due_this_week")}
    now_date = today()
    requests = list(
        request_queryset()
        .filter(group_id__in=active_ids)
        .filter(Q(status__in=OPEN_REQUEST_STATUSES) | Q(status=UpdateRequestStatus.SUBMITTED))
        .order_by("due_date", "requested_at")[:12]
    )
    due_activities = list(
        activity_queryset()
        .filter(group_id__in=active_ids, status__in=OPEN_ACTIVITY_STATUSES, due_date__lte=now_date + timedelta(days=7))
        .order_by("due_date")[:8]
    )
    ws_ids = viewer_workspace_ids(user)
    return {
        **totals,
        "update_requests": [serialize_request(r, user, can_manage=True) for r in requests],
        "activities_due": [serialize_activity(a, user, can_manage=True, viewer_workspace_ids=ws_ids) for a in due_activities],
    }


def _my_work(user, group_ids) -> dict:
    ws_ids = viewer_workspace_ids(user)
    activities = (
        activity_queryset()
        .filter(
            group_id__in=group_ids,
            assignees__user=user,
            assignees__removed_at__isnull=True,
            status__in=OPEN_ACTIVITY_STATUSES,
        )
        .distinct()
        .order_by("due_date", "-created_at")[:30]
    )
    requests = (
        request_queryset()
        .filter(group_id__in=group_ids, assigned_to=user, status__in=OPEN_REQUEST_STATUSES)
        .order_by("due_date", "requested_at")[:30]
    )
    return {
        "activities": [serialize_activity(a, user, can_manage=False, viewer_workspace_ids=ws_ids) for a in activities],
        "update_requests": [serialize_request(r, user, can_manage=False) for r in requests],
    }


def _group_cards(user):
    visible = visible_group_ids(user)
    managed = managed_group_ids(user) & visible
    groups = list(ResearchGroup.objects.filter(pk__in=visible).select_related("owner", "owner__department"))
    memberships = {
        m.group_id: m
        for m in ResearchGroupMember.objects.select_related("category").filter(
            user=user, status=GroupMemberStatus.ACTIVE, group_id__in=visible
        )
    }
    counts = group_counts(visible)
    member_ids = [g.pk for g in groups if g.pk not in managed]
    work = member_work_counts(user, member_ids)
    managed_cards, member_cards = [], []
    for group in sorted(groups, key=lambda g: (g.status != GroupStatus.ACTIVE, g.name.lower())):
        if group.pk in managed:
            role = GroupRole.OWNER if group.owner_id == user.pk else GroupRole.MANAGER
            managed_cards.append(serialize_group_card(group, role, counts[group.pk], memberships.get(group.pk)))
        else:
            membership = memberships.get(group.pk)
            card = serialize_group_card(group, membership.role if membership else GroupRole.MEMBER, counts[group.pk], membership)
            card["counts"].update(work.get(group.pk, {}))
            member_cards.append(card)
    return visible, managed, managed_cards, member_cards


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def groups_home(request):
    denied = _gate(request)
    if denied:
        return denied
    user = request.user
    visible, managed, managed_cards, member_cards = _group_cards(user)
    events = (
        visible_events(visible, user, managed)
        .select_related("group", "actor", "actor__department", "subject_user", "subject_user__department")
        .order_by("-created_at")[:10]
    )
    return Response(
        {
            "can_create": can_create_group(user),
            "is_faculty": is_group_faculty(user),
            "managed_groups": managed_cards,
            "member_groups": member_cards,
            "needs_attention": _needs_attention(user, managed) if managed else None,
            "my_work": _my_work(user, [c["id"] for c in member_cards] + [c["id"] for c in managed_cards]),
            "recent_events": [serialize_event(e, include_group=True) for e in events],
        }
    )


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def groups_collection(request):
    denied = _gate(request)
    if denied:
        return denied
    user = request.user
    if request.method == "GET":
        _, _, managed_cards, member_cards = _group_cards(user)
        return Response({"managed_groups": managed_cards, "member_groups": member_cards})

    if not can_create_group(user):
        return _error(
            "Only IIT Roorkee faculty can create research groups.",
            status.HTTP_403_FORBIDDEN,
            "my_research_groups_faculty_only",
        )
    name = _text(request.data.get("name"), 1000)
    if not name:
        return _error("Group name is required.", status.HTTP_400_BAD_REQUEST, field_errors={"name": "Required"})
    if len(name) > 200:
        return _error("Group name can be at most 200 characters.", status.HTTP_400_BAD_REQUEST)
    short_code = _text(request.data.get("short_code"), 20)
    description = _text(request.data.get("description"), MAX_TEXT, multiline=True)
    group = ResearchGroup.objects.create(owner=user, name=name, short_code=short_code, description=description)
    record_event(group, user, GroupEventAction.GROUP_CREATED, target_type="group", target_id=group.pk, target_label=name)
    access = resolve_group_access(user, group.pk)
    return Response({**_group_card(access), "permissions": _group_permissions(access)}, status=status.HTTP_201_CREATED)


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def group_detail(request, group_id):
    access, error = _group(request, group_id, manage=request.method == "PATCH")
    if error:
        return error
    group, user = access.group, request.user
    if request.method == "PATCH":
        changes = {}
        if "name" in request.data:
            name = _text(request.data.get("name"), 1000)
            if not name or len(name) > 200:
                return _error("Group name is required (at most 200 characters).", status.HTTP_400_BAD_REQUEST)
            changes["name"] = name
        if "short_code" in request.data:
            changes["short_code"] = _text(request.data.get("short_code"), 20)
        if "description" in request.data:
            changes["description"] = _text(request.data.get("description"), MAX_TEXT, multiline=True)
        if changes:
            for field, value in changes.items():
                setattr(group, field, value)
            group.save(update_fields=[*changes.keys(), "updated_at"])
            record_event(group, user, GroupEventAction.GROUP_UPDATED, target_type="group", target_id=group.pk,
                         target_label=group.name, details={"fields": sorted(changes)})

    categories = group.categories.all() if access.is_manager else group.categories.filter(active=True)
    managed = {group.pk} if access.is_manager else set()
    events = (
        visible_events([group.pk], user, managed)
        .select_related("actor", "actor__department", "subject_user", "subject_user__department")
        .order_by("-created_at")[:10]
    )
    data = {
        **_group_card(access),
        "owner_details": public_user(group.owner),
        "permissions": _group_permissions(access),
        "categories": [serialize_category(c) for c in categories],
        "recent_events": [serialize_event(e) for e in events],
        "needs_attention": _needs_attention(user, {group.pk}) if access.is_manager and not group.is_archived else None,
    }
    if not access.is_manager:
        data["my_work"] = _my_work(user, [group.pk])
    return Response(data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def group_archive(request, group_id):
    access, error = _group(request, group_id, owner=True)
    if error:
        return error
    group = access.group
    group.status = GroupStatus.ARCHIVED
    group.archived_at = timezone.now()
    group.save(update_fields=["status", "archived_at", "updated_at"])
    record_event(group, request.user, GroupEventAction.GROUP_ARCHIVED, target_type="group", target_id=group.pk,
                 target_label=group.name)
    return Response({"status": group.status, "archived_at": group.archived_at})


# ---------------------------------------------------------------- members


def _category_for(group, raw):
    """(category_or_None, error). Missing/empty clears the category."""
    if raw in (None, ""):
        return None, None
    category = ResearchGroupCategory.objects.filter(group=group, pk=_int_or_none(raw) or -1, active=True).first()
    if category is None:
        return None, _not_found("Category")
    return category, None


def _member_type(raw):
    value = str(raw or GroupMemberType.OTHER).upper()
    return value if value in GroupMemberType.values else None


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def members_collection(request, group_id):
    access, error = _group(request, group_id, manage=request.method == "POST")
    if error:
        return error
    group, user = access.group, request.user
    if request.method == "GET":
        qs = ResearchGroupMember.objects.filter(group=group).select_related("user", "user__department", "category")
        if not (access.is_manager and request.query_params.get("include_left") == "1"):
            qs = qs.filter(status=GroupMemberStatus.ACTIVE)
        if access.is_manager:
            qs = annotate_member_workload(qs)
        qs = qs.order_by("status", "category__display_order", "user__name")
        return Response(
            {
                "owner": public_user(group.owner),
                "results": [serialize_member(m, for_manager=access.is_manager) for m in qs],
            }
        )

    if request.data.get("confirm") is not True:
        return _error("Confirm before adding a member.", status.HTTP_400_BAD_REQUEST, "confirmation_required")
    raw_user = request.data.get("user_id")
    target = eligible_users().exclude(pk=group.owner_id).filter(pk=_int_or_none(raw_user) or -1).first()
    if target is None:
        return _error(
            "Only IIT Roorkee students and faculty can be added to a research group.",
            status.HTTP_400_BAD_REQUEST,
            "member_not_eligible",
        )
    member_type = _member_type(request.data.get("member_type"))
    if member_type is None:
        return _error("Invalid member type.", status.HTTP_400_BAD_REQUEST)
    role = str(request.data.get("role") or GroupRole.MEMBER).upper()
    if role not in (GroupRole.MEMBER, GroupRole.MANAGER):
        return _error("Invalid role.", status.HTTP_400_BAD_REQUEST)
    if role == GroupRole.MANAGER and not (access.is_owner and is_group_faculty(target)):
        return _error("Only the owner can add managers, and managers must be IITR faculty.", status.HTTP_400_BAD_REQUEST)
    category, error = _category_for(group, request.data.get("category_id"))
    if error:
        return error
    try:
        with transaction.atomic():
            member = ResearchGroupMember.objects.create(
                group=group, user=target, role=role, member_type=member_type, category=category, added_by=user
            )
    except IntegrityError:
        return _error("This person is already a member of the group.", status.HTTP_409_CONFLICT, "already_member")
    record_event(group, user, GroupEventAction.MEMBER_ADDED, subject_user=target, target_type="member",
                 target_id=member.pk, target_label=target.name or target.email,
                 details={"member_type": member_type, "category": category.name if category else None})
    member = ResearchGroupMember.objects.select_related("user", "user__department", "category", "group").get(pk=member.pk)
    transaction.on_commit(lambda: notify_member_added(member, user))
    return Response(serialize_member(member, for_manager=True), status=status.HTTP_201_CREATED)


@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def member_detail(request, group_id, member_id):
    access, error = _group(request, group_id, manage=request.method != "GET")
    if error:
        return error
    if not access.is_manager:
        return _forbidden_manage()
    group, user = access.group, request.user
    member = (
        ResearchGroupMember.objects.select_related("user", "user__department", "category", "group")
        .filter(pk=member_id, group=group)
        .first()
    )
    if member is None:
        return _not_found("Member")

    if request.method == "DELETE":
        if member.status != GroupMemberStatus.ACTIVE:
            return _not_found("Member")
        if member.role == GroupRole.MANAGER and not access.is_owner:
            return _error("Only the owner can remove a manager.", status.HTTP_403_FORBIDDEN, "group_owner_only")
        now = timezone.now()
        with transaction.atomic():
            member.status = GroupMemberStatus.LEFT
            member.left_at = now
            member.removed_by = user
            member.save(update_fields=["status", "left_at", "removed_by"])
            ResearchGroupActivityAssignee.objects.filter(
                activity__group=group, user=member.user, removed_at__isnull=True
            ).update(removed_at=now)
            ResearchUpdateRequest.objects.filter(
                group=group, assigned_to=member.user, status__in=OPEN_REQUEST_STATUSES
            ).update(status=UpdateRequestStatus.CANCELLED, cancelled_at=now)
            record_event(group, user, GroupEventAction.MEMBER_REMOVED, subject_user=member.user, target_type="member",
                         target_id=member.pk, target_label=member.user.name or member.user.email)
        transaction.on_commit(lambda: notify_member_removed(member))
        return Response({"removed": True})

    if request.method == "PATCH":
        if member.status != GroupMemberStatus.ACTIVE:
            return _error("This person is no longer a member.", status.HTTP_409_CONFLICT)
        changes = {}
        if "member_type" in request.data:
            member_type = _member_type(request.data.get("member_type"))
            if member_type is None:
                return _error("Invalid member type.", status.HTTP_400_BAD_REQUEST)
            changes["member_type"] = member_type
        if "category_id" in request.data:
            category, error = _category_for(group, request.data.get("category_id"))
            if error:
                return error
            changes["category"] = category
        if "role" in request.data:
            role = str(request.data.get("role") or "").upper()
            if role not in (GroupRole.MEMBER, GroupRole.MANAGER):
                return _error("Invalid role.", status.HTTP_400_BAD_REQUEST)
            if role != member.role:
                if not access.is_owner:
                    return _error("Only the owner can change roles.", status.HTTP_403_FORBIDDEN, "group_owner_only")
                if role == GroupRole.MANAGER and not is_group_faculty(member.user):
                    return _error("Managers must be IITR faculty.", status.HTTP_400_BAD_REQUEST)
                changes["role"] = role
        if changes:
            for field, value in changes.items():
                setattr(member, field, value)
            member.save(update_fields=list(changes.keys()))
            record_event(
                group, user, GroupEventAction.MEMBER_UPDATED, subject_user=member.user, target_type="member",
                target_id=member.pk, target_label=member.user.name or member.user.email,
                details={"member_type": member.member_type, "category": member.category.name if member.category else None,
                         "role": member.role},
            )

    member = annotate_member_workload(ResearchGroupMember.objects.filter(pk=member.pk)).select_related(
        "user", "user__department", "category"
    ).get()
    ws_ids = viewer_workspace_ids(user)
    assignments = (
        activity_queryset()
        .filter(group=group, assignees__user=member.user)
        .distinct()
        .order_by("-updated_at")[:30]
    )
    requests = request_queryset().filter(group=group, assigned_to=member.user).order_by("-requested_at")[:15]
    related_ws = ResearchWorkspace.objects.select_related("owner", "owner__department").filter(
        Q(group_links__group=group, owner=member.user)
        | Q(group_activities__group=group, group_activities__assignees__user=member.user,
            group_activities__assignees__removed_at__isnull=True)
    ).distinct()
    events = (
        ResearchGroupEvent.objects.filter(group=group, subject_user=member.user)
        .select_related("actor", "actor__department", "subject_user", "subject_user__department")
        .order_by("-created_at")[:15]
    )
    return Response(
        {
            **serialize_member(member, for_manager=True),
            "activities": [serialize_activity(a, user, can_manage=True, viewer_workspace_ids=ws_ids) for a in assignments],
            "update_requests": [serialize_request(r, user, can_manage=True) for r in requests],
            "workspaces": [serialize_linked_workspace(w, ws_ids) for w in related_ws],
            "recent_events": [serialize_event(e) for e in events],
        }
    )


# ---------------------------------------------------------------- categories


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def categories_collection(request, group_id):
    access, error = _group(request, group_id, manage=request.method == "POST")
    if error:
        return error
    group = access.group
    if request.method == "GET":
        qs = group.categories.all() if access.is_manager else group.categories.filter(active=True)
        return Response({"results": [serialize_category(c) for c in qs]})
    name = _text(request.data.get("name"), 200)
    if not name or len(name) > 120:
        return _error("Category name is required (at most 120 characters).", status.HTTP_400_BAD_REQUEST)
    if group.categories.filter(active=True, name__iexact=name).exists():
        return _error("An active category with this name already exists.", status.HTTP_409_CONFLICT, "category_exists")
    next_order = (group.categories.aggregate(m=Max("display_order"))["m"] or 0) + 1
    category = ResearchGroupCategory.objects.create(
        group=group, name=name, description=_text(request.data.get("description"), 500), display_order=next_order
    )
    record_event(group, request.user, GroupEventAction.CATEGORY_CREATED, target_type="category",
                 target_id=category.pk, target_label=name)
    return Response(serialize_category(category), status=status.HTTP_201_CREATED)


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def category_detail(request, group_id, category_id):
    access, error = _group(request, group_id, manage=True)
    if error:
        return error
    group = access.group
    category = ResearchGroupCategory.objects.filter(group=group, pk=category_id).first()
    if category is None:
        return _not_found("Category")
    changes = {}
    if "name" in request.data:
        name = _text(request.data.get("name"), 200)
        if not name or len(name) > 120:
            return _error("Category name is required (at most 120 characters).", status.HTTP_400_BAD_REQUEST)
        changes["name"] = name
    if "description" in request.data:
        changes["description"] = _text(request.data.get("description"), 500)
    if "active" in request.data:
        changes["active"] = bool(request.data.get("active"))
    if "display_order" in request.data:
        order = _int_or_none(request.data.get("display_order"))
        if order is None:
            return _error("Invalid display order.", status.HTTP_400_BAD_REQUEST)
        changes["display_order"] = order
    will_be_active = changes.get("active", category.active)
    final_name = changes.get("name", category.name)
    if will_be_active and group.categories.filter(active=True, name__iexact=final_name).exclude(pk=category.pk).exists():
        return _error("An active category with this name already exists.", status.HTTP_409_CONFLICT, "category_exists")
    if changes:
        for field, value in changes.items():
            setattr(category, field, value)
        category.save(update_fields=[*changes.keys(), "updated_at"])
        record_event(group, request.user, GroupEventAction.CATEGORY_UPDATED, target_type="category",
                     target_id=category.pk, target_label=category.name, details={"fields": sorted(changes)})
    return Response(serialize_category(category))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def categories_reorder(request, group_id):
    access, error = _group(request, group_id, manage=True)
    if error:
        return error
    raw = request.data.get("ids")
    if not isinstance(raw, list) or not raw or len(raw) > 200:
        return _error("Provide the category ids in the new order.", status.HTTP_400_BAD_REQUEST)
    ids = [_int_or_none(v) for v in raw]
    categories = {c.pk: c for c in access.group.categories.filter(pk__in=[i for i in ids if i])}
    if len(categories) != len(set(ids)) or None in ids:
        return _not_found("Category")
    with transaction.atomic():
        for index, cid in enumerate(ids, start=1):
            ResearchGroupCategory.objects.filter(pk=cid).update(display_order=index)
    return Response({"results": [serialize_category(c) for c in access.group.categories.all()]})


# ---------------------------------------------------------------- activities


def _linkable_workspace_qs(access: GroupAccess, user):
    """Workspaces the caller can open whose owner is the caller or a member of this group."""
    from .access import accessible_workspace_ids

    return ResearchWorkspace.objects.filter(
        pk__in=accessible_workspace_ids(user), owner_id__in=_active_member_user_ids(access.group) | {user.pk}
    )


def _linkable_booking_qs(access: GroupAccess, user):
    return Booking.objects.filter(user_id__in=_active_member_user_ids(access.group) | {user.pk})


def _resolve_links(access: GroupAccess, user, data) -> tuple[dict, Response | None]:
    """Validated workspace/equipment/booking links present in the payload."""
    links = {}
    if "workspace_id" in data:
        raw = data.get("workspace_id")
        if raw in (None, ""):
            links["workspace"] = None
        else:
            workspace = _linkable_workspace_qs(access, user).filter(pk=str(raw)).first() if _is_uuid(raw) else None
            if workspace is None:
                return {}, _not_found("Workspace")
            links["workspace"] = workspace
    if "equipment_id" in data:
        raw = data.get("equipment_id")
        if raw in (None, ""):
            links["equipment"] = None
        else:
            equipment = Equipment.objects.filter(equipment_id=_int_or_none(raw) or -1).first()
            if equipment is None:
                return {}, _not_found("Equipment")
            links["equipment"] = equipment
    if "booking_id" in data:
        raw = data.get("booking_id")
        if raw in (None, ""):
            links["booking"] = None
        else:
            booking = _linkable_booking_qs(access, user).select_related("equipment").filter(
                booking_id=_int_or_none(raw) or -1
            ).first()
            if booking is None:
                return {}, _not_found("Booking")
            links["booking"] = booking
    return links, None


def _is_uuid(raw) -> bool:
    import uuid

    try:
        uuid.UUID(str(raw))
        return True
    except (TypeError, ValueError):
        return False


def _assignee_users(access: GroupAccess, raw) -> tuple[list | None, Response | None]:
    if raw is None:
        return None, None
    if not isinstance(raw, list) or len(raw) > MAX_BULK:
        return None, _error("Assignees must be a list of group members.", status.HTTP_400_BAD_REQUEST)
    ordered = list(dict.fromkeys(_int_or_none(v) for v in raw))
    if None in ordered:
        return None, _error("Assignees must be a list of group members.", status.HTTP_400_BAD_REQUEST)
    allowed = {
        m.user_id: m.user
        for m in ResearchGroupMember.objects.filter(
            group=access.group, status=GroupMemberStatus.ACTIVE, user_id__in=ordered
        ).select_related("user")
    }
    users = [allowed[i] for i in ordered if i in allowed]
    if len(users) != len(ordered):
        return None, _error("Activities can only be assigned to active group members.", status.HTTP_400_BAD_REQUEST,
                            "assignee_not_member")
    return users, None


def _activity_fields(access: GroupAccess, data, *, creating: bool) -> tuple[dict, Response | None]:
    fields = {}
    if creating or "title" in data:
        title = _text(data.get("title"), 1000)
        if not title or len(title) > 250:
            return {}, _error("Activity title is required (at most 250 characters).", status.HTTP_400_BAD_REQUEST)
        fields["title"] = title
    if "description" in data:
        fields["description"] = _text(data.get("description"), MAX_TEXT, multiline=True)
    if "category_id" in data:
        category, error = _category_for(access.group, data.get("category_id"))
        if error:
            return {}, error
        fields["category"] = category
    if "status" in data:
        value = str(data.get("status") or "").upper()
        if value not in GroupActivityStatus.values:
            return {}, _error("Invalid status.", status.HTTP_400_BAD_REQUEST)
        fields["status"] = value
    if "priority" in data:
        value = str(data.get("priority") or "").upper()
        if value not in GroupActivityPriority.values:
            return {}, _error("Invalid priority.", status.HTTP_400_BAD_REQUEST)
        fields["priority"] = value
    if "progress_percent" in data:
        value = _parse_percent(data.get("progress_percent"))
        if value is False:
            return {}, _error("Progress must be between 0 and 100.", status.HTTP_400_BAD_REQUEST)
        fields["progress_percent"] = value or 0
    for key in ("start_date", "due_date"):
        if key in data:
            value = _parse_date(data.get(key))
            if value is False:
                return {}, _error("Invalid date.", status.HTTP_400_BAD_REQUEST)
            fields[key] = value
    return fields, None


MANAGER_ACTIVITY_KEYS = {
    "title", "description", "category_id", "status", "priority", "progress_percent", "start_date", "due_date",
    "workspace_id", "equipment_id", "booking_id", "assignee_user_ids",
}
ASSIGNEE_ACTIVITY_KEYS = {"my_status", "my_progress_percent", "my_note"}


def _sync_assignees(activity, users, actor) -> list:
    """Set the active assignee list; returns newly assigned users."""
    wanted = {u.pk: u for u in users}
    current = {a.user_id: a for a in activity.assignees.filter(removed_at__isnull=True)}
    now = timezone.now()
    added = []
    for user_id, assignment in current.items():
        if user_id not in wanted:
            assignment.removed_at = now
            assignment.save(update_fields=["removed_at", "updated_at"])
            record_event(activity.group, actor, GroupEventAction.ACTIVITY_UNASSIGNED, subject_user=assignment.user,
                         target_type="activity", target_id=activity.pk, target_label=activity.title)
    for user_id, user in wanted.items():
        if user_id not in current:
            ResearchGroupActivityAssignee.objects.create(activity=activity, user=user, assigned_by=actor)
            record_event(activity.group, actor, GroupEventAction.ACTIVITY_ASSIGNED, subject_user=user,
                         target_type="activity", target_id=activity.pk, target_label=activity.title)
            added.append(user)
    return added


def _serialize_activity_for(activity_id, user, access: GroupAccess):
    activity = activity_queryset().get(pk=activity_id)
    return serialize_activity(activity, user, can_manage=access.can_manage, viewer_workspace_ids=viewer_workspace_ids(user))


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def activities_collection(request, group_id):
    access, error = _group(request, group_id, manage=request.method == "POST")
    if error:
        return error
    group, user = access.group, request.user
    if request.method == "GET":
        qs = activity_queryset().filter(group=group)
        if not access.is_manager:
            qs = qs.filter(assignees__user=user, assignees__removed_at__isnull=True)
        state = request.query_params.get("state")
        if state == "open":
            qs = qs.filter(status__in=OPEN_ACTIVITY_STATUSES)
        elif state == "closed":
            qs = qs.exclude(status__in=OPEN_ACTIVITY_STATUSES)
        category = _int_or_none(request.query_params.get("category"))
        if category:
            qs = qs.filter(category_id=category)
        assignee = _int_or_none(request.query_params.get("assignee"))
        if assignee and access.is_manager:
            qs = qs.filter(assignees__user_id=assignee, assignees__removed_at__isnull=True)
        ws_ids = viewer_workspace_ids(user)
        items = qs.distinct().order_by("status", "due_date", "-created_at")[:200]
        return Response(
            {"results": [serialize_activity(a, user, can_manage=access.can_manage, viewer_workspace_ids=ws_ids) for a in items]}
        )

    fields, error = _activity_fields(access, request.data, creating=True)
    if error:
        return error
    links, error = _resolve_links(access, user, request.data)
    if error:
        return error
    assignees, error = _assignee_users(access, request.data.get("assignee_user_ids", []))
    if error:
        return error
    if fields.get("start_date") and fields.get("due_date") and fields["due_date"] < fields["start_date"]:
        return _error("The due date cannot be before the start date.", status.HTTP_400_BAD_REQUEST)
    with transaction.atomic():
        activity = ResearchGroupActivity.objects.create(group=group, created_by=user, **fields, **links)
        if activity.status == GroupActivityStatus.COMPLETED:
            activity.completed_at = timezone.now()
            activity.save(update_fields=["completed_at"])
        record_event(group, user, GroupEventAction.ACTIVITY_CREATED, target_type="activity", target_id=activity.pk,
                     target_label=activity.title)
        added = _sync_assignees(activity, assignees or [], user)
    for assignee in added:
        transaction.on_commit(lambda a=assignee: notify_activity_assigned(activity, a, user))
    return Response(_serialize_activity_for(activity.pk, user, access), status=status.HTTP_201_CREATED)


def _activity_access(request, activity_id, *, write: bool):
    denied = _gate(request)
    if denied:
        return None, None, denied
    activity = ResearchGroupActivity.objects.select_related("group").filter(pk=activity_id).first()
    if activity is None:
        return None, None, _not_found("Activity")
    access = resolve_group_access(request.user, activity.group_id)
    if access is None:
        return None, None, _not_found("Activity")
    is_assignee = activity.assignees.filter(user=request.user, removed_at__isnull=True).exists()
    if not access.is_manager and not is_assignee:
        return None, None, _not_found("Activity")
    if write and activity.group.is_archived:
        return None, None, _archived()
    return access, activity, None


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def activity_detail(request, activity_id):
    access, activity, error = _activity_access(request, activity_id, write=request.method == "PATCH")
    if error:
        return error
    user = request.user
    if request.method == "GET":
        return Response(_serialize_activity_for(activity.pk, user, access))

    keys = set(request.data.keys())
    manager_keys = keys & MANAGER_ACTIVITY_KEYS
    assignee_keys = keys & ASSIGNEE_ACTIVITY_KEYS
    if manager_keys and not access.can_manage:
        return _error("Only the group's faculty can change the activity details.", status.HTTP_403_FORBIDDEN,
                      "group_manager_only")
    assignment = activity.assignees.filter(user=user, removed_at__isnull=True).select_related("user").first()
    if assignee_keys and assignment is None:
        return _error("Only assigned members can update their progress.", status.HTTP_403_FORBIDDEN, "not_assignee")

    added = []
    with transaction.atomic():
        if manager_keys:
            fields, error = _activity_fields(access, request.data, creating=False)
            if error:
                return error
            links, error = _resolve_links(access, user, request.data)
            if error:
                return error
            start = fields.get("start_date", activity.start_date)
            due = fields.get("due_date", activity.due_date)
            if start and due and due < start:
                return _error("The due date cannot be before the start date.", status.HTTP_400_BAD_REQUEST)
            assignees = None
            if "assignee_user_ids" in request.data:
                assignees, error = _assignee_users(access, request.data.get("assignee_user_ids"))
                if error:
                    return error
            previous_status = activity.status
            changed = {**fields, **links}
            if "status" in fields and fields["status"] != previous_status:
                changed["completed_at"] = timezone.now() if fields["status"] == GroupActivityStatus.COMPLETED else None
            for field, value in changed.items():
                setattr(activity, field, value)
            if changed:
                activity.save()
                action = (
                    GroupEventAction.ACTIVITY_COMPLETED
                    if changed.get("status") == GroupActivityStatus.COMPLETED and previous_status != GroupActivityStatus.COMPLETED
                    else GroupEventAction.ACTIVITY_UPDATED
                )
                record_event(activity.group, user, action, target_type="activity", target_id=activity.pk,
                             target_label=activity.title, details={"fields": sorted(k for k in changed if k != "completed_at")})
            if assignees is not None:
                added = _sync_assignees(activity, assignees, user)

        if assignee_keys:
            update_fields = []
            if "my_status" in request.data:
                value = str(request.data.get("my_status") or "").upper()
                if value not in ASSIGNEE_STATUSES:
                    return _error("You can set your status to not started, in progress, waiting or submitted.",
                                  status.HTTP_400_BAD_REQUEST)
                assignment.status = value
                update_fields.append("status")
            if "my_progress_percent" in request.data:
                value = _parse_percent(request.data.get("my_progress_percent"))
                if value is False or value is None:
                    return _error("Progress must be between 0 and 100.", status.HTTP_400_BAD_REQUEST)
                assignment.progress_percent = value
                update_fields.append("progress_percent")
            if "my_note" in request.data:
                assignment.note = _text(request.data.get("my_note"), MAX_TEXT, multiline=True)
                update_fields.append("note")
            if update_fields:
                assignment.save(update_fields=[*update_fields, "updated_at"])
                record_event(activity.group, user, GroupEventAction.PROGRESS_UPDATED, subject_user=user,
                             target_type="activity", target_id=activity.pk, target_label=activity.title,
                             details={"status": assignment.status, "progress_percent": assignment.progress_percent})
    for assignee in added:
        transaction.on_commit(lambda a=assignee: notify_activity_assigned(activity, a, user))
    return Response(_serialize_activity_for(activity.pk, user, access))


# ---------------------------------------------------------------- update requests


def _filter_requests(qs, state: str | None):
    now_date = today()
    overdue_q = Q(status=UpdateRequestStatus.OVERDUE) | Q(status=UpdateRequestStatus.PENDING, due_date__lt=now_date)
    if state == "pending":
        return qs.filter(status=UpdateRequestStatus.PENDING).exclude(due_date__lt=now_date)
    if state == "overdue":
        return qs.filter(overdue_q)
    if state == "submitted":
        return qs.filter(status=UpdateRequestStatus.SUBMITTED)
    if state == "history":
        return qs.filter(status__in=(UpdateRequestStatus.REVIEWED, UpdateRequestStatus.CANCELLED))
    return qs


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def updates_list(request, group_id):
    access, error = _group(request, group_id)
    if error:
        return error
    user = request.user
    qs = request_queryset().filter(group=access.group)
    if not access.is_manager:
        qs = qs.filter(assigned_to=user)
    else:
        assignee = _int_or_none(request.query_params.get("assignee"))
        if assignee:
            qs = qs.filter(assigned_to_id=assignee)
    qs = _filter_requests(qs, request.query_params.get("state"))
    page, size = _page(request, default=50, maximum=200)
    total = qs.count()
    items = list(qs.order_by("due_date", "-requested_at")[(page - 1) * size : page * size])
    return Response(
        {
            "results": [serialize_request(r, user, can_manage=access.is_manager) for r in items],
            "pagination": {"page": page, "page_size": size, "total": total, "has_next": page * size < total},
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def update_requests_create(request, group_id):
    access, error = _group(request, group_id, manage=True)
    if error:
        return error
    group, user = access.group, request.user
    title = _text(request.data.get("title"), 1000)
    if not title or len(title) > 250:
        return _error("Title is required (at most 250 characters).", status.HTTP_400_BAD_REQUEST)
    instructions = _text(request.data.get("instructions"), MAX_TEXT, multiline=True)
    due = _parse_date(request.data.get("due_date"))
    if due is False:
        return _error("Invalid due date.", status.HTTP_400_BAD_REQUEST)
    if due and due < today():
        return _error("The due date cannot be in the past.", status.HTTP_400_BAD_REQUEST)
    recurrence = str(request.data.get("recurrence") or UpdateRecurrence.NONE).upper()
    if recurrence != UpdateRecurrence.NONE:
        return _error("Only one-time update requests are supported right now.", status.HTTP_400_BAD_REQUEST)
    raw_users = request.data.get("assigned_user_ids")
    if raw_users is None and request.data.get("assigned_to") is not None:
        raw_users = [request.data.get("assigned_to")]
    users, error = _assignee_users(access, raw_users or [])
    if error:
        return error
    if not users:
        return _error("Choose at least one group member.", status.HTTP_400_BAD_REQUEST)
    activity = None
    if request.data.get("activity_id") not in (None, ""):
        raw = request.data.get("activity_id")
        activity = ResearchGroupActivity.objects.filter(group=group, pk=str(raw)).first() if _is_uuid(raw) else None
        if activity is None:
            return _not_found("Activity")
    created = []
    with transaction.atomic():
        for member_user in users:
            req = ResearchUpdateRequest.objects.create(
                group=group, activity=activity, requested_by=user, assigned_to=member_user, title=title,
                instructions=instructions, due_date=due, recurrence=UpdateRecurrence.NONE,
            )
            record_event(group, user, GroupEventAction.UPDATE_REQUESTED, subject_user=member_user,
                         target_type="update_request", target_id=req.pk, target_label=title)
            created.append(req.pk)
    by_id = {r.pk: r for r in request_queryset().filter(pk__in=created)}
    fresh = [by_id[pk] for pk in created]
    for req in fresh:
        transaction.on_commit(lambda r=req: notify_update_requested(r))
    return Response({"results": [serialize_request(r, user, can_manage=True) for r in fresh]},
                    status=status.HTTP_201_CREATED)


def _request_access(request, request_id, *, write: bool = False):
    """(access, update_request, is_assignee, error). Only managers and the assignee may see a request."""
    denied = _gate(request)
    if denied:
        return None, None, False, denied
    req = request_queryset().filter(pk=request_id).first()
    if req is None:
        return None, None, False, _not_found("Update request")
    access = resolve_group_access(request.user, req.group_id)
    if access is None:
        return None, None, False, _not_found("Update request")
    is_assignee = req.assigned_to_id == request.user.pk
    if not access.is_manager and not is_assignee:
        return None, None, False, _not_found("Update request")
    if write and req.group.is_archived:
        return None, None, False, _archived()
    return access, req, is_assignee, None


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def update_request_detail(request, request_id):
    access, req, _, error = _request_access(request, request_id)
    if error:
        return error
    return Response(serialize_request(req, request.user, can_manage=access.is_manager))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def update_request_submit(request, request_id):
    access, req, is_assignee, error = _request_access(request, request_id, write=True)
    if error:
        return error
    user = request.user
    if not is_assignee:
        return _error("Only the person asked for this update can submit it.", status.HTTP_403_FORBIDDEN, "not_assignee")
    if req.status not in OPEN_REQUEST_STATUSES:
        return _error("This update request is no longer open.", status.HTTP_409_CONFLICT, "request_closed")
    data = request.data
    texts = {k: _text(data.get(k), MAX_TEXT, multiline=True) for k in ("work_completed", "current_status", "blockers", "next_steps")}
    if not (texts["work_completed"] or texts["current_status"]):
        return _error("Describe the work completed or the current status.", status.HTTP_400_BAD_REQUEST)
    progress = _parse_percent(data.get("progress_percent"))
    if progress is False:
        return _error("Progress must be between 0 and 100.", status.HTTP_400_BAD_REQUEST)
    expected = _parse_date(data.get("expected_completion_date"))
    if expected is False:
        return _error("Invalid expected completion date.", status.HTTP_400_BAD_REQUEST)
    raw_attachments = data.get("attachment_ids") or []
    if not isinstance(raw_attachments, list) or len(raw_attachments) > int(settings.MY_RESEARCH_GROUP_MAX_ATTACHMENTS):
        return _error("Invalid attachments.", status.HTTP_400_BAD_REQUEST)
    attachment_ids = [str(a) for a in raw_attachments if _is_uuid(a)]
    attachments = list(
        ResearchUpdateAttachment.objects.filter(
            pk__in=attachment_ids, request=req, uploaded_by=user, status=AttachmentStatus.AVAILABLE, update__isnull=True
        )
    )
    if len(attachments) != len(set(attachment_ids)) or len(attachment_ids) != len(raw_attachments):
        return _not_found("Attachment")
    now = timezone.now()
    with transaction.atomic():
        locked = ResearchUpdateRequest.objects.select_for_update().get(pk=req.pk)
        if locked.status not in OPEN_REQUEST_STATUSES or ResearchUpdate.objects.filter(request=locked).exists():
            return _error("This update request is no longer open.", status.HTTP_409_CONFLICT, "request_closed")
        update = ResearchUpdate.objects.create(
            request=locked, submitted_by=user, progress_percent=progress, expected_completion_date=expected, **texts
        )
        ResearchUpdateAttachment.objects.filter(pk__in=[a.pk for a in attachments]).update(update=update)
        locked.status = UpdateRequestStatus.SUBMITTED
        locked.completed_at = now
        locked.save(update_fields=["status", "completed_at"])
        if locked.activity_id and progress is not None:
            ResearchGroupActivityAssignee.objects.filter(
                activity_id=locked.activity_id, user=user, removed_at__isnull=True
            ).update(progress_percent=progress, updated_at=now)
        record_event(req.group, user, GroupEventAction.UPDATE_SUBMITTED, subject_user=user,
                     target_type="update_request", target_id=req.pk, target_label=req.title)
    req = request_queryset().get(pk=req.pk)
    transaction.on_commit(lambda: notify_update_submitted(req))
    return Response(serialize_request(req, user, can_manage=access.is_manager))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def update_request_review(request, request_id):
    access, req, _, error = _request_access(request, request_id, write=True)
    if error:
        return error
    if not access.can_manage:
        return _forbidden_manage()
    if req.status != UpdateRequestStatus.SUBMITTED:
        return _error("Only submitted updates can be marked as reviewed.", status.HTTP_409_CONFLICT)
    req.status = UpdateRequestStatus.REVIEWED
    req.reviewed_at = timezone.now()
    req.reviewed_by = request.user
    req.review_comment = _text(request.data.get("comment"), MAX_TEXT, multiline=True)
    req.save(update_fields=["status", "reviewed_at", "reviewed_by", "review_comment"])
    record_event(req.group, request.user, GroupEventAction.UPDATE_REVIEWED, subject_user=req.assigned_to,
                 target_type="update_request", target_id=req.pk, target_label=req.title)
    req = request_queryset().get(pk=req.pk)
    transaction.on_commit(lambda: notify_update_reviewed(req))
    return Response(serialize_request(req, request.user, can_manage=True))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def update_request_cancel(request, request_id):
    access, req, _, error = _request_access(request, request_id, write=True)
    if error:
        return error
    if not access.can_manage:
        return _forbidden_manage()
    if req.status not in OPEN_REQUEST_STATUSES:
        return _error("This update request is no longer open.", status.HTTP_409_CONFLICT, "request_closed")
    req.status = UpdateRequestStatus.CANCELLED
    req.cancelled_at = timezone.now()
    req.save(update_fields=["status", "cancelled_at"])
    record_event(req.group, request.user, GroupEventAction.UPDATE_CANCELLED, subject_user=req.assigned_to,
                 target_type="update_request", target_id=req.pk, target_label=req.title)
    return Response(serialize_request(request_queryset().get(pk=req.pk), request.user, can_manage=True))


# ---------------------------------------------------------------- update attachments (existing private bucket)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def attachment_initiate(request, request_id):
    access, req, is_assignee, error = _request_access(request, request_id, write=True)
    if error:
        return error
    if not is_assignee or req.status not in OPEN_REQUEST_STATUSES:
        return _error("Attachments can only be added while submitting your update.", status.HTTP_403_FORBIDDEN)
    if not storage.storage_configured():
        return _error("Research storage is not configured.", status.HTTP_503_SERVICE_UNAVAILABLE, "storage_unavailable")
    try:
        display_name = file_policy.clean_display_name(request.data.get("filename"))
    except file_policy.InvalidFilename as exc:
        return _error(str(exc), status.HTTP_400_BAD_REQUEST)
    if file_policy.is_blocked_name(display_name):
        return _error("Executable and installer files cannot be attached.", status.HTTP_400_BAD_REQUEST, "blocked_extension")
    try:
        size = int(request.data.get("size"))
    except (TypeError, ValueError):
        return _error("File size is required.", status.HTTP_400_BAD_REQUEST)
    max_size = int(settings.MY_RESEARCH_GROUP_ATTACHMENT_MAX_SIZE)
    if size < 0 or size > max_size:
        return _error(f"Attachments can be at most {max_size // (1024**2)} MB.", status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                      "file_too_large")
    existing = ResearchUpdateAttachment.objects.filter(
        request=req, status__in=(AttachmentStatus.PENDING_UPLOAD, AttachmentStatus.AVAILABLE)
    ).count()
    if existing >= int(settings.MY_RESEARCH_GROUP_MAX_ATTACHMENTS):
        return _error("Too many attachments for this update.", status.HTTP_400_BAD_REQUEST, "too_many_attachments")
    raw_type = str(request.data.get("content_type") or "").strip().lower()[:150]
    content_type = raw_type if "/" in raw_type and " " not in raw_type else "application/octet-stream"
    import uuid

    attachment_id = uuid.uuid4()
    key = build_attachment_key(req.group_id, req.pk, attachment_id, file_policy.storage_safe_name(display_name))
    att = ResearchUpdateAttachment.objects.create(
        id=attachment_id, request=req, original_name=file_policy.strip_control_chars(request.data.get("filename"))[:255],
        display_name=display_name, storage_key=key, declared_content_type=content_type, size_bytes=size,
        uploaded_by=request.user,
    )
    expires_in = int(settings.MY_RESEARCH_UPLOAD_URL_EXPIRY_SECONDS)
    try:
        url, headers = storage.presign_put(key, content_type=content_type, expires_in=expires_in)
    except storage.ResearchStorageError:
        logger.exception("my_research groups attachment presign failed attachment=%s", att.pk)
        mark_attachment_failed(att, "Storage was unavailable when the upload started.", delete_object=False)
        return _error("Storage is temporarily unavailable. Please try again.", status.HTTP_503_SERVICE_UNAVAILABLE)
    return Response(
        {"attachment": serialize_attachment(att), "upload": {"method": "PUT", "url": url, "headers": headers,
                                                              "expires_in": expires_in}},
        status=status.HTTP_201_CREATED,
    )


def _attachment_access(request, attachment_id, *, statuses):
    denied = _gate(request)
    if denied:
        return None, None, denied
    att = ResearchUpdateAttachment.objects.select_related("request", "request__group").filter(
        pk=attachment_id, status__in=statuses
    ).first()
    if att is None:
        return None, None, _not_found("Attachment")
    access = resolve_group_access(request.user, att.request.group_id)
    if access is None or not (access.is_manager or att.request.assigned_to_id == request.user.pk):
        return None, None, _not_found("Attachment")
    return access, att, None


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def attachment_complete(request, attachment_id):
    _, att, error = _attachment_access(request, attachment_id, statuses=(AttachmentStatus.PENDING_UPLOAD,))
    if error:
        return error
    if att.uploaded_by_id != request.user.pk:
        return _not_found("Attachment")
    try:
        head = storage.head_object(att.storage_key)
        actual = int(head.get("ContentLength") or 0)
        if actual != att.size_bytes:
            mark_attachment_failed(att, "Uploaded size does not match.", delete_object=True)
            return _error("The uploaded file size does not match.", status.HTTP_422_UNPROCESSABLE_ENTITY, "upload_rejected")
        detected = file_policy.sniff_type(storage.read_prefix(att.storage_key, file_policy.SNIFF_BYTES))
    except storage.ObjectNotFound:
        return _error("The file has not reached storage yet.", status.HTTP_409_CONFLICT, "upload_not_found")
    except storage.ResearchStorageError:
        logger.exception("my_research groups attachment verify failed attachment=%s", att.pk)
        return _error("Could not verify the upload. Please retry.", status.HTTP_503_SERVICE_UNAVAILABLE, "storage_unavailable")
    reason = file_policy.rejection_reason(detected)
    if reason:
        mark_attachment_failed(att, reason, delete_object=True)
        return _error(reason, status.HTTP_422_UNPROCESSABLE_ENTITY, "upload_rejected")
    att.detected_type = detected
    att.status = AttachmentStatus.AVAILABLE
    att.uploaded_at = timezone.now()
    att.save(update_fields=["detected_type", "status", "uploaded_at"])
    return Response(serialize_attachment(att))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def attachment_download(request, attachment_id):
    _, att, error = _attachment_access(request, attachment_id, statuses=(AttachmentStatus.AVAILABLE,))
    if error:
        return error
    expires_in = int(settings.MY_RESEARCH_DOWNLOAD_URL_EXPIRY_SECONDS)
    try:
        url = storage.presign_get(
            att.storage_key, filename=att.display_name, disposition="attachment",
            content_type="application/octet-stream", expires_in=expires_in,
        )
    except storage.ResearchStorageError:
        logger.exception("my_research groups attachment download presign failed attachment=%s", att.pk)
        return _error("Storage is temporarily unavailable. Please try again.", status.HTTP_503_SERVICE_UNAVAILABLE)
    return Response({"url": url, "expires_in": expires_in})


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def attachment_delete(request, attachment_id):
    _, att, error = _attachment_access(
        request, attachment_id, statuses=(AttachmentStatus.PENDING_UPLOAD, AttachmentStatus.AVAILABLE)
    )
    if error:
        return error
    if att.uploaded_by_id != request.user.pk or att.update_id is not None:
        return _error("Submitted attachments cannot be removed.", status.HTTP_409_CONFLICT)
    try:
        storage.delete_object(att.storage_key)
    except storage.ResearchStorageError:
        logger.exception("my_research groups attachment delete failed attachment=%s", att.pk)
    att.status = AttachmentStatus.DELETED
    att.save(update_fields=["status"])
    return Response({"deleted": True})


# ---------------------------------------------------------------- workspaces (association only)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def group_workspaces(request, group_id):
    access, error = _group(request, group_id, manage=request.method == "POST")
    if error:
        return error
    group, user = access.group, request.user
    if request.method == "POST":
        raw = request.data.get("workspace_ids")
        if not isinstance(raw, list) or not raw or len(raw) > MAX_BULK:
            return _error("Provide between 1 and 50 workspace ids.", status.HTTP_400_BAD_REQUEST)
        wanted = {str(v) for v in raw if _is_uuid(v)}
        workspaces = list(_linkable_workspace_qs(access, user).filter(pk__in=wanted))
        if not workspaces or len(wanted) != len(raw):
            return _not_found("Workspace")
        for workspace in workspaces:
            _, created = ResearchGroupWorkspace.objects.get_or_create(group=group, workspace=workspace,
                                                                      defaults={"added_by": user})
            if created:
                record_event(group, user, GroupEventAction.WORKSPACE_LINKED, target_type="workspace",
                             target_id=workspace.pk, target_label=workspace.name)
        if len(workspaces) != len(wanted):
            return _not_found("Workspace")
    links = ResearchGroupWorkspace.objects.filter(group=group).select_related(
        "workspace", "workspace__owner", "workspace__owner__department"
    )
    ws_ids = viewer_workspace_ids(user)
    return Response({"results": [serialize_linked_workspace(link.workspace, ws_ids) for link in links]})


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def group_workspace_unlink(request, group_id, workspace_id):
    access, error = _group(request, group_id, manage=True)
    if error:
        return error
    link = ResearchGroupWorkspace.objects.filter(group=access.group, workspace_id=workspace_id).select_related(
        "workspace"
    ).first()
    if link is None:
        return _not_found("Workspace")
    name = link.workspace.name
    link.delete()
    record_event(access.group, request.user, GroupEventAction.WORKSPACE_UNLINKED, target_type="workspace",
                 target_id=workspace_id, target_label=name)
    return Response({"removed": True})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def linkable_workspaces(request, group_id):
    access, error = _group(request, group_id, manage=True)
    if error:
        return error
    linked = ResearchGroupWorkspace.objects.filter(group=access.group).values_list("workspace_id", flat=True)
    qs = (
        _linkable_workspace_qs(access, request.user)
        .filter(status=WorkspaceStatus.ACTIVE)
        .select_related("owner", "owner__department")
        .order_by("name")
    )
    ws_ids = viewer_workspace_ids(request.user)
    return Response({
        "results": [{**serialize_linked_workspace(w, ws_ids), "linked": w.pk in set(linked)} for w in qs[:200]]
    })


# ---------------------------------------------------------------- publications (existing claims)


def _linkable_claims(access: GroupAccess, user):
    members = _active_member_user_ids(access.group)
    return EquipmentPublicationClaim.objects.filter(
        Q(submitted_by=user) | Q(submitted_by_id__in=members, status=EquipmentPublicationClaimStatus.APPROVED)
    )


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def group_publications(request, group_id):
    access, error = _group(request, group_id, manage=request.method == "POST")
    if error:
        return error
    group, user = access.group, request.user
    if request.method == "POST":
        raw = request.data.get("claim_ids")
        if not isinstance(raw, list) or not raw or len(raw) > MAX_BULK:
            return _error("Provide between 1 and 50 publication ids.", status.HTTP_400_BAD_REQUEST)
        wanted = {_int_or_none(v) for v in raw} - {None}
        claims = list(_linkable_claims(access, user).filter(claim_id__in=wanted))
        if not claims:
            return _not_found("Publication")
        for claim in claims:
            _, created = ResearchGroupPublication.objects.get_or_create(group=group, claim=claim,
                                                                        defaults={"added_by": user})
            if created:
                record_event(group, user, GroupEventAction.PUBLICATION_LINKED, target_type="publication",
                             target_id=claim.claim_id, target_label=claim.title)
    return Response({"results": serialize_group_publications(group)})


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def group_publication_unlink(request, group_id, claim_id):
    access, error = _group(request, group_id, manage=True)
    if error:
        return error
    link = ResearchGroupPublication.objects.filter(group=access.group, claim_id=claim_id).select_related("claim").first()
    if link is None:
        return _not_found("Publication")
    title = link.claim.title
    link.delete()
    record_event(access.group, request.user, GroupEventAction.PUBLICATION_UNLINKED, target_type="publication",
                 target_id=claim_id, target_label=title)
    return Response({"removed": True})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def linkable_group_publications(request, group_id):
    access, error = _group(request, group_id, manage=True)
    if error:
        return error
    linked = ResearchGroupPublication.objects.filter(group=access.group).values_list("claim_id", flat=True)
    claims = (
        _linkable_claims(access, request.user)
        .exclude(claim_id__in=linked)
        .prefetch_related("equipments")
        .order_by("-created_at")[:100]
    )
    return Response({"results": [serialize_publication(c) for c in claims]})


# ---------------------------------------------------------------- activity link pickers / feed


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def linkable_bookings(request, group_id):
    access, error = _group(request, group_id, manage=True)
    if error:
        return error
    qs = _linkable_booking_qs(access, request.user).select_related("equipment", "user")
    user_id = _int_or_none(request.query_params.get("user_id"))
    if user_id:
        qs = qs.filter(user_id=user_id)
    q = _text(request.query_params.get("q"), 100)
    if q:
        match = Q(equipment__name__icontains=q) | Q(equipment__code__icontains=q) | Q(virtual_booking_id__icontains=q)
        if q.isdigit():
            match |= Q(booking_id=int(q))
        qs = qs.filter(match)
    return Response(
        {"results": [{**serialize_booking_ref(b), "user": public_user(b.user)} for b in qs.order_by("-created_at")[:50]]}
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def linkable_equipment(request, group_id):
    _, error = _group(request, group_id, manage=True)
    if error:
        return error
    q = _text(request.query_params.get("q"), 100)
    qs = Equipment.objects.all()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(code__icontains=q))
    return Response(
        {"results": [{"equipment_id": e.equipment_id, "name": e.name, "code": e.code} for e in qs.order_by("name")[:25]]}
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def group_feed(request, group_id):
    access, error = _group(request, group_id)
    if error:
        return error
    managed = {access.group.pk} if access.is_manager else set()
    qs = (
        visible_events([access.group.pk], request.user, managed)
        .select_related("actor", "actor__department", "subject_user", "subject_user__department")
        .order_by("-created_at")
    )
    page, size = _page(request)
    total = qs.count()
    items = list(qs[(page - 1) * size : page * size])
    return Response(
        {
            "results": [serialize_event(e) for e in items],
            "pagination": {"page": page, "page_size": size, "total": total, "has_next": page * size < total},
        }
    )

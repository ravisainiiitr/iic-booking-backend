"""Serialization, feed events, counts and notifications for Research Groups."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Iterable

from django.conf import settings
from django.db.models import Count, IntegerField, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.html import escape

from iic_booking.communication.utils import booking_display_id_for_email

from . import storage
from .access import accessible_workspace_ids
from .group_models import (
    AttachmentStatus,
    GroupActivityStatus,
    GroupMemberStatus,
    GroupRole,
    ResearchGroup,
    ResearchGroupActivity,
    ResearchGroupActivityAssignee,
    ResearchGroupEvent,
    ResearchGroupMember,
    ResearchUpdateAttachment,
    ResearchUpdateRequest,
    UpdateRequestStatus,
)
from .services import serialize_publication, user_summary

logger = logging.getLogger(__name__)

OPEN_ACTIVITY_STATUSES = (
    GroupActivityStatus.NOT_STARTED,
    GroupActivityStatus.IN_PROGRESS,
    GroupActivityStatus.WAITING,
    GroupActivityStatus.SUBMITTED,
    GroupActivityStatus.UNDER_REVIEW,
)
OPEN_REQUEST_STATUSES = (UpdateRequestStatus.PENDING, UpdateRequestStatus.OVERDUE)
# Statuses an assignee may set on their own assignment; review/completion stays with faculty.
ASSIGNEE_STATUSES = (
    GroupActivityStatus.NOT_STARTED,
    GroupActivityStatus.IN_PROGRESS,
    GroupActivityStatus.WAITING,
    GroupActivityStatus.SUBMITTED,
)


def today() -> date:
    return timezone.localdate()


# ---------------------------------------------------------------- events


def record_event(
    group: ResearchGroup,
    actor,
    action: str,
    *,
    subject_user=None,
    target_type: str = "",
    target_id: Any = "",
    target_label: str = "",
    details: dict | None = None,
) -> ResearchGroupEvent:
    return ResearchGroupEvent.objects.create(
        group=group,
        actor=actor,
        subject_user=subject_user,
        action=action,
        target_type=target_type,
        target_id=str(target_id or ""),
        target_label=(target_label or "")[:300],
        details=details or {},
    )


def serialize_event(event: ResearchGroupEvent, *, include_group: bool = False) -> dict[str, Any]:
    data = {
        "id": event.pk,
        "action": event.action,
        "action_label": event.get_action_display(),
        "actor": public_user(event.actor) if event.actor_id else None,
        "subject": public_user(event.subject_user) if event.subject_user_id else None,
        "target_type": event.target_type,
        "target_id": event.target_id,
        "target_label": event.target_label,
        "created_at": event.created_at,
    }
    if include_group:
        data["group_id"] = str(event.group_id)
        data["group_name"] = event.group.name
    return data


def visible_events(group_ids: Iterable, user, managed_ids: set):
    """Managers see all events of their groups; members see group-wide events, their own events, and
    events about activities they are assigned to (never titles of other people's activities)."""
    managed = [g for g in group_ids if g in managed_ids]
    member_only = [g for g in group_ids if g not in managed_ids]
    my_activity_ids = [
        str(pk)
        for pk in ResearchGroupActivityAssignee.objects.filter(
            user=user, activity__group_id__in=member_only
        ).values_list("activity_id", flat=True)
    ]
    group_wide = Q(subject_user__isnull=True) & (~Q(target_type="activity") | Q(target_id__in=my_activity_ids))
    return ResearchGroupEvent.objects.filter(
        Q(group_id__in=managed) | (Q(group_id__in=member_only) & (group_wide | Q(subject_user=user)))
    )


# ---------------------------------------------------------------- users / members


def public_user(user) -> dict[str, Any] | None:
    """Name and department only (safe for any group member)."""
    if user is None:
        return None
    department = getattr(user, "department", None)
    return {"id": user.pk, "name": user.name or user.email, "department": department.name if department else None}


def serialize_category(category) -> dict[str, Any] | None:
    if category is None:
        return None
    return {
        "id": category.pk,
        "name": category.name,
        "description": category.description,
        "display_order": category.display_order,
        "active": category.active,
    }


def serialize_member(member: ResearchGroupMember, *, for_manager: bool) -> dict[str, Any]:
    data = {
        "id": member.pk,
        "user": user_summary(member.user) if for_manager else public_user(member.user),
        "role": member.role,
        "role_label": member.get_role_display(),
        "member_type": member.member_type,
        "member_type_label": member.get_member_type_display(),
        "category": serialize_category(member.category),
        "status": member.status,
        "joined_at": member.joined_at,
        "left_at": member.left_at,
    }
    if for_manager:
        data["active_activities"] = getattr(member, "open_assignments", None)
        data["open_update_requests"] = getattr(member, "open_requests", None)
    return data


def annotate_member_workload(queryset):
    open_assignments = Coalesce(
        Subquery(
            ResearchGroupActivityAssignee.objects.filter(
                user=OuterRef("user"),
                activity__group=OuterRef("group"),
                removed_at__isnull=True,
                activity__status__in=OPEN_ACTIVITY_STATUSES,
            )
            .order_by()
            .values("user")
            .annotate(c=Count("pk"))
            .values("c")[:1],
            output_field=IntegerField(),
        ),
        Value(0),
    )
    open_requests = Coalesce(
        Subquery(
            ResearchUpdateRequest.objects.filter(
                assigned_to=OuterRef("user"), group=OuterRef("group"), status__in=OPEN_REQUEST_STATUSES
            )
            .order_by()
            .values("assigned_to")
            .annotate(c=Count("pk"))
            .values("c")[:1],
            output_field=IntegerField(),
        ),
        Value(0),
    )
    return queryset.annotate(open_assignments=open_assignments, open_requests=open_requests)


# ---------------------------------------------------------------- workspaces / bookings (association only)


def serialize_linked_workspace(workspace, viewer_workspace_ids: set) -> dict[str, Any] | None:
    """Name and owner only; `accessible` reflects the caller's own workspace permission."""
    if workspace is None:
        return None
    return {
        "id": str(workspace.pk),
        "name": workspace.name,
        "owner": public_user(workspace.owner),
        "status": workspace.status,
        "accessible": workspace.pk in viewer_workspace_ids,
    }


def serialize_booking_ref(booking) -> dict[str, Any] | None:
    if booking is None:
        return None
    equipment = booking.equipment
    return {
        "booking_id": booking.booking_id,
        "display_id": booking_display_id_for_email(booking),
        "status_display": booking.get_status_display(),
        "equipment_name": equipment.name if equipment else None,
    }


def serialize_equipment_ref(equipment) -> dict[str, Any] | None:
    if equipment is None:
        return None
    return {"equipment_id": equipment.equipment_id, "name": equipment.name, "code": equipment.code}


# ---------------------------------------------------------------- activities


def activity_queryset():
    return ResearchGroupActivity.objects.select_related(
        "group", "category", "created_by", "workspace", "workspace__owner", "workspace__owner__department",
        "equipment", "booking", "booking__equipment",
    ).prefetch_related("assignees__user", "assignees__user__department")


def serialize_assignee(assignee: ResearchGroupActivityAssignee) -> dict[str, Any]:
    return {
        "id": assignee.pk,
        "user": public_user(assignee.user),
        "status": assignee.status,
        "status_label": assignee.get_status_display(),
        "progress_percent": assignee.progress_percent,
        "note": assignee.note,
        "assigned_at": assignee.assigned_at,
        "updated_at": assignee.updated_at,
    }


def serialize_activity(activity: ResearchGroupActivity, user, *, can_manage: bool, viewer_workspace_ids: set) -> dict:
    assignees = [a for a in activity.assignees.all() if a.removed_at is None]
    mine = next((a for a in assignees if a.user_id == user.pk), None)
    visible = assignees if can_manage else ([mine] if mine else [])
    is_open = activity.status in OPEN_ACTIVITY_STATUSES
    return {
        "id": str(activity.pk),
        "group_id": str(activity.group_id),
        "group_name": activity.group.name,
        "title": activity.title,
        "description": activity.description,
        "category": serialize_category(activity.category),
        "status": activity.status,
        "status_label": activity.get_status_display(),
        "priority": activity.priority,
        "priority_label": activity.get_priority_display(),
        "progress_percent": activity.progress_percent,
        "start_date": activity.start_date,
        "due_date": activity.due_date,
        "is_overdue": bool(is_open and activity.due_date and activity.due_date < today()),
        "completed_at": activity.completed_at,
        "created_by": public_user(activity.created_by) if activity.created_by_id else None,
        "created_at": activity.created_at,
        "updated_at": activity.updated_at,
        "workspace": serialize_linked_workspace(activity.workspace, viewer_workspace_ids),
        "equipment": serialize_equipment_ref(activity.equipment),
        "booking": serialize_booking_ref(activity.booking),
        "assignees": [serialize_assignee(a) for a in visible],
        "assignee_count": len(assignees),
        "my_assignment": serialize_assignee(mine) if mine else None,
        "permissions": {"can_edit": can_manage, "can_update_progress": bool(mine) and not activity.group.is_archived},
    }


# ---------------------------------------------------------------- update requests


def effective_request_status(req: ResearchUpdateRequest) -> str:
    if req.status == UpdateRequestStatus.PENDING and req.due_date and req.due_date < today():
        return UpdateRequestStatus.OVERDUE
    return req.status


def request_queryset():
    return ResearchUpdateRequest.objects.select_related(
        "group", "activity", "requested_by", "assigned_to", "assigned_to__department", "reviewed_by", "submission",
    ).prefetch_related("attachments")


def serialize_attachment(att: ResearchUpdateAttachment) -> dict[str, Any]:
    return {
        "id": str(att.id),
        "name": att.display_name,
        "size_bytes": att.size_bytes,
        "status": att.status,
        "detected_type": att.detected_type,
        "created_at": att.created_at,
    }


def serialize_submission(update) -> dict[str, Any] | None:
    if update is None:
        return None
    return {
        "id": str(update.id),
        "work_completed": update.work_completed,
        "current_status": update.current_status,
        "blockers": update.blockers,
        "next_steps": update.next_steps,
        "progress_percent": update.progress_percent,
        "expected_completion_date": update.expected_completion_date,
        "submitted_at": update.submitted_at,
    }


def serialize_request(req: ResearchUpdateRequest, user, *, can_manage: bool) -> dict[str, Any]:
    status_value = effective_request_status(req)
    submission = getattr(req, "submission", None)
    days_overdue = (today() - req.due_date).days if status_value == UpdateRequestStatus.OVERDUE and req.due_date else 0
    is_assignee = req.assigned_to_id == user.pk
    attachments = [a for a in req.attachments.all() if a.status == AttachmentStatus.AVAILABLE]
    return {
        "id": str(req.id),
        "group_id": str(req.group_id),
        "group_name": req.group.name,
        "activity": {"id": str(req.activity_id), "title": req.activity.title} if req.activity_id else None,
        "title": req.title,
        "instructions": req.instructions,
        "due_date": req.due_date,
        "days_overdue": days_overdue,
        "status": status_value,
        "status_label": UpdateRequestStatus(status_value).label,
        "recurrence": req.recurrence,
        "requested_by": public_user(req.requested_by) if req.requested_by_id else None,
        "assigned_to": public_user(req.assigned_to),
        "requested_at": req.requested_at,
        "completed_at": req.completed_at,
        "reviewed_at": req.reviewed_at,
        "reviewed_by": public_user(req.reviewed_by) if req.reviewed_by_id else None,
        "review_comment": req.review_comment,
        "submission": serialize_submission(submission) if (can_manage or is_assignee) else None,
        "attachments": [serialize_attachment(a) for a in attachments] if (can_manage or is_assignee) else [],
        "permissions": {
            "can_submit": is_assignee and status_value in OPEN_REQUEST_STATUSES and not req.group.is_archived,
            "can_review": can_manage and status_value == UpdateRequestStatus.SUBMITTED,
            "can_cancel": can_manage and status_value in OPEN_REQUEST_STATUSES,
        },
    }


# ---------------------------------------------------------------- group summaries


def group_counts(group_ids: Iterable) -> dict:
    """Factual counts per group: members, open activities, pending/overdue/submitted requests."""
    ids = list(group_ids)
    counts = {gid: {"members": 0, "active_activities": 0, "pending_updates": 0, "overdue_updates": 0, "awaiting_review": 0,
                    "activities_due_this_week": 0} for gid in ids}
    if not ids:
        return counts
    for row in (
        ResearchGroupMember.objects.filter(group_id__in=ids, status=GroupMemberStatus.ACTIVE)
        .values("group_id").annotate(c=Count("pk"))
    ):
        counts[row["group_id"]]["members"] = row["c"]
    now_date = today()
    week_end = now_date + timedelta(days=7)
    for row in (
        ResearchGroupActivity.objects.filter(group_id__in=ids, status__in=OPEN_ACTIVITY_STATUSES)
        .values("group_id")
        .annotate(
            c=Count("pk"),
            due=Count("pk", filter=Q(due_date__gte=now_date, due_date__lte=week_end)),
        )
    ):
        counts[row["group_id"]]["active_activities"] = row["c"]
        counts[row["group_id"]]["activities_due_this_week"] = row["due"]
    overdue_q = Q(status=UpdateRequestStatus.OVERDUE) | Q(status=UpdateRequestStatus.PENDING, due_date__lt=now_date)
    for row in (
        ResearchUpdateRequest.objects.filter(group_id__in=ids)
        .values("group_id")
        .annotate(
            pending=Count("pk", filter=Q(status__in=OPEN_REQUEST_STATUSES) & ~overdue_q),
            overdue=Count("pk", filter=overdue_q),
            submitted=Count("pk", filter=Q(status=UpdateRequestStatus.SUBMITTED)),
        )
    ):
        counts[row["group_id"]]["pending_updates"] = row["pending"]
        counts[row["group_id"]]["overdue_updates"] = row["overdue"]
        counts[row["group_id"]]["awaiting_review"] = row["submitted"]
    return counts


def serialize_group_card(group: ResearchGroup, role: str, counts: dict, membership=None) -> dict[str, Any]:
    is_manager = role in (GroupRole.OWNER, GroupRole.MANAGER)
    data = {
        "id": str(group.pk),
        "name": group.name,
        "short_code": group.short_code,
        "description": group.description,
        "status": group.status,
        "owner": public_user(group.owner),
        "my_role": role,
        "created_at": group.created_at,
        "archived_at": group.archived_at,
        "counts": {"members": counts.get("members", 0)},
    }
    if is_manager:
        data["counts"].update(
            {k: counts.get(k, 0) for k in ("active_activities", "pending_updates", "overdue_updates", "awaiting_review",
                                           "activities_due_this_week")}
        )
    if membership is not None:
        data["my_membership"] = {
            "member_type": membership.member_type,
            "member_type_label": membership.get_member_type_display(),
            "category": serialize_category(membership.category),
        }
    return data


def member_work_counts(user, group_ids: Iterable) -> dict:
    ids = list(group_ids)
    result = {gid: {"my_active_activities": 0, "my_open_requests": 0} for gid in ids}
    for row in (
        ResearchGroupActivityAssignee.objects.filter(
            user=user, removed_at__isnull=True, activity__group_id__in=ids, activity__status__in=OPEN_ACTIVITY_STATUSES
        ).values("activity__group_id").annotate(c=Count("pk"))
    ):
        result[row["activity__group_id"]]["my_active_activities"] = row["c"]
    for row in (
        ResearchUpdateRequest.objects.filter(assigned_to=user, group_id__in=ids, status__in=OPEN_REQUEST_STATUSES)
        .values("group_id").annotate(c=Count("pk"))
    ):
        result[row["group_id"]]["my_open_requests"] = row["c"]
    return result


def viewer_workspace_ids(user) -> set:
    return accessible_workspace_ids(user)


def serialize_group_publications(group) -> list[dict]:
    links = group.publication_links.select_related("claim").prefetch_related("claim__equipments")
    return [serialize_publication(link.claim) for link in links]


# ---------------------------------------------------------------- attachments


def build_attachment_key(group_id, request_id, attachment_id, safe_filename: str) -> str:
    prefix = (getattr(settings, "MY_RESEARCH_S3_PREFIX", "research") or "research").strip("/")
    return f"{prefix}/groups/{group_id}/update-requests/{request_id}/{attachment_id}/{safe_filename}"


def mark_attachment_failed(att: ResearchUpdateAttachment, reason: str, *, delete_object: bool) -> None:
    if delete_object:
        try:
            storage.delete_object(att.storage_key)
        except storage.ResearchStorageError:
            logger.exception("my_research groups: failed to delete rejected attachment=%s", att.pk)
    att.status = AttachmentStatus.FAILED
    att.failure_reason = reason[:255]
    att.save(update_fields=["status", "failure_reason"])


# ---------------------------------------------------------------- notifications


def _push(recipient, title: str, message: str, link_path: str, event: str, **meta) -> None:
    from iic_booking.communication.service import CommunicationService
    from iic_booking.communication.utils import get_frontend_absolute_url

    try:
        CommunicationService.send_push_notification(
            recipient=recipient,
            title=title,
            message=message,
            metadata={
                "notification_type": "info",
                "link": get_frontend_absolute_url(link_path),
                "event": event,
                **{k: str(v) for k, v in meta.items()},
            },
        )
    except Exception:
        logger.exception("my_research groups push failed event=%s recipient=%s", event, getattr(recipient, "pk", None))


def _email(recipient, subject: str, heading: str, message: str, rows: list[tuple[str, str]], link_path: str) -> None:
    from iic_booking.communication.email_branding import COLOR_PRIMARY
    from iic_booking.communication.styled_transactional_emails import _send, _shell
    from iic_booking.communication.utils import get_frontend_absolute_url

    link = get_frontend_absolute_url(link_path)
    try:
        body = f"<p style='margin:0 0 12px 0;'>{escape(message)}</p>" + "".join(
            f"<p style='margin:0 0 8px 0;'><b>{escape(k)}:</b> {escape(v)}</p>" for k, v in rows if v
        ) + (
            f"<p style='margin:16px 0 0 0;'><a href='{escape(link)}' style='background:{COLOR_PRIMARY};color:#fff;"
            f"padding:10px 14px;border-radius:8px;text-decoration:none;font-weight:700;'>Open My Research</a></p>"
        )
        text = "\n".join([message, *[f"{k}: {v}" for k, v in rows if v], f"Open: {link}"])
        _send(recipient.email, subject, text, _shell(heading, subject, body))
    except Exception:
        logger.exception("my_research groups email failed subject=%s", subject)


def _actor_name(user) -> str:
    from iic_booking.communication.email_branding import user_display_name

    return user_display_name(user) if user is not None else "Your supervisor"


def notify_member_added(member: ResearchGroupMember, actor) -> None:
    group = member.group
    message = f"{_actor_name(actor)} added you to the research group \"{group.name}\" as {member.get_member_type_display()}."
    path = f"/my-research/groups/{group.pk}"
    _push(member.user, "Added to a Research Group", message, path, "research_group.member_added", research_group_id=group.pk)
    _email(
        member.user, "You were added to a Research Group", "Research Group", message,
        [("Group", group.name), ("Category", member.category.name if member.category else ""),
         ("Note", "Group membership does not give access to research workspaces; those are shared separately.")],
        path,
    )


def notify_member_removed(member: ResearchGroupMember) -> None:
    _push(
        member.user, "Removed from a Research Group",
        f"You are no longer a member of the research group \"{member.group.name}\".",
        "/my-research", "research_group.member_removed", research_group_id=member.group_id,
    )


def notify_activity_assigned(activity: ResearchGroupActivity, user, actor) -> None:
    due = activity.due_date.strftime("%d %b %Y") if activity.due_date else "no due date"
    _push(
        user, "Research activity assigned",
        f"{_actor_name(actor)} assigned you \"{activity.title}\" in {activity.group.name} ({due}).",
        f"/my-research/groups/{activity.group_id}?tab=activities", "research_group.activity_assigned",
        research_group_id=activity.group_id, research_activity_id=activity.pk,
    )


def notify_update_requested(req: ResearchUpdateRequest) -> None:
    due = req.due_date.strftime("%d %b %Y") if req.due_date else ""
    message = f"{_actor_name(req.requested_by)} requested an update: \"{req.title}\"" + (f", due {due}." if due else ".")
    path = f"/my-research/groups/{req.group_id}?tab=updates&request={req.pk}"
    _push(req.assigned_to, "Research update requested", message, path, "research_group.update_requested",
          research_group_id=req.group_id, research_update_request_id=req.pk)
    _email(req.assigned_to, "Research update requested", "Research Group", message,
           [("Group", req.group.name), ("Due", due), ("Instructions", req.instructions[:1000])], path)


def notify_update_submitted(req: ResearchUpdateRequest) -> None:
    if req.requested_by is None:
        return
    _push(
        req.requested_by, "Research update submitted",
        f"{_actor_name(req.assigned_to)} submitted \"{req.title}\" in {req.group.name}.",
        f"/my-research/groups/{req.group_id}?tab=updates&request={req.pk}", "research_group.update_submitted",
        research_group_id=req.group_id, research_update_request_id=req.pk,
    )


def notify_update_reviewed(req: ResearchUpdateRequest) -> None:
    _push(
        req.assigned_to, "Research update reviewed",
        f"{_actor_name(req.reviewed_by)} reviewed your update \"{req.title}\".",
        f"/my-research/groups/{req.group_id}?tab=updates&request={req.pk}", "research_group.update_reviewed",
        research_group_id=req.group_id, research_update_request_id=req.pk,
    )


def notify_update_overdue(req: ResearchUpdateRequest) -> None:
    message = f"Your update \"{req.title}\" for {req.group.name} is overdue."
    path = f"/my-research/groups/{req.group_id}?tab=updates&request={req.pk}"
    _push(req.assigned_to, "Research update overdue", message, path, "research_group.update_overdue",
          research_group_id=req.group_id, research_update_request_id=req.pk)
    _email(req.assigned_to, "Research update overdue", "Research Group", message,
           [("Due", req.due_date.strftime("%d %b %Y") if req.due_date else "")], path)


def notify_activity_due(assignee: ResearchGroupActivityAssignee) -> None:
    activity = assignee.activity
    _push(
        assignee.user, "Research activity due soon",
        f"\"{activity.title}\" in {activity.group.name} is due on {activity.due_date.strftime('%d %b %Y')}.",
        f"/my-research/groups/{activity.group_id}?tab=activities", "research_group.activity_due",
        research_group_id=activity.group_id, research_activity_id=activity.pk,
    )

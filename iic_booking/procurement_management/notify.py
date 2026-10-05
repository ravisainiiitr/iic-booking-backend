"""Notifications. In-app always (after commit, failure-safe); email only when an active ``CommunicationTemplate``
with the event's ``procurement_*`` code exists, so the module sends no email until the templates are created."""

from __future__ import annotations

import logging

from django.db import transaction

from . import constants as c

logger = logging.getLogger(__name__)
R = c.ModuleRole
S = c.ApprovalStage

LINK_REQUEST = "/procurement/requests/{id}"


def _email(users, code: str, context: dict) -> None:
    def send():
        try:
            from iic_booking.communication.models import CommunicationTemplate
            from iic_booking.communication.service import CommunicationService

            if not CommunicationTemplate.objects.filter(code=code, is_active=True).exists():
                return
            for user in users:
                if getattr(user, "email", "") and getattr(user, "is_active", True):
                    CommunicationService.send_email(user, template=code, template_context=context)
        except Exception:
            logger.exception("procurement email %s failed", code)

    transaction.on_commit(send)


def notify(users, *, department_id: int, title: str, message: str, link: str, event: str, actor=None, extra=None) -> None:
    from .access import pilot_audience

    users = [u for u in pilot_audience(department_id, users) if getattr(u, "pk", None) != getattr(actor, "pk", None)]
    if not users:
        return
    try:
        from iic_booking.communication.in_app import notify_in_app

        notify_in_app(
            users, title=title, message=message, link=link, notification_type="info", event=event,
            created_by=actor, extra=extra or {},
        )
    except Exception:
        logger.exception("procurement in-app notification %s failed", event)
    _email(users, f"procurement_{event}", {"title": title, "message": message, "link": link, **(extra or {})})


def department_role_users(department_id: int, role: str, *, permission: str | None = None) -> list:
    from .models import ProcurementRoleAssignment

    rows = ProcurementRoleAssignment.objects.filter(department_id=department_id, role=role, active=True).select_related("user")
    out = []
    for row in rows:
        if permission and role == R.OFFICE and permission not in (row.permissions or []):
            continue
        out.append(row.user)
    return out


def hod_users(department_id: int) -> list:
    from iic_booking.users.models import User

    from .access import department_hod_ids

    users = list(User.objects.filter(pk__in=department_hod_ids(department_id), is_active=True))
    users += department_role_users(department_id, R.HOD)
    return users


def stage_approvers(r, stage: str) -> list:
    """Users who can act on ``stage`` of request ``r`` (the requester is never included)."""
    from iic_booking.communication.in_app import equipment_oic_users

    if stage == S.OIC:
        users = equipment_oic_users(r.equipment)
    elif stage == S.STORES:
        users = department_role_users(r.department_id, R.OC_STORES)
    elif stage == S.HOD:
        users = hod_users(r.department_id)
    else:
        users = []
    seen, out = set(), []
    for u in users:
        if u.pk in seen or u.pk == r.requested_by_id or not u.is_active:
            continue
        seen.add(u.pk)
        out.append(u)
    return out


def request_pending(r, stage: str, actor) -> None:
    users = stage_approvers(r, stage)
    if stage == S.HOD:
        users += department_role_users(r.department_id, R.OFFICE, permission=c.OfficePermission.OFFLINE_APPROVAL)
    notify(
        users,
        department_id=r.department_id,
        title=f"Approval needed: {r.number}",
        message=f"{r.title} (₹{r.estimated_total}) is waiting for {c.ApprovalStage(stage).label} approval.",
        link=LINK_REQUEST.format(id=r.pk),
        event="request_pending",
        actor=actor,
        extra={"request_id": r.pk, "number": r.number, "stage": stage},
    )


def request_update(r, action: str, actor, reason: str = "") -> None:
    label = c.ApprovalActionType(action).label if action in c.ApprovalActionType.values else action
    msg = f"{r.number} — {r.title}: {label}."
    if reason:
        msg += f" Reason: {reason}"
    notify(
        [r.requested_by],
        department_id=r.department_id,
        title=f"Request {r.number}: {label}",
        message=msg,
        link=LINK_REQUEST.format(id=r.pk),
        event="request_update",
        actor=actor,
        extra={"request_id": r.pk, "number": r.number, "action": action},
    )


def office_users(department_id: int, permission: str) -> list:
    return department_role_users(department_id, R.OFFICE, permission=permission)

"""Helpers for OIC notice-board requests and equipment-unavailability notices."""

from __future__ import annotations

from django.db.models import Q
from django.utils import timezone

from iic_booking.users.models import UserType

from .models import Notice


def is_main_admin(user) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_staff", False):
        return True
    return getattr(user, "user_type", None) == UserType.ADMIN


def is_oic(user) -> bool:
    return bool(
        user
        and getattr(user, "is_authenticated", False)
        and getattr(user, "user_type", None) == UserType.MANAGER
    )


def user_can_manage_notice_request(user, notice: Notice) -> bool:
    """OIC may act on own requests or equipment-linked drafts for equipment they manage."""
    if is_main_admin(user):
        return True
    if not is_oic(user):
        return False
    if notice.requested_by_id == getattr(user, "id", None):
        return True
    if notice.created_by_id == getattr(user, "id", None):
        return True
    if notice.equipment_id:
        from iic_booking.equipment.models import EquipmentManager, EquipmentTemporaryOIC
        from iic_booking.equipment.reports import get_equipment_ids_managed_by_oic

        try:
            managed = set(get_equipment_ids_managed_by_oic(user.id))
        except Exception:
            managed = set(
                EquipmentManager.objects.filter(manager_id=user.id).values_list(
                    "equipment_id", flat=True
                )
            )
        if notice.equipment_id in managed:
            return True
        return EquipmentTemporaryOIC.objects.filter(
            equipment_id=notice.equipment_id,
            temporary_oic=user,
            resume_at__gt=timezone.now(),
        ).exists()
    return False


def public_notices_queryset():
    """Notices visible on the public notice board."""
    now = timezone.now()
    return Notice.objects.filter(
        is_active=True,
        approval_status=Notice.ApprovalStatus.APPROVED,
    ).filter(Q(expiry_date__isnull=True) | Q(expiry_date__gt=now))


def equipment_display_label(equipment) -> str:
    """'Name (CODE)', without repeating the code when the name already contains it."""
    code = (getattr(equipment, "code", None) or "").strip()
    name = (getattr(equipment, "name", None) or "").strip()
    if not name:
        return code or f"Equipment #{equipment.pk}"
    if not code or code.lower() in name.lower():
        return name
    return f"{name} ({code})"


def build_equipment_unavailable_copy(equipment, *, since=None) -> tuple[str, str]:
    label = equipment_display_label(equipment)
    since_date = since or timezone.localdate()
    since_text = f"{since_date.day} {since_date:%b %Y}"
    location = " ".join((getattr(equipment, "location", None) or "").split()).rstrip(".")
    department = getattr(getattr(equipment, "internal_department", None), "name", None) or ""

    suffix = " — Under Maintenance"
    title = f"{label[: 255 - len(suffix)]}{suffix}"
    lines = [
        f"{label} is under maintenance from {since_text} and is not available for booking until further notice.",
    ]
    if location:
        lines.append(f"Location: {location}.")
    if department.strip():
        lines.append(f"Facility: {department.strip()}.")
    lines.append(
        "New bookings are paused while the equipment is being serviced. "
        "Please plan your experiments accordingly or contact the facility for urgent requirements."
    )
    lines.append(
        "This notice will be removed automatically once the equipment is back in operation."
    )
    return title, "\n".join(lines)


def create_or_reuse_equipment_unavailable_draft(*, equipment, actor) -> Notice:
    """
    Idempotent: reuse open DRAFT/PENDING EQUIPMENT_UNAVAILABLE notice for this equipment.
    """
    open_qs = Notice.objects.filter(
        equipment=equipment,
        source=Notice.Source.EQUIPMENT_UNAVAILABLE,
        approval_status__in=[
            Notice.ApprovalStatus.DRAFT,
            Notice.ApprovalStatus.PENDING,
        ],
    ).order_by("-created_at")
    existing = open_qs.first()
    title, description = build_equipment_unavailable_copy(
        equipment,
        since=timezone.localdate(existing.created_at) if existing else None,
    )
    if existing:
        existing.title = title
        existing.description = description
        existing.notice_type = Notice.NoticeType.WARNING
        if existing.approval_status == Notice.ApprovalStatus.DRAFT:
            existing.needs_oic_expiry = True
        existing.save(
            update_fields=[
                "title",
                "description",
                "notice_type",
                "needs_oic_expiry",
                "updated_at",
            ]
        )
        return existing

    return Notice.objects.create(
        title=title,
        description=description,
        content="",
        notice_type=Notice.NoticeType.WARNING,
        is_active=False,
        priority=10,
        created_by=actor if getattr(actor, "is_authenticated", False) else None,
        requested_by=actor if getattr(actor, "is_authenticated", False) else None,
        expiry_date=None,
        approval_status=Notice.ApprovalStatus.DRAFT,
        source=Notice.Source.EQUIPMENT_UNAVAILABLE,
        equipment=equipment,
        needs_oic_expiry=True,
        expiry_unlimited=False,
    )


def expire_equipment_linked_notices(*, equipment, actor=None) -> int:
    """
    When equipment returns to Operational: stamp expiry_date=now on linked
    APPROVED / DRAFT / PENDING EQUIPMENT_UNAVAILABLE notices so they leave the board.
    """
    now = timezone.now()
    qs = Notice.objects.filter(
        equipment=equipment,
        source=Notice.Source.EQUIPMENT_UNAVAILABLE,
        approval_status__in=[
            Notice.ApprovalStatus.DRAFT,
            Notice.ApprovalStatus.PENDING,
            Notice.ApprovalStatus.APPROVED,
        ],
    )
    count = 0
    for notice in qs:
        notice.expiry_date = now
        notice.expiry_unlimited = False
        notice.needs_oic_expiry = False
        # Keep APPROVED for audit but inactive + expired hides from board.
        if notice.approval_status == Notice.ApprovalStatus.APPROVED:
            notice.is_active = False
        elif notice.approval_status in (
            Notice.ApprovalStatus.DRAFT,
            Notice.ApprovalStatus.PENDING,
        ):
            notice.approval_status = Notice.ApprovalStatus.REJECTED
            notice.review_comment = (
                (notice.review_comment or "").strip()
                + ("\n" if notice.review_comment else "")
                + f"Auto-closed: equipment returned to Operational at {now.isoformat()}."
            ).strip()
            notice.reviewed_at = now
            if actor and getattr(actor, "is_authenticated", False):
                notice.reviewed_by = actor
            notice.is_active = False
        notice.save()
        count += 1
    return count


NON_OPERATIONAL_STATUSES = frozenset({"REPAIR", "INACTIVE", "MAINTENANCE"})

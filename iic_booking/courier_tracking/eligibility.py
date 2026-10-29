"""Who may declare how a sample arrives, and who may use courier tracking.

* Internal IIT Roorkee users (``UserType.get_internal_user_codes()``) always deliver manually. There is no courier
  exception for them.
* External users (``UserType.get_external_user_codes()``) may register a courier shipment when the booking was made as
  an external booking (``Booking.user_type_snapshot`` - the same rule the Accounts In Charge and the external sample
  workflow use), the user is still external now, and the equipment's department has Courier Tracking switched on
  (``department_modules``; on for IIC by default, off elsewhere). If the snapshot and the current type disagree the
  booking is treated as not external: manual delivery only.
* Only the booking's own user declares the arrival. Staff can view.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from iic_booking.department_modules import access as module_access
from iic_booking.department_modules.constants import ModuleKey
from iic_booking.users.models.user_type import UserType

from .constants import ARRIVAL_OPEN_BOOKING_STATUSES, USER_TRACE_STATUSES

MODULE = ModuleKey.COURIER_TRACKING.value


@dataclass
class Eligibility:
    may_declare: bool
    manual_allowed: bool
    courier_allowed: bool
    is_external: bool
    department_enabled: bool
    reasons: list[str] = field(default_factory=list)
    courier_reason: str = ""

    def as_dict(self) -> dict:
        return {
            "may_declare": self.may_declare,
            "manual_allowed": self.manual_allowed,
            "courier_allowed": self.courier_allowed,
            "is_external": self.is_external,
            "department_enabled": self.department_enabled,
            "default_mode": "COURIER" if self.courier_allowed else "MANUAL",
            "reasons": self.reasons,
            "courier_reason": self.courier_reason,
        }


def booking_is_external(booking) -> bool:
    """External booking by an (still) external user. Fails closed when the two disagree or either is missing."""
    snapshot = (getattr(booking, "user_type_snapshot", "") or "").strip()
    current = (getattr(getattr(booking, "user", None), "user_type", "") or "").strip()
    external = UserType.get_external_user_codes()
    return snapshot in external and current in external


def booking_is_internal(booking) -> bool:
    snapshot = (getattr(booking, "user_type_snapshot", "") or "").strip()
    current = (getattr(getattr(booking, "user", None), "user_type", "") or "").strip()
    internal = UserType.get_internal_user_codes()
    return snapshot in internal or current in internal


def department_tracking_enabled(booking) -> bool:
    """New-work check for the booking's equipment department (test-users-only applies to the booking's user)."""
    equipment = getattr(booking, "equipment", None)
    dept_id = getattr(equipment, "internal_department_id", None)
    if not dept_id:
        return False
    return module_access.department_allows(dept_id, MODULE, getattr(booking, "user", None))


def department_tracking_on(department_id) -> bool:
    """Department switch only (no user): used for lookups of shipments already registered."""
    if not department_id:
        return False
    return module_access.cell(department_id, MODULE).enabled


def tracking_enabled_since(department_id):
    """When Courier Tracking was last switched on for the department (the seeding or the latest off → on change in
    the module audit log), else when its enabled row was created. None when unknown: callers then fail closed."""
    from iic_booking.department_modules.models import DepartmentModuleAuditLog, DepartmentModuleSetting

    if not department_id:
        return None
    turned_on = (
        DepartmentModuleAuditLog.objects.filter(department_id=department_id, module_key=MODULE, new_value__enabled=True)
        .exclude(old_value__enabled=True)
        .order_by("-created_at", "-id")
        .values_list("created_at", flat=True)
        .first()
    )
    if turned_on:
        return turned_on
    return (
        DepartmentModuleSetting.objects.filter(department_id=department_id, module_key=MODULE, enabled=True)
        .values_list("created_at", flat=True)
        .first()
    )


def sample_with_lab(booking) -> bool:
    """The lab has recorded the sample beyond the user's own "Sample Sent" (held, forwarded, accepted, ...)."""
    return booking.sample_trace_events.exclude(status__in=USER_TRACE_STATUSES).exists()


def arrival_eligibility(user, booking) -> Eligibility:
    reasons: list[str] = []
    owner = bool(user and booking and booking.user_id == user.id)
    if not owner:
        reasons.append("Only the person who made the booking can say how the sample will arrive.")
    status_open = str(getattr(booking, "status", "")) in ARRIVAL_OPEN_BOOKING_STATUSES
    if not status_open:
        reasons.append("This booking is no longer waiting for a sample.")
    # Once the lab has recorded the sample, a new declaration would put "Sample Sent" after the lab's own event and
    # make the booking look not yet received (and eligible for the automatic operator-unavailable refund).
    with_lab = bool(status_open and booking is not None and booking.pk and sample_with_lab(booking))
    if with_lab:
        reasons.append("The lab has already recorded this sample, so how it arrives can no longer be changed.")
    may_declare = owner and status_open and not with_lab
    external = booking_is_external(booking)
    enabled = department_tracking_enabled(booking) if external else False
    courier_reason = ""
    if not external:
        courier_reason = (
            "IIT Roorkee users bring samples to the lab in person, so courier tracking is not used."
            if booking_is_internal(booking)
            else "Courier tracking is available only for bookings made by external users."
        )
    elif not enabled:
        courier_reason = "Courier tracking is not available for this equipment's department. Choose manual delivery."
    return Eligibility(
        may_declare=may_declare,
        manual_allowed=may_declare,
        courier_allowed=may_declare and external and enabled,
        is_external=external,
        department_enabled=enabled,
        reasons=reasons,
        courier_reason=courier_reason,
    )


def user_is_external(user) -> bool:
    return (getattr(user, "user_type", "") or "") in UserType.get_external_user_codes()

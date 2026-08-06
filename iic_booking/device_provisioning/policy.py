"""Department provisioning policy evaluation (Phase R.2.3)."""

from __future__ import annotations

import ipaddress
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.db.models.signals import post_save
from django.dispatch import receiver

from iic_booking.device_provisioning.models import (
    DeviceLifecycle,
    DepartmentProvisioningPolicy,
    ProvisionedDevice,
    ProvisioningMode,
)
from iic_booking.users.models.user_type import UserType

# Department Admin or higher (and Main Admin / superuser).
_PROVISIONING_USER_TYPES = {
    UserType.ADMIN,
    UserType.DEPT_ADMIN,
    UserType.MANAGER,  # Officer In Charge
}

_BLOCKING_LIFECYCLES = {
    DeviceLifecycle.ACTIVE,
    DeviceLifecycle.PROVISIONING,
    DeviceLifecycle.SUSPENDED,
    DeviceLifecycle.PENDING_APPROVAL,
}


@dataclass(frozen=True)
class AutoApproveDecision:
    allow: bool
    reason: str
    mode: str
    department_id: int | None = None
    policy_id: int | None = None
    detail: dict[str, Any] | None = None


def default_mode_for_new_department() -> str:
    """IIT Roorkee default for newly created departments."""
    return ProvisioningMode.TRUSTED_AUTO_APPROVE


def get_policy(department) -> DepartmentProvisioningPolicy | None:
    if department is None:
        return None
    return DepartmentProvisioningPolicy.objects.filter(department_id=department.pk).first()


def ensure_policy_for_new_department(department) -> DepartmentProvisioningPolicy:
    """Create TRUSTED_AUTO_APPROVE policy for a newly created department."""
    policy, _ = DepartmentProvisioningPolicy.objects.get_or_create(
        department=department,
        defaults={"provisioning_mode": default_mode_for_new_department()},
    )
    return policy


def resolve_effective_mode(department) -> str:
    """
    Existing departments without a row: lazy-create TRUSTED_AUTO_APPROVE on first
    authenticated provisioning (see create_session). Until then, treat as MANUAL
    when only inspecting mode without ensuring a row.
    New departments get a TRUSTED row via signal.
    """
    policy = get_policy(department)
    if policy is None:
        return ProvisioningMode.MANUAL_APPROVAL
    return policy.provisioning_mode


def user_has_provisioning_rights(user, department) -> bool:
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    user_type = str(getattr(user, "user_type", "") or "").lower()
    if user_type not in _PROVISIONING_USER_TYPES:
        return False
    if user_type == UserType.ADMIN:
        return True
    user_dept_id = getattr(user, "department_id", None)
    if department is None or user_dept_id is None:
        return False
    return int(user_dept_id) == int(department.pk)


def user_belongs_to_department(user, department) -> bool:
    if user is None or department is None:
        return False
    if getattr(user, "is_superuser", False):
        return True
    user_type = str(getattr(user, "user_type", "") or "").lower()
    if user_type == UserType.ADMIN:
        return True
    return getattr(user, "department_id", None) == department.pk


def _parse_ip(value: str | None):
    if not value:
        return None
    try:
        return ipaddress.ip_address(str(value).strip())
    except ValueError:
        return None


def ip_in_allowed_networks(ip: str | None, cidrs: list | None, *, extra_ips: list | None = None) -> bool:
    """True if client IP or any extra local IP falls in an allowed CIDR."""
    networks = []
    for raw in cidrs or []:
        try:
            networks.append(ipaddress.ip_network(str(raw).strip(), strict=False))
        except ValueError:
            continue
    if not networks:
        return False

    candidates = []
    parsed = _parse_ip(ip)
    if parsed is not None:
        candidates.append(parsed)
    for extra in extra_ips or []:
        p = _parse_ip(str(extra))
        if p is not None:
            candidates.append(p)

    for addr in candidates:
        for net in networks:
            if addr in net:
                return True
    return False


def find_blocking_device(*, fingerprint: str, device_type: str) -> ProvisionedDevice | None:
    if not fingerprint:
        return None
    return (
        ProvisionedDevice.objects.filter(
            fingerprint=fingerprint,
            device_type=device_type,
            lifecycle__in=_BLOCKING_LIFECYCLES,
        )
        .order_by("-updated_at")
        .first()
    )


def find_revoked_device(*, fingerprint: str, device_type: str) -> ProvisionedDevice | None:
    if not fingerprint:
        return None
    return (
        ProvisionedDevice.objects.filter(
            fingerprint=fingerprint,
            device_type=device_type,
            lifecycle=DeviceLifecycle.REVOKED,
        )
        .order_by("-updated_at")
        .first()
    )


def evaluate_auto_approve(
    *,
    user,
    department,
    policy: DepartmentProvisioningPolicy | None,
    fingerprint: str,
    client_ip: str | None,
    local_ips: list | None,
    device_type: str,
    mfa_satisfied: bool = False,
) -> AutoApproveDecision:
    """
    Decide whether portal may auto-approve a DSA provisioning session.

    Auto approval never bypasses authentication — caller must pass an authenticated user.
    """
    mode = policy.provisioning_mode if policy else ProvisioningMode.MANUAL_APPROVAL
    policy_id = policy.id if policy else None
    dept_id = department.pk if department else None

    if mode == ProvisioningMode.MANUAL_APPROVAL:
        return AutoApproveDecision(
            allow=False,
            reason="manual_approval_required",
            mode=mode,
            department_id=dept_id,
            policy_id=policy_id,
        )

    if mode == ProvisioningMode.DEVICE_CODE:
        return AutoApproveDecision(
            allow=False,
            reason="device_code_required",
            mode=mode,
            department_id=dept_id,
            policy_id=policy_id,
        )

    if user is None or not getattr(user, "is_authenticated", False):
        return AutoApproveDecision(
            allow=False,
            reason="administrator_not_authenticated",
            mode=mode,
            department_id=dept_id,
            policy_id=policy_id,
        )

    if department is None:
        return AutoApproveDecision(
            allow=False,
            reason="department_required",
            mode=mode,
            department_id=None,
            policy_id=policy_id,
        )

    if not user_belongs_to_department(user, department):
        return AutoApproveDecision(
            allow=False,
            reason="administrator_not_in_department",
            mode=mode,
            department_id=dept_id,
            policy_id=policy_id,
        )

    if not user_has_provisioning_rights(user, department):
        return AutoApproveDecision(
            allow=False,
            reason="insufficient_provisioning_rights",
            mode=mode,
            department_id=dept_id,
            policy_id=policy_id,
        )

    require_fp = True if policy is None else bool(policy.require_device_fingerprint)
    if require_fp and not (fingerprint or "").strip():
        return AutoApproveDecision(
            allow=False,
            reason="fingerprint_required",
            mode=mode,
            department_id=dept_id,
            policy_id=policy_id,
        )

    blocking = find_blocking_device(fingerprint=fingerprint, device_type=device_type)
    if blocking is not None:
        return AutoApproveDecision(
            allow=False,
            reason="duplicate_active_device",
            mode=mode,
            department_id=dept_id,
            policy_id=policy_id,
            detail={"existing_device_id": str(blocking.id), "lifecycle": blocking.lifecycle},
        )

    revoked = find_revoked_device(fingerprint=fingerprint, device_type=device_type)
    if revoked is not None and policy is not None and not policy.auto_approve_existing_reinstalls:
        return AutoApproveDecision(
            allow=False,
            reason="reinstall_requires_manual_approval",
            mode=mode,
            department_id=dept_id,
            policy_id=policy_id,
            detail={"revoked_device_id": str(revoked.id)},
        )

    if policy is not None and policy.require_mfa and not mfa_satisfied:
        return AutoApproveDecision(
            allow=False,
            reason="mfa_required",
            mode=mode,
            department_id=dept_id,
            policy_id=policy_id,
        )

    if mode == ProvisioningMode.RESTRICTED_AUTO_APPROVE:
        cidrs = list(policy.allowed_networks or []) if policy else []
        if not ip_in_allowed_networks(client_ip, cidrs, extra_ips=local_ips):
            return AutoApproveDecision(
                allow=False,
                reason="network_not_allowed",
                mode=mode,
                department_id=dept_id,
                policy_id=policy_id,
                detail={"client_ip": client_ip, "allowed_networks": cidrs},
            )

    if mode not in {
        ProvisioningMode.TRUSTED_AUTO_APPROVE,
        ProvisioningMode.RESTRICTED_AUTO_APPROVE,
    }:
        return AutoApproveDecision(
            allow=False,
            reason="policy_does_not_permit_auto_approve",
            mode=mode,
            department_id=dept_id,
            policy_id=policy_id,
        )

    return AutoApproveDecision(
        allow=True,
        reason="trusted_auto_approve" if mode == ProvisioningMode.TRUSTED_AUTO_APPROVE else "restricted_auto_approve",
        mode=mode,
        department_id=dept_id,
        policy_id=policy_id,
        detail={
            "fingerprint": fingerprint,
            "client_ip": client_ip,
            "administrator": getattr(user, "email", None) or getattr(user, "username", None),
            "reinstall": bool(revoked),
        },
    )


def generate_device_code() -> str:
    """Human-friendly device code (XXXX-XXXX)."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    left = "".join(secrets.choice(alphabet) for _ in range(4))
    right = "".join(secrets.choice(alphabet) for _ in range(4))
    return f"{left}-{right}"


def device_code_ttl() -> timedelta:
    return timedelta(minutes=30)


def pending_ttl_from_policy(policy: DepartmentProvisioningPolicy | None) -> timedelta:
    hours = 24
    if policy is not None and policy.maximum_pending_lifetime_hours:
        hours = max(1, int(policy.maximum_pending_lifetime_hours))
    return timedelta(hours=hours)


def _connect_department_signal():
    from iic_booking.users.models import Department

    @receiver(post_save, sender=Department, dispatch_uid="device_provisioning_dept_policy")
    def _create_policy_for_new_department(sender, instance, created, **kwargs):
        if created:
            ensure_policy_for_new_department(instance)


_connect_department_signal()

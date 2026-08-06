"""DSA installer services: credential gate, equipment linking."""

from __future__ import annotations

import hashlib
from typing import Any

from django.db import transaction
from django.utils import timezone

from iic_booking.sync.services.tokens import verify_hash


def sha256_filefield(file_field) -> str:
    if not file_field:
        return ""
    h = hashlib.sha256()
    file_field.open("rb")
    try:
        for chunk in file_field.chunks():
            h.update(chunk)
    finally:
        file_field.close()
    return h.hexdigest()


def _extract_credentials(request) -> tuple[str, str, str]:
    data = request.data if hasattr(request, "data") and isinstance(request.data, dict) else {}
    agent_uuid = (
        request.META.get("HTTP_X_AGENT_UUID")
        or request.headers.get("X-Agent-UUID")
        or data.get("agent_uuid")
        or data.get("agentUuid")
        or ""
    )
    secret = (
        request.META.get("HTTP_X_ENROLLMENT_SECRET")
        or request.headers.get("X-Enrollment-Secret")
        or data.get("enrollment_secret")
        or data.get("enrollmentSecret")
        or ""
    )
    auth_header = ""
    try:
        auth_header = request.META.get("HTTP_AUTHORIZATION") or request.headers.get("Authorization") or ""
    except Exception:
        auth_header = ""
    access_token = ""
    if isinstance(auth_header, str) and auth_header.lower().startswith("bearer "):
        access_token = auth_header.split(" ", 1)[1].strip()
    # Prefer dedicated header — some reverse proxies strip Authorization on GET.
    if not access_token:
        access_token = (
            request.META.get("HTTP_X_AGENT_ACCESS_TOKEN")
            or request.headers.get("X-Agent-Access-Token")
            or ""
        ).strip()
    if not access_token:
        access_token = str(data.get("access_token") or data.get("accessToken") or "").strip()
    return str(agent_uuid).strip(), str(secret).strip(), access_token


def resolve_installer_agent(request, *, allow_access_token: bool = True) -> tuple[bool, str, Any]:
    """
    Resolve DSA agent for installer bootstrap (legacy + post-claim).

    1. Agent UUID + enrollment secret (break-glass / upgrades)
    2. Agent UUID + Bearer / X-Agent-Access-Token (zero-touch claim)
    3. Access token alone via ProvisionedDevice bridge (R.2.x zero-touch)
    """
    from iic_booking.sync.models import AgentLifecycleStatus, DepartmentSyncAgent

    agent_uuid, secret, access_token = _extract_credentials(request)

    agent = None
    if agent_uuid:
        agent = DepartmentSyncAgent.objects.filter(agent_uuid=agent_uuid).first()
        if agent is None:
            return False, "Invalid agent credentials.", None
    elif allow_access_token and access_token:
        # Prefer Device Provisioning bridge (O(1) prefix lookup) over scanning DSA rows.
        try:
            from iic_booking.device_provisioning.services import authenticate_device_token

            device = authenticate_device_token(access_token)
        except Exception:
            device = None
        if device is not None and getattr(device, "legacy_dsa_id", None):
            agent = DepartmentSyncAgent.objects.filter(pk=device.legacy_dsa_id).first()
        if agent is None and device is not None:
            agent = DepartmentSyncAgent.objects.filter(agent_uuid=device.id).first()
        if agent is None:
            return False, "Invalid or expired access token.", None
    else:
        return False, "Agent UUID and enrollment secret (or access token) are required.", None

    if agent.status in {AgentLifecycleStatus.DISABLED, AgentLifecycleStatus.REVOKED}:
        return False, "Agent is disabled or revoked.", None

    if secret:
        if not agent.enrollment_token_hash or not verify_hash(secret, agent.enrollment_token_hash):
            return False, "Invalid agent credentials.", None
        return True, "", agent

    if allow_access_token and access_token:
        if not agent.access_token_hash or not verify_hash(access_token, agent.access_token_hash):
            return False, "Invalid or expired access token.", None
        if agent.access_token_expires_at and agent.access_token_expires_at < timezone.now():
            return False, "Invalid or expired access token.", None
        return True, "", agent

    return False, "Agent UUID and enrollment secret (or access token) are required.", None


def build_equipment_tree_for_department(department_id: int | None = None) -> dict:
    """Shared department→equipment tree shape for DSA installer / provisioning."""
    from iic_booking.equipment.models import Equipment

    equipment_qs = Equipment.objects.select_related("internal_department").order_by("name")
    if department_id is not None:
        scoped = equipment_qs.filter(internal_department_id=department_id)
        if scoped.exists():
            equipment_qs = scoped

    by_dept: dict[str, dict] = {}
    for eq in equipment_qs[:500]:
        dept = eq.internal_department
        dept_id = str(dept.id) if dept else "unassigned"
        dept_name = dept.name if dept else "Unassigned"
        if dept_id not in by_dept:
            by_dept[dept_id] = {
                "id": dept_id,
                "name": dept_name,
                "equipment": [],
            }
        lab_name = ""
        lab = getattr(eq, "laboratory", None) or getattr(eq, "lab", None)
        if lab is not None:
            lab_name = getattr(lab, "name", "") or str(lab)
        by_dept[dept_id]["equipment"].append(
            {
                "id": eq.pk,
                "name": eq.name,
                "code": getattr(eq, "code", "") or "",
                "laboratory": lab_name,
            }
        )

    return {
        "count": len(by_dept),
        "departments": list(by_dept.values()),
    }


@transaction.atomic
def link_agent_to_equipment(
    *,
    agent,
    equipment,
    result_folder: str = "",
    unc_path: str = "",
    watch_folder: str = "",
    hostname: str = "",
    ip_address: str = "",
    share_name: str = "",
) -> dict[str, Any]:
    """Create/update EquipmentSyncProfile + AgentAssignment; set agent.equipment."""
    from iic_booking.sync.models import AgentAssignment, EquipmentSyncProfile

    profile, profile_created = EquipmentSyncProfile.objects.get_or_create(
        equipment=equipment,
        defaults={
            "primary_agent": agent,
            "hostname": hostname or "",
            "ip_address": ip_address or "",
            "share_name": share_name or "",
            "unc_path": unc_path or "",
            "watch_folder": watch_folder or result_folder or "",
            "sync_enabled": True,
            "watch_enabled": True,
            "upload_enabled": True,
        },
    )

    update_fields: list[str] = []
    if profile.primary_agent_id != agent.id:
        profile.primary_agent = agent
        update_fields.append("primary_agent")
    if hostname and profile.hostname != hostname:
        profile.hostname = hostname
        update_fields.append("hostname")
    if ip_address and profile.ip_address != ip_address:
        profile.ip_address = ip_address
        update_fields.append("ip_address")
    if share_name and profile.share_name != share_name:
        profile.share_name = share_name
        update_fields.append("share_name")
    if unc_path and profile.unc_path != unc_path:
        profile.unc_path = unc_path
        update_fields.append("unc_path")
    folder = (watch_folder or result_folder or "").strip()
    if folder and profile.watch_folder != folder:
        profile.watch_folder = folder
        update_fields.append("watch_folder")
    if not profile.sync_enabled:
        profile.sync_enabled = True
        update_fields.append("sync_enabled")
    if not profile.watch_enabled:
        profile.watch_enabled = True
        update_fields.append("watch_enabled")
    if not getattr(profile, "upload_enabled", True):
        profile.upload_enabled = True
        update_fields.append("upload_enabled")
    if update_fields:
        profile.configuration_version = (profile.configuration_version or 0) + 1
        update_fields.extend(["configuration_version", "updated_at"])
        profile.save(update_fields=list(dict.fromkeys(update_fields)))

    AgentAssignment.objects.filter(sync_profile=profile, is_active=True).exclude(
        sync_agent=agent
    ).update(is_active=False, unassigned_at=timezone.now())
    assignment, assignment_created = AgentAssignment.objects.update_or_create(
        sync_agent=agent,
        sync_profile=profile,
        defaults={"is_active": True, "unassigned_at": None, "notes": "Linked by DSA installer"},
    )

    if agent.equipment_id != equipment.pk:
        agent.equipment = equipment
        agent.save(update_fields=["equipment", "updated_at"])

    return {
        "profile_id": str(profile.id),
        "profile_created": profile_created,
        "assignment_id": str(assignment.id),
        "assignment_created": assignment_created,
        "agent_id": str(agent.id),
        "equipment_id": equipment.pk,
        "watch_folder": profile.watch_folder,
        "unc_path": profile.unc_path,
    }

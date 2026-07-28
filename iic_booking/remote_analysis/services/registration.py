"""Registration service — register / update workstations and issue tokens."""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.utils import timezone

from iic_booking.remote_analysis.constants import AuditCategory, WorkstationStatus
from iic_booking.remote_analysis.models import (
    AnalysisWorkstation,
    WorkstationCapability,
    WorkstationInventory,
    WorkstationStateHistory,
)
from iic_booking.remote_analysis.services.audit import record_event
from iic_booking.remote_analysis.services.health import update_workstation_health
from iic_booking.remote_analysis.services.tokens import issue_agent_token, revoke_all_tokens


def _transition(workstation: AnalysisWorkstation, to_status: str, reason: str = "") -> None:
    from_status = workstation.status
    if from_status == to_status:
        return
    workstation.status = to_status
    WorkstationStateHistory.objects.create(
        workstation=workstation,
        from_status=from_status,
        to_status=to_status,
        reason=reason,
    )


def _apply_workstation_payload(workstation: AnalysisWorkstation, payload: dict[str, Any]) -> None:
    mapping = {
        "hostname": "hostname",
        "computerName": "hostname",
        "displayName": "display_name",
        "department": "department_name",
        "building": "building",
        "room": "room",
        "description": "description",
        "operatingSystem": "operating_system",
        "windowsVersion": "windows_version",
        "cpuModel": "cpu",
        "cpu": "cpu",
        "cpuCores": "cpu_cores",
        "memoryGB": "memory_gb",
        "memory": "memory_gb",
        "storageGB": "storage_gb",
        "storage": "storage_gb",
        "gpu": "gpu",
        "GPU": "gpu",
        "ipAddress": "ip_address",
        "IPAddress": "ip_address",
        "macAddress": "mac_address",
        "MACAddress": "mac_address",
        "agentVersion": "agent_version",
        "schemaVersion": "schema_version",
        "supportsRDP": "supports_rdp",
        "supportsClipboard": "supports_clipboard",
        "supportsFileTransfer": "supports_file_transfer",
        "supportsAudio": "supports_audio",
        "supportsMultiMonitor": "supports_multi_monitor",
    }
    for src, dest in mapping.items():
        if src in payload and payload[src] is not None:
            setattr(workstation, dest, payload[src])

    if not workstation.display_name:
        workstation.display_name = workstation.hostname or workstation.agent_id


def _upsert_capabilities(workstation: AnalysisWorkstation) -> WorkstationCapability:
    caps, _ = WorkstationCapability.objects.get_or_create(workstation=workstation)
    caps.supports_rdp = workstation.supports_rdp
    caps.supports_clipboard = workstation.supports_clipboard
    caps.supports_file_transfer = workstation.supports_file_transfer
    caps.supports_audio = workstation.supports_audio
    caps.supports_multi_monitor = workstation.supports_multi_monitor
    caps.gpu_available = bool(workstation.gpu)
    caps.ram_gb = workstation.memory_gb
    caps.cpu_cores = workstation.cpu_cores
    caps.disk_space_gb = workstation.storage_gb
    caps.save()
    return caps


class RegistrationService:
    """Register workstation, validate AgentId, issue token, prevent duplicates."""

    @transaction.atomic
    def register(self, payload: dict[str, Any]) -> dict[str, Any]:
        workstation_data = payload.get("workstation") or payload
        agent_id = (
            workstation_data.get("agentId")
            or workstation_data.get("agent_id")
            or payload.get("agentId")
            or payload.get("agent_id")
        )
        if not agent_id:
            raise ValueError("agentId is required")

        agent_id = str(agent_id).strip()
        existing = AnalysisWorkstation.objects.select_for_update().filter(agent_id=agent_id).first()
        created = existing is None
        workstation = existing or AnalysisWorkstation(agent_id=agent_id)

        _apply_workstation_payload(workstation, workstation_data)
        now = timezone.now()

        if created:
            workstation.registration_date = now
            _transition(workstation, WorkstationStatus.REGISTERING, "Initial registration")
            workstation.save()
            WorkstationInventory.objects.get_or_create(workstation=workstation)
            _upsert_capabilities(workstation)
            _transition(workstation, WorkstationStatus.ONLINE, "Registration accepted")
            workstation.save(update_fields=["status", "updated_at"])
            _transition(workstation, WorkstationStatus.AVAILABLE, "Ready")
            workstation.save(update_fields=["status", "updated_at"])
            revoke_all_tokens(workstation)
            token_row, plaintext = issue_agent_token(workstation)
            record_event(
                category=AuditCategory.REGISTRATION,
                action="Registered",
                details=f"New workstation {agent_id}",
                workstation=workstation,
                correlation_id=agent_id,
            )
        else:
            # Prevent duplicate registration — refresh metadata, rotate token optionally
            _apply_workstation_payload(workstation, workstation_data)
            if workstation.status in {WorkstationStatus.OFFLINE, WorkstationStatus.ERROR, WorkstationStatus.UNKNOWN}:
                _transition(workstation, WorkstationStatus.ONLINE, "Re-registration contact")
            workstation.save()
            _upsert_capabilities(workstation)
            # Keep existing active token; issue new only if none active
            active = workstation.tokens.filter(is_active=True).first()
            if active is None:
                token_row, plaintext = issue_agent_token(workstation)
            else:
                token_row, plaintext = active, ""
            record_event(
                category=AuditCategory.REGISTRATION,
                action="Updated",
                details=f"Existing workstation {agent_id} updated (no duplicate)",
                workstation=workstation,
                correlation_id=agent_id,
            )

        update_workstation_health(workstation)
        return {
            "accepted": True,
            "created": created,
            "workstation_id": str(workstation.id),
            "agent_id": workstation.agent_id,
            "status": workstation.status,
            "token": plaintext or None,
            "token_expires_at": token_row.expires_at.isoformat() if token_row and token_row.expires_at else None,
            "message": "Registered" if created else "Already registered; metadata updated",
        }

"""Availability engine — determine whether a workstation is eligible for allocation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from django.db.models import Q
from django.utils import timezone

from iic_booking.remote_analysis.constants import (
    HEARTBEAT_TIMEOUT_FOR_RESERVATION_SECONDS,
    MIN_HEALTH_SCORE_FOR_ALLOCATION,
    NON_OPERATIONAL_STATUSES,
    ReservationStatus,
    WorkstationStatus,
)
from iic_booking.remote_analysis.models import (
    AnalysisWorkstation,
    InstalledSoftware,
    MaintenanceWindow,
    SoftwareLicense,
    WorkstationHeartbeat,
)
from iic_booking.remote_analysis.scheduler_models import AnalysisReservation, SoftwareRequirement


BLOCKING_STATUSES = set(NON_OPERATIONAL_STATUSES) | {
    WorkstationStatus.BUSY,
    WorkstationStatus.PREPARING,
    WorkstationStatus.RESERVED,
}


@dataclass
class AvailabilityResult:
    available: bool
    reasons: list[str] = field(default_factory=list)
    workstation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reasons": self.reasons,
            "workstation_id": self.workstation_id,
        }


class AvailabilityEngine:
    """Evaluate workstation availability for a time window and optional requirements."""

    def is_under_maintenance(
        self,
        workstation: AnalysisWorkstation,
        start: datetime,
        end: datetime,
    ) -> bool:
        return MaintenanceWindow.objects.filter(
            active=True,
            start__lt=end,
            end__gt=start,
        ).filter(Q(workstation=workstation) | Q(workstation__isnull=True)).exists()

    def has_reservation_overlap(
        self,
        workstation: AnalysisWorkstation,
        start: datetime,
        end: datetime,
        *,
        exclude_reservation_id=None,
    ) -> bool:
        active = {
            ReservationStatus.RESERVED,
            ReservationStatus.AWAITING_CHECKIN,
            ReservationStatus.PREPARING,
            ReservationStatus.READY,
            ReservationStatus.ACTIVE,
        }
        qs = AnalysisReservation.objects.filter(
            workstation=workstation,
            status__in=active,
            reserved_start__lt=end,
            reserved_end__gt=start,
        )
        if exclude_reservation_id:
            qs = qs.exclude(pk=exclude_reservation_id)
        return qs.exists()

    def _has_usable_agent_token(self, workstation: AnalysisWorkstation) -> bool:
        active = workstation.tokens.filter(is_active=True).order_by("-issued_at").first()
        if active is None:
            return False
        if active.expires_at and active.expires_at < timezone.now():
            return False
        return True

    def heartbeat_fresh(self, workstation: AnalysisWorkstation) -> bool:
        """True only when a recent heartbeat was received (agent is actively polling)."""
        if workstation.last_heartbeat is None:
            return False
        age = (timezone.now() - workstation.last_heartbeat).total_seconds()
        return age <= HEARTBEAT_TIMEOUT_FOR_RESERVATION_SECONDS

    def agent_online(self, workstation: AnalysisWorkstation) -> bool:
        """
        True when the agent is considered reachable for allocation.

        Prefer a fresh heartbeat. If status is already AVAILABLE/ONLINE and an
        active agent token exists, allow allocation even when heartbeat is missing
        or briefly stale (common when agents restart or heartbeats lag).
        """
        if self.heartbeat_fresh(workstation):
            return True

        if workstation.status in {
            WorkstationStatus.AVAILABLE,
            WorkstationStatus.ONLINE,
            WorkstationStatus.BUSY,
            WorkstationStatus.PREPARING,
            WorkstationStatus.RESERVED,
            WorkstationStatus.CLEANING,
        } and workstation.enabled:
            return self._has_usable_agent_token(workstation)
        return False

    def token_expired(self, workstation: AnalysisWorkstation) -> bool:
        active = workstation.tokens.filter(is_active=True).order_by("-issued_at").first()
        if active is None:
            return True
        if active.expires_at and active.expires_at < timezone.now():
            return True
        return False

    def software_matches(
        self,
        workstation: AnalysisWorkstation,
        requirement: SoftwareRequirement | None,
    ) -> tuple[bool, list[str]]:
        if requirement is None:
            return True, []
        reasons: list[str] = []
        if requirement.software:
            qs = InstalledSoftware.objects.filter(
                workstation=workstation,
                is_present=True,
                allocation_enabled=True,
                software_name__icontains=requirement.software,
            )
            if requirement.minimum_version:
                # Simple lexical/version compare — accept if any version >= requested when numeric-ish
                found = False
                for row in qs:
                    if _version_gte(row.version, requirement.minimum_version):
                        found = True
                        break
                if not found:
                    reasons.append(
                        f"Missing software {requirement.software} >= {requirement.minimum_version}"
                    )
            elif not qs.exists():
                if requirement.required:
                    reasons.append(f"Missing required software {requirement.software}")
        if requirement.license_required and requirement.software:
            lic_ok = SoftwareLicense.objects.filter(
                workstation=workstation,
                software__icontains=requirement.software,
            ).exclude(status__iexact="Expired").exists()
            if not lic_ok:
                reasons.append(f"License required for {requirement.software}")
        if requirement.gpu_required and not (workstation.gpu or "").strip():
            reasons.append("GPU required")
        if requirement.minimum_ram_gb and workstation.memory_gb < requirement.minimum_ram_gb:
            reasons.append(f"RAM {workstation.memory_gb} < {requirement.minimum_ram_gb}")
        if requirement.minimum_storage_gb and workstation.storage_gb < requirement.minimum_storage_gb:
            reasons.append(f"Storage {workstation.storage_gb} < {requirement.minimum_storage_gb}")
        if requirement.minimum_cpu_cores and workstation.cpu_cores < requirement.minimum_cpu_cores:
            reasons.append(f"CPU cores {workstation.cpu_cores} < {requirement.minimum_cpu_cores}")
        if requirement.operating_system and requirement.operating_system.lower() not in (
            workstation.operating_system or ""
        ).lower():
            reasons.append(f"OS mismatch ({requirement.operating_system})")
        return len(reasons) == 0, reasons

    def capability_matches(
        self,
        workstation: AnalysisWorkstation,
        requested: dict[str, Any] | None,
    ) -> tuple[bool, list[str]]:
        if not requested:
            return True, []
        reasons: list[str] = []
        caps = getattr(workstation, "capabilities", None)
        mapping = {
            "supports_rdp": ("supports_rdp", workstation.supports_rdp),
            "supports_clipboard": ("supports_clipboard", workstation.supports_clipboard),
            "supports_file_transfer": ("supports_file_transfer", workstation.supports_file_transfer),
            "supports_audio": ("supports_audio", workstation.supports_audio),
            "supports_multi_monitor": ("supports_multi_monitor", workstation.supports_multi_monitor),
            "gpu": ("gpu", bool(workstation.gpu)),
        }
        for key, (attr, fallback) in mapping.items():
            if not requested.get(key):
                continue
            value = getattr(caps, attr, fallback) if caps is not None and hasattr(caps, attr) else fallback
            if not value:
                reasons.append(f"Capability missing: {key}")
        resources = requested.get("resources") or {}
        if resources.get("min_ram_gb") and workstation.memory_gb < float(resources["min_ram_gb"]):
            reasons.append("Insufficient RAM")
        if resources.get("min_cpu_cores") and workstation.cpu_cores < int(resources["min_cpu_cores"]):
            reasons.append("Insufficient CPU cores")
        if resources.get("min_storage_gb") and workstation.storage_gb < float(resources["min_storage_gb"]):
            reasons.append("Insufficient storage")
        return len(reasons) == 0, reasons

    def evaluate(
        self,
        workstation: AnalysisWorkstation,
        start: datetime,
        end: datetime,
        *,
        requirement: SoftwareRequirement | None = None,
        requested_capabilities: dict | None = None,
        exclude_reservation_id=None,
    ) -> AvailabilityResult:
        reasons: list[str] = []
        wid = str(workstation.id)

        if not workstation.enabled:
            reasons.append("Workstation disabled")
        if workstation.status in BLOCKING_STATUSES:
            reasons.append(f"Status {workstation.status} not allocatable")
        if workstation.health_score < MIN_HEALTH_SCORE_FOR_ALLOCATION:
            reasons.append(f"Health score {workstation.health_score} below threshold")
        if not self.agent_online(workstation):
            reasons.append("Agent offline / heartbeat timeout")
        if self.token_expired(workstation):
            reasons.append("Agent token expired or missing")
        # R11 stale inventory safety — prefer correct allocation over optimistic.
        from iic_booking.remote_analysis.constants import INVENTORY_STALE_SECONDS

        inv_at = getattr(workstation, "last_inventory_update", None)
        if inv_at is None:
            reasons.append("Software inventory never published")
        else:
            age = (timezone.now() - inv_at).total_seconds()
            if age > INVENTORY_STALE_SECONDS:
                reasons.append("Software inventory stale")
        if self.is_under_maintenance(workstation, start, end):
            reasons.append("Maintenance window")
        if self.has_reservation_overlap(workstation, start, end, exclude_reservation_id=exclude_reservation_id):
            reasons.append("Existing reservation overlap")

        soft_ok, soft_reasons = self.software_matches(workstation, requirement)
        reasons.extend(soft_reasons)
        cap_ok, cap_reasons = self.capability_matches(workstation, requested_capabilities)
        reasons.extend(cap_reasons)

        # Current load hint
        latest = (
            WorkstationHeartbeat.objects.filter(workstation=workstation)
            .order_by("-received_at")
            .first()
        )
        if latest and latest.cpu >= 95:
            reasons.append("CPU saturated")

        # Hard-require ALL named softwares (equipment catalog / workflow).
        # Soft scoring alone is insufficient — incomplete coverage must not allocate.
        required_names = []
        if requested_capabilities:
            raw_names = requested_capabilities.get("required_software_names") or []
            if isinstance(raw_names, (list, tuple)):
                required_names = [str(n).strip() for n in raw_names if str(n).strip()]
        if required_names:
            from iic_booking.remote_analysis.models import InstalledSoftware

            missing = [
                name
                for name in required_names
                if not InstalledSoftware.objects.filter(
                    workstation=workstation,
                    is_present=True,
                    allocation_enabled=True,
                    software_name__icontains=name,
                ).exists()
            ]
            if missing:
                reasons.append(
                    "Missing required software: " + ", ".join(missing)
                )

        available = len(reasons) == 0
        # Busy workstations remain ineligible via overlap / status checks above;
        # surface a clearer reason when status is BUSY without overlap text.
        if (
            not available
            and workstation.status == WorkstationStatus.BUSY
            and not any("reservation overlap" in r.lower() for r in reasons)
        ):
            if "Busy" not in reasons and not any(r.startswith("Status") for r in reasons):
                reasons.append("Workstation busy")

        return AvailabilityResult(available=available, reasons=reasons, workstation_id=wid)

    def list_available(
        self,
        start: datetime,
        end: datetime,
        *,
        department_id: int | None = None,
        requirement: SoftwareRequirement | None = None,
        requested_capabilities: dict | None = None,
    ) -> list[tuple[AnalysisWorkstation, AvailabilityResult]]:
        qs = AnalysisWorkstation.objects.select_related("capabilities", "department").filter(enabled=True)
        if department_id is not None:
            qs = qs.filter(Q(department_id=department_id) | Q(department_id__isnull=True))
        results = []
        for ws in qs:
            result = self.evaluate(
                ws,
                start,
                end,
                requirement=requirement,
                requested_capabilities=requested_capabilities,
            )
            if result.available:
                results.append((ws, result))
        return results


def _version_gte(installed: str, minimum: str) -> bool:
    if not minimum:
        return True
    if not installed:
        return False
    try:
        inst = [int(p) for p in installed.split(".") if p.isdigit() or p.isnumeric()]
        req = [int(p) for p in minimum.split(".") if p.isdigit() or p.isnumeric()]
        if not req:
            return installed.lower() >= minimum.lower()
        # pad
        length = max(len(inst), len(req))
        inst += [0] * (length - len(inst))
        req += [0] * (length - len(req))
        return inst >= req
    except Exception:
        return installed.lower() >= minimum.lower()

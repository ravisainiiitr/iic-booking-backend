"""Candidate scoring / allocation algorithm."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone

from iic_booking.remote_analysis.constants import DEFAULT_SCORING_WEIGHTS, SessionStatus
from iic_booking.remote_analysis.models import AnalysisWorkstation, SoftwareLicense, WorkstationHeartbeat
from iic_booking.remote_analysis.scheduler_models import (
    AllocationRule,
    AnalysisReservation,
    ReservationPreference,
    SoftwareRequirement,
)
from iic_booking.remote_analysis.services.availability import AvailabilityEngine


@dataclass
class CandidateScore:
    workstation: AnalysisWorkstation
    score: float
    breakdown: dict[str, float]
    available: bool
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "workstation_id": str(self.workstation.id),
            "hostname": self.workstation.hostname,
            "display_name": self.workstation.display_name,
            "score": round(self.score, 2),
            "breakdown": {k: round(v, 2) for k, v in self.breakdown.items()},
            "available": self.available,
            "reasons": self.reasons,
            "health_score": self.workstation.health_score,
            "status": self.workstation.status,
        }


class AllocationService:
    def __init__(self):
        self.availability = AvailabilityEngine()

    def scoring_weights(self, *, department_id: int | None = None, user=None) -> dict[str, float]:
        weights = dict(DEFAULT_SCORING_WEIGHTS)
        rules = AllocationRule.objects.filter(is_active=True)
        if department_id is not None:
            dept_rule = rules.filter(department_id=department_id).order_by("-priority_boost").first()
            if dept_rule:
                weights = dept_rule.effective_weights()
        if user is not None:
            user_rule = rules.filter(user=user).order_by("-priority_boost").first()
            if user_rule:
                weights.update(user_rule.effective_weights())
        return weights

    def priority_boost(self, *, department_id: int | None = None, user=None) -> int:
        boost = 0
        qs = AllocationRule.objects.filter(is_active=True)
        if department_id is not None:
            for rule in qs.filter(department_id=department_id):
                boost += rule.priority_boost
        if user is not None:
            for rule in qs.filter(user=user):
                boost += rule.priority_boost
        return boost

    def score_workstation(
        self,
        workstation: AnalysisWorkstation,
        *,
        start: datetime,
        end: datetime,
        requirement: SoftwareRequirement | None = None,
        requested_capabilities: dict | None = None,
        department_id: int | None = None,
        user=None,
        exclude_reservation_id=None,
        equipment=None,
        pool_boost_by_ws: dict | None = None,
        catalog_max_concurrent: int = 0,
        software_name: str = "",
        required_software_names: list[str] | None = None,
        prefer_workstation_id=None,
    ) -> CandidateScore:
        avail = self.availability.evaluate(
            workstation,
            start,
            end,
            requirement=requirement,
            requested_capabilities=requested_capabilities,
            exclude_reservation_id=exclude_reservation_id,
        )
        reasons = list(avail.reasons)
        available = avail.available

        # License seat / catalog max concurrent (admin-maintained)
        if catalog_max_concurrent and catalog_max_concurrent > 0 and software_name:
            from iic_booking.remote_analysis.session_models import RemoteDesktopSession

            open_statuses = {
                SessionStatus.PREPARING,
                SessionStatus.READY,
                SessionStatus.TOKEN_GENERATED,
                SessionStatus.LAUNCHED,
                SessionStatus.CONNECTING,
                SessionStatus.CONNECTED,
                SessionStatus.ACTIVE,
                SessionStatus.IDLE,
            }
            active = RemoteDesktopSession.objects.filter(
                workstation=workstation,
                status__in=open_statuses,
            ).count()
            if active >= catalog_max_concurrent:
                available = False
                reasons.append("software_concurrent_limit")

        # Optional per-workstation SoftwareLicense.seats
        if software_name:
            lic = (
                SoftwareLicense.objects.filter(workstation=workstation, software__icontains=software_name)
                .order_by("-updated_at")
                .first()
            )
            if lic and lic.seats is not None:
                from iic_booking.remote_analysis.session_models import RemoteDesktopSession

                open_statuses = {
                    SessionStatus.PREPARING,
                    SessionStatus.READY,
                    SessionStatus.TOKEN_GENERATED,
                    SessionStatus.LAUNCHED,
                    SessionStatus.CONNECTING,
                    SessionStatus.CONNECTED,
                    SessionStatus.ACTIVE,
                    SessionStatus.IDLE,
                }
                active = RemoteDesktopSession.objects.filter(
                    workstation=workstation,
                    status__in=open_statuses,
                ).count()
                if active >= int(lic.seats):
                    available = False
                    reasons.append("license_seats_exhausted")

        weights = self.scoring_weights(department_id=department_id, user=user)
        breakdown: dict[str, float] = {}

        breakdown["health_score"] = (workstation.health_score / 100.0) * weights.get("health_score", 0)

        latest = (
            WorkstationHeartbeat.objects.filter(workstation=workstation)
            .order_by("-received_at")
            .first()
        )
        cpu = latest.cpu if latest else 50
        mem = latest.memory if latest else 50
        breakdown["cpu_load"] = max(0.0, (100 - cpu) / 100.0) * weights.get("cpu_load", 0)
        breakdown["memory_usage"] = max(0.0, (100 - mem) / 100.0) * weights.get("memory_usage", 0)

        recent_count = AnalysisReservation.objects.filter(
            workstation=workstation,
            allocated_at__gte=timezone.now() - timedelta(hours=24),
        ).count()
        breakdown["recent_usage"] = max(0.0, 1.0 - min(recent_count, 10) / 10.0) * weights.get(
            "recent_usage", 0
        )

        soft_ok, _ = self.availability.software_matches(workstation, requirement)
        if requirement is None:
            soft_ratio = 1.0
        elif soft_ok:
            soft_ratio = 1.0
        else:
            soft_ratio = 0.2
        breakdown["software_match"] = soft_ratio * weights.get("software_match", 0)

        cap_ok, _ = self.availability.capability_matches(workstation, requested_capabilities)
        breakdown["capability_match"] = (1.0 if cap_ok else 0.3) * weights.get("capability_match", 0)

        affinity = 0.0
        if department_id and workstation.department_id == department_id:
            affinity = 1.0
        elif department_id is None or workstation.department_id is None:
            affinity = 0.5
        breakdown["department_affinity"] = affinity * weights.get("department_affinity", 0)

        idle_minutes = latest.idle_time_minutes if latest else 0
        breakdown["idle_time"] = min(1.0, idle_minutes / 60.0) * weights.get("idle_time", 0)

        # GPU availability score
        has_gpu = bool((workstation.gpu or "").strip()) or bool(
            getattr(getattr(workstation, "capabilities", None), "gpu_available", False)
        )
        breakdown["gpu_score"] = (1.0 if has_gpu else 0.2) * weights.get("gpu_score", 0)

        # Historical performance — prefer higher health + fewer failures in notes proxy via health
        hist = max(0.0, min(1.0, workstation.health_score / 100.0))
        breakdown["historical_performance"] = hist * weights.get("historical_performance", 0)

        # Multi-software coverage (workflow same-PC preference)
        if required_software_names:
            covered = self._software_coverage_ratio(workstation, required_software_names)
            breakdown["multi_software_coverage"] = covered * weights.get("multi_software_coverage", 0)

        if prefer_workstation_id is not None and str(workstation.id) == str(prefer_workstation_id):
            breakdown["same_environment_pin"] = 8.0

        if user is not None:
            pref = ReservationPreference.objects.filter(user=user).first()
            if pref and pref.preferred_workstation_id == workstation.id:
                breakdown["preference"] = 5.0
            elif pref and pref.preferred_building and pref.preferred_building.lower() == (
                workstation.building or ""
            ).lower():
                breakdown["preference"] = 2.0

        # EQUIPMENT_PRIORITY — preferred Analysis PC pool for this equipment
        if pool_boost_by_ws and workstation.id in pool_boost_by_ws:
            breakdown["equipment_priority"] = float(pool_boost_by_ws[workstation.id])
        elif equipment is not None and pool_boost_by_ws is not None and pool_boost_by_ws:
            # Pool configured but this WS not in it — mild penalty (still eligible if empty-pool semantics elsewhere)
            breakdown["equipment_priority"] = 0.0

        total = sum(breakdown.values())
        if not available:
            total *= 0.0

        return CandidateScore(
            workstation=workstation,
            score=total,
            breakdown=breakdown,
            available=available,
            reasons=reasons,
        )

    def _software_coverage_ratio(self, workstation: AnalysisWorkstation, names: list[str]) -> float:
        from iic_booking.remote_analysis.models import InstalledSoftware

        if not names:
            return 1.0
        hits = 0
        for name in names:
            if InstalledSoftware.objects.filter(
                workstation=workstation, is_present=True, software_name__icontains=name
            ).exists():
                hits += 1
        return hits / float(len(names))

    def find_workstation_with_all_software(self, names: list[str]) -> AnalysisWorkstation | None:
        """Return highest-health ONLINE workstation that has every listed software installed."""
        from iic_booking.remote_analysis.constants import WorkstationStatus
        from iic_booking.remote_analysis.models import InstalledSoftware

        if not names:
            return None
        qs = AnalysisWorkstation.objects.filter(
            enabled=True,
            status__in={
                WorkstationStatus.ONLINE,
                WorkstationStatus.AVAILABLE,
                WorkstationStatus.BUSY,
            },
        ).order_by("-health_score")
        for ws in qs:
            if all(
                InstalledSoftware.objects.filter(
                    workstation=ws, is_present=True, software_name__icontains=n
                ).exists()
                for n in names
            ):
                return ws
        return None

    def _pool_boost_map(self, equipment) -> dict:
        if equipment is None:
            return {}
        from iic_booking.remote_analysis.catalog_models import EquipmentAnalysisPool

        return {
            row.workstation_id: float(row.priority_boost)
            for row in EquipmentAnalysisPool.objects.filter(equipment=equipment)
        }

    def rank_candidates(
        self,
        start: datetime,
        end: datetime,
        *,
        department_id: int | None = None,
        requirement: SoftwareRequirement | None = None,
        requested_capabilities: dict | None = None,
        user=None,
        exclude_reservation_id=None,
        include_unavailable: bool = False,
        equipment=None,
        catalog_max_concurrent: int = 0,
        software_name: str = "",
        required_software_names: list[str] | None = None,
        prefer_workstation_id=None,
    ) -> list[CandidateScore]:
        qs = AnalysisWorkstation.objects.select_related("capabilities", "department").filter(enabled=True)
        pool_boost = self._pool_boost_map(equipment)
        # When a pool is defined, prefer scoring all but boost pool members; do not hard-filter
        # (empty pool = global). Hard-filter only if AllocationRule EQUIPMENT_PRIORITY active and pool non-empty.
        if pool_boost and AllocationRule.objects.filter(
            is_active=True, rule_type="EQUIPMENT_PRIORITY"
        ).exists():
            qs = qs.filter(id__in=list(pool_boost.keys()))

        scored = [
            self.score_workstation(
                ws,
                start=start,
                end=end,
                requirement=requirement,
                requested_capabilities=requested_capabilities,
                department_id=department_id,
                user=user,
                exclude_reservation_id=exclude_reservation_id,
                equipment=equipment,
                pool_boost_by_ws=pool_boost,
                catalog_max_concurrent=catalog_max_concurrent,
                software_name=software_name or (requirement.software if requirement else ""),
                required_software_names=required_software_names,
                prefer_workstation_id=prefer_workstation_id,
            )
            for ws in qs
        ]
        if not include_unavailable:
            scored = [s for s in scored if s.available]
        scored.sort(key=lambda s: s.score, reverse=True)
        return scored

    def select_best(self, **kwargs) -> CandidateScore | None:
        ranked = self.rank_candidates(**kwargs)
        return ranked[0] if ranked else None

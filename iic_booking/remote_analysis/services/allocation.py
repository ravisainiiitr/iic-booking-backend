"""Candidate scoring / allocation algorithm."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone

from iic_booking.remote_analysis.constants import DEFAULT_SCORING_WEIGHTS
from iic_booking.remote_analysis.models import AnalysisWorkstation, InstalledSoftware, WorkstationHeartbeat
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
    ) -> CandidateScore:
        avail = self.availability.evaluate(
            workstation,
            start,
            end,
            requirement=requirement,
            requested_capabilities=requested_capabilities,
            exclude_reservation_id=exclude_reservation_id,
        )
        weights = self.scoring_weights(department_id=department_id, user=user)
        breakdown: dict[str, float] = {}

        # Health 0-100 → weighted
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

        # Recent usage — fewer recent reservations is better
        recent_count = AnalysisReservation.objects.filter(
            workstation=workstation,
            allocated_at__gte=timezone.now() - timedelta(hours=24),
        ).count()
        breakdown["recent_usage"] = max(0.0, 1.0 - min(recent_count, 10) / 10.0) * weights.get(
            "recent_usage", 0
        )

        # Software match
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

        # Department affinity
        affinity = 0.0
        if department_id and workstation.department_id == department_id:
            affinity = 1.0
        elif department_id is None or workstation.department_id is None:
            affinity = 0.5
        breakdown["department_affinity"] = affinity * weights.get("department_affinity", 0)

        # Idle time from heartbeat
        idle_minutes = latest.idle_time_minutes if latest else 0
        breakdown["idle_time"] = min(1.0, idle_minutes / 60.0) * weights.get("idle_time", 0)

        # Preference boost
        if user is not None:
            pref = ReservationPreference.objects.filter(user=user).first()
            if pref and pref.preferred_workstation_id == workstation.id:
                breakdown["preference"] = 5.0
            elif pref and pref.preferred_building and pref.preferred_building.lower() == (
                workstation.building or ""
            ).lower():
                breakdown["preference"] = 2.0

        total = sum(breakdown.values())
        if not avail.available:
            total *= 0.0  # ineligible

        return CandidateScore(
            workstation=workstation,
            score=total,
            breakdown=breakdown,
            available=avail.available,
            reasons=avail.reasons,
        )

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
    ) -> list[CandidateScore]:
        qs = AnalysisWorkstation.objects.select_related("capabilities", "department").filter(enabled=True)
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

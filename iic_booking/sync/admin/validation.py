"""Configuration validation rules for the Operations Console."""

from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Count, Exists, OuterRef, Prefetch
from django.utils.translation import gettext_lazy as _

from iic_booking.sync.models import (
    AgentAssignment,
    AgentLifecycleStatus,
    DepartmentSyncAgent,
    EquipmentSyncProfile,
)


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    severity: str  # warning | error
    message: str
    object_repr: str = ""


def validate_sync_profile(profile: EquipmentSyncProfile) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    active_assignment = None
    if hasattr(profile, "_prefetched_objects_cache") and "assignments" in profile._prefetched_objects_cache:
        for assignment in profile.assignments.all():
            if assignment.is_active:
                active_assignment = assignment
                break
    else:
        active_assignment = profile.assignments.filter(is_active=True).select_related("sync_agent").first()

    if not (profile.watch_folder or "").strip():
        issues.append(
            ValidationIssue(
                code="WATCH_FOLDER_MISSING",
                severity="warning",
                message=str(_("Watch folder is missing.")),
                object_repr=str(profile),
            )
        )
    if active_assignment is None:
        issues.append(
            ValidationIssue(
                code="AGENT_MISSING",
                severity="warning",
                message=str(_("No active agent assignment.")),
                object_repr=str(profile),
            )
        )
    incomplete = not any(
        [
            (profile.hostname or "").strip(),
            (profile.ip_address or "").strip(),
            (profile.share_name or "").strip(),
            (profile.unc_path or "").strip(),
            (profile.watch_folder or "").strip(),
        ]
    )
    if incomplete:
        issues.append(
            ValidationIssue(
                code="CONFIG_INCOMPLETE",
                severity="warning",
                message=str(_("Configuration appears incomplete (no host/share/watch path).")),
                object_repr=str(profile),
            )
        )
    if not profile.sync_enabled:
        issues.append(
            ValidationIssue(
                code="SYNC_DISABLED",
                severity="warning",
                message=str(_("Sync is disabled for this profile.")),
                object_repr=str(profile),
            )
        )
    return issues


def validate_agent(agent: DepartmentSyncAgent) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if agent.laboratory_id is None:
        issues.append(
            ValidationIssue(
                code="AGENT_WITHOUT_LABORATORY",
                severity="warning",
                message=str(_("Agent has no laboratory assigned.")),
                object_repr=str(agent),
            )
        )
    if agent.status == AgentLifecycleStatus.ENROLLED and not agent.agent_secret_hash:
        issues.append(
            ValidationIssue(
                code="MISSING_SECRET",
                severity="error",
                message=str(_("Enrolled agent has no secret hash.")),
                object_repr=str(agent),
            )
        )
    return issues


def collect_system_validation_issues(*, limit: int = 50) -> list[ValidationIssue]:
    """Aggregate cross-entity validation findings for the dashboard."""
    issues: list[ValidationIssue] = []

    # Equipment with sync-ish DSA enabled fields but no sync profile (compatibility path).
    from iic_booking.equipment.models import Equipment

    missing_profile = (
        Equipment.objects.filter(dsa_enabled=True)
        .filter(~Exists(EquipmentSyncProfile.objects.filter(equipment_id=OuterRef("pk"))))
        .select_related("internal_department")[:limit]
    )
    for equipment in missing_profile:
        issues.append(
            ValidationIssue(
                code="EQUIPMENT_WITHOUT_SYNC_PROFILE",
                severity="warning",
                message=str(_("Equipment has DSA enabled but no EquipmentSyncProfile.")),
                object_repr=f"{equipment.code} — {equipment.name}",
            )
        )

    profiles = (
        EquipmentSyncProfile.objects.select_related("equipment", "equipment__internal_department")
        .prefetch_related(
            Prefetch(
                "assignments",
                queryset=AgentAssignment.objects.filter(is_active=True).select_related("sync_agent"),
            )
        )[: limit * 2]
    )
    for profile in profiles:
        issues.extend(validate_sync_profile(profile))

    agents = DepartmentSyncAgent.objects.select_related("department", "laboratory")[:limit]
    for agent in agents:
        issues.extend(validate_agent(agent))

    # Duplicate active assignments should be impossible via constraint; still surface orphans.
    dupes = (
        AgentAssignment.objects.filter(is_active=True)
        .values("sync_profile_id")
        .annotate(c=Count("id"))
        .filter(c__gt=1)[:limit]
    )
    for row in dupes:
        issues.append(
            ValidationIssue(
                code="DUPLICATE_ASSIGNMENTS",
                severity="error",
                message=str(_("Multiple active assignments for one sync profile.")),
                object_repr=str(row["sync_profile_id"]),
            )
        )

    # Configuration mismatch: enrolled agent reporting older config than any assigned profile.
    enrolled = (
        DepartmentSyncAgent.objects.filter(status=AgentLifecycleStatus.ENROLLED)
        .prefetch_related(
            Prefetch(
                "assignments",
                queryset=AgentAssignment.objects.filter(is_active=True).select_related("sync_profile"),
            )
        )[:limit]
    )
    for agent in enrolled:
        if agent.last_reported_configuration_version is None:
            continue
        for assignment in agent.assignments.all():
            profile = assignment.sync_profile
            if profile.configuration_version > agent.last_reported_configuration_version:
                issues.append(
                    ValidationIssue(
                        code="CONFIGURATION_VERSION_MISMATCH",
                        severity="warning",
                        message=str(
                            _("Agent reported config v%(reported)s but profile is v%(current)s.")
                            % {
                                "reported": agent.last_reported_configuration_version,
                                "current": profile.configuration_version,
                            }
                        ),
                        object_repr=str(agent),
                    )
                )
                break
            if (
                agent.last_reported_schema_version is not None
                and profile.schema_version > agent.last_reported_schema_version
            ):
                issues.append(
                    ValidationIssue(
                        code="SCHEMA_VERSION_MISMATCH",
                        severity="warning",
                        message=str(
                            _("Agent reported schema v%(reported)s but profile is v%(current)s.")
                            % {
                                "reported": agent.last_reported_schema_version,
                                "current": profile.schema_version,
                            }
                        ),
                        object_repr=str(agent),
                    )
                )
                break

    # Deduplicate by code+object while preserving order.
    seen: set[tuple[str, str]] = set()
    unique: list[ValidationIssue] = []
    for issue in issues:
        key = (issue.code, issue.object_repr)
        if key in seen:
            continue
        seen.add(key)
        unique.append(issue)
        if len(unique) >= limit:
            break
    return unique

"""Community and membership sync: one community per Equipment Group, members from bookings and staff assignments.

Rules (confirmed by the portal owner, 11 Nov 2026)
--------------------------------------------------
* Bookers: every booking except ``PENDING_PAYMENT`` counts, the same rule as Facility User Groups (cancelled,
  refunded, waitlisted and not-utilized bookings count). A mode child counts for its base instrument's group when
  it has no group of its own. ``Booking.user`` is the booker. External users are included (General IITR
  Discussions stays internal-only; that is decided in ``access``, not here).
* The booking source is *sticky*: once a user booked in a group they stay a booker of it, even if the equipment is
  later moved to another group (history is kept).
* Staff sources are live: OIC = ``EquipmentManager`` + active ``EquipmentTemporaryOIC``; Lab Operator (Lab
  In-charge) = ``EquipmentOperator`` + the acting operator of an active ``EquipmentOperatorCoverage``. When every
  staff source ends and there is no booking or manual add, the membership becomes LAPSED (no access, history kept).
* SUSPENDED / REVOKED are manual states; the sync updates sources underneath them but never changes the status.
* Renamed groups rename their community (the slug and URL stay). Removed groups archive their community; nothing
  is ever deleted.
* Idempotent: recomputed from source data every time, one row per (community, user) enforced by a unique
  constraint, each community applied in its own transaction with the community row locked (concurrent syncs
  serialise). ``dry_run`` performs the exact same work inside a transaction that is rolled back.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from dataclasses import field
from typing import Iterable

from django.db import IntegrityError
from django.db import transaction
from django.db.models import Count
from django.db.models import Max
from django.db.models import Min
from django.db.models import Q
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.text import slugify

from iic_booking.equipment.models import Booking
from iic_booking.equipment.models import BookingStatus
from iic_booking.equipment.models import Equipment
from iic_booking.equipment.models import EquipmentGroup
from iic_booking.equipment.models import EquipmentManager
from iic_booking.equipment.models import EquipmentOperator
from iic_booking.equipment.models import EquipmentOperatorCoverage
from iic_booking.equipment.models import EquipmentTemporaryOIC

from ..models import MANUAL_STATUSES
from ..models import Community
from ..models import CommunityKind
from ..models import CommunityMembership
from ..models import CommunityStatus
from ..models import CommunitySyncRun
from ..models import MembershipAuditAction
from ..models import MembershipAuditLog
from ..models import MembershipStatus

logger = logging.getLogger(__name__)

EXCLUDED_BOOKING_STATUSES = (BookingStatus.PENDING_PAYMENT,)
SOURCE_FIELDS = ("via_booking", "via_oic", "via_operator")
_GROUP_OF_EQUIPMENT = Coalesce("equipment__equipment_group_id", "equipment__parent_equipment__equipment_group_id")


class _Rollback(Exception):
    """Raised inside the dry-run transaction to discard every write."""


@dataclass
class Sources:
    via_booking: bool = False
    via_oic: bool = False
    via_operator: bool = False
    booking_count: int = 0
    first_booked_at: object = None
    last_booked_at: object = None


@dataclass
class SyncReport:
    dry_run: bool = True
    scope: str = "all"
    communities_created: list = field(default_factory=list)
    communities_renamed: list = field(default_factory=list)
    communities_archived: list = field(default_factory=list)
    communities_reactivated: list = field(default_factory=list)
    test_only_changed: list = field(default_factory=list)
    members_created: int = 0
    members_reactivated: int = 0
    members_lapsed: int = 0
    sources_added: int = 0
    sources_removed: int = 0
    unchanged: int = 0
    per_community: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "scope": self.scope,
            "communities_created": self.communities_created,
            "communities_renamed": self.communities_renamed,
            "communities_archived": self.communities_archived,
            "communities_reactivated": self.communities_reactivated,
            "test_only_changed": self.test_only_changed,
            "members_created": self.members_created,
            "members_reactivated": self.members_reactivated,
            "members_lapsed": self.members_lapsed,
            "sources_added": self.sources_added,
            "sources_removed": self.sources_removed,
            "unchanged": self.unchanged,
            "per_community": self.per_community,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Communities
# ---------------------------------------------------------------------------


def _slug_for(group: EquipmentGroup) -> str:
    base = slugify(group.name or "")[:60].strip("-") or "group"
    return f"{base}-{group.pk}"


def _group_is_test_only(group_id: int) -> bool:
    flags = list(
        Equipment.objects.filter(equipment_group_id=group_id).values_list("visible_to_test_accounts_only", flat=True)
    )
    return bool(flags) and all(flags)


def ensure_community(group: EquipmentGroup, report: SyncReport | None = None) -> Community:
    """Create or refresh the community of ``group`` (name and test-only flag). Never deletes anything."""
    community = Community.objects.filter(kind=CommunityKind.GROUP, equipment_group_ref_id=group.pk).first()
    test_only = _group_is_test_only(group.pk)
    if community is None:
        try:
            with transaction.atomic():
                community = Community.objects.create(
                    kind=CommunityKind.GROUP,
                    slug=_slug_for(group),
                    name=group.name[:255],
                    description=(group.description or "")[:2000],
                    equipment_group=group,
                    equipment_group_ref_id=group.pk,
                    group_name_snapshot=group.name[:255],
                    is_test_only=test_only,
                )
        except IntegrityError:
            community = Community.objects.get(kind=CommunityKind.GROUP, equipment_group_ref_id=group.pk)
        else:
            if report is not None:
                report.communities_created.append({"group_id": group.pk, "name": group.name})
            return community
    changed = []
    if community.name != group.name[:255]:
        if report is not None:
            report.communities_renamed.append({"group_id": group.pk, "from": community.name, "to": group.name})
        community.name = group.name[:255]
        community.group_name_snapshot = group.name[:255]
        changed += ["name", "group_name_snapshot"]
    if community.equipment_group_id != group.pk:
        community.equipment_group_id = group.pk
        changed.append("equipment_group")
    if community.status == CommunityStatus.ARCHIVED:
        community.status = CommunityStatus.ACTIVE
        community.archived_at = None
        changed += ["status", "archived_at"]
        if report is not None:
            report.communities_reactivated.append({"group_id": group.pk, "name": group.name})
    if community.is_test_only != test_only:
        community.is_test_only = test_only
        changed.append("is_test_only")
        if report is not None:
            report.test_only_changed.append({"group_id": group.pk, "test_only": test_only})
    if changed:
        community.save(update_fields=[*changed, "updated_at"])
    return community


def archive_orphaned_communities(report: SyncReport | None = None) -> int:
    """Archive communities whose equipment group no longer exists (discussions are kept, read-only)."""
    existing = set(EquipmentGroup.objects.values_list("pk", flat=True))
    orphans = Community.objects.filter(kind=CommunityKind.GROUP, status=CommunityStatus.ACTIVE).exclude(
        equipment_group_ref_id__in=existing
    )
    count = 0
    for community in orphans:
        community.status = CommunityStatus.ARCHIVED
        community.archived_at = timezone.now()
        community.equipment_group = None
        community.save(update_fields=["status", "archived_at", "equipment_group", "updated_at"])
        count += 1
        if report is not None:
            report.communities_archived.append({"group_id": community.equipment_group_ref_id, "name": community.name})
    return count


# ---------------------------------------------------------------------------
# Desired membership from source data
# ---------------------------------------------------------------------------


def _staff_equipment_by_group(group_ids: set[int] | None) -> dict[int, set[int]]:
    qs = Equipment.objects.annotate(gid=Coalesce("equipment_group_id", "parent_equipment__equipment_group_id"))
    qs = qs.filter(gid__isnull=False)
    if group_ids is not None:
        qs = qs.filter(gid__in=group_ids)
    out: dict[int, set[int]] = defaultdict(set)
    for eid, gid in qs.values_list("equipment_id", "gid"):
        out[eid].add(gid)
    return out


def desired_members(group_ids: Iterable[int] | None = None, user_ids: Iterable[int] | None = None) -> dict[int, dict[int, Sources]]:
    """{group_id: {user_id: Sources}} recomputed from bookings and current staff assignments."""
    gids = None if group_ids is None else {int(g) for g in group_ids}
    uids = None if user_ids is None else {int(u) for u in user_ids}
    out: dict[int, dict[int, Sources]] = defaultdict(dict)

    def src(gid: int, uid: int) -> Sources:
        row = out[gid].get(uid)
        if row is None:
            row = out[gid][uid] = Sources()
        return row

    bookings = Booking.objects.exclude(status__in=EXCLUDED_BOOKING_STATUSES).annotate(gid=_GROUP_OF_EQUIPMENT)
    bookings = bookings.filter(gid__isnull=False, user_id__isnull=False)
    if gids is not None:
        bookings = bookings.filter(gid__in=gids)
    if uids is not None:
        bookings = bookings.filter(user_id__in=uids)
    for row in bookings.values("gid", "user_id").annotate(n=Count("pk"), first=Min("created_at"), last=Max("created_at")):
        s = src(row["gid"], row["user_id"])
        s.via_booking = True
        s.booking_count = row["n"]
        s.first_booked_at = row["first"]
        s.last_booked_at = row["last"]

    groups_of = _staff_equipment_by_group(gids)
    now = timezone.now()

    def staff(rows: Iterable[tuple[int, int]], attr: str) -> None:
        for eid, uid in rows:
            if uid is None or (uids is not None and uid not in uids):
                continue
            for gid in groups_of.get(eid, ()):
                setattr(src(gid, uid), attr, True)

    eq_ids = list(groups_of)
    staff(EquipmentManager.objects.filter(equipment_id__in=eq_ids).values_list("equipment_id", "manager_id"), "via_oic")
    staff(
        EquipmentTemporaryOIC.objects.active(now).filter(equipment_id__in=eq_ids).values_list("equipment_id", "temporary_oic_id"),
        "via_oic",
    )
    staff(EquipmentOperator.objects.filter(equipment_id__in=eq_ids).values_list("equipment_id", "operator_id"), "via_operator")
    coverage = EquipmentOperatorCoverage.objects.filter(
        equipment_id__in=eq_ids, starts_at__lte=now, ends_at__gte=now, acting_operator__isnull=False
    ).filter(Q(ended_early_at__isnull=True) | Q(ended_early_at__gt=now))
    staff(coverage.values_list("equipment_id", "acting_operator_id"), "via_operator")
    return out


# ---------------------------------------------------------------------------
# Applying desired state to one community
# ---------------------------------------------------------------------------


def _audit(community, user_id, action, *, actor=None, detail=None, run=None):
    MembershipAuditLog.objects.create(
        community=community, user_id=user_id, actor=actor, action=action, detail=detail or {}, sync_run=run
    )


def _has_any_source(m: CommunityMembership) -> bool:
    return m.via_booking or m.via_oic or m.via_operator or m.via_manual


def apply_community(
    community: Community,
    desired: dict[int, Sources],
    *,
    scope_user_ids: set[int] | None = None,
    report: SyncReport,
    run: CommunitySyncRun | None = None,
) -> None:
    """Bring the memberships of ``community`` in line with ``desired``.

    ``scope_user_ids`` limits which existing rows may lose staff sources (incremental syncs only look at the users
    they recomputed). Must run inside a transaction; the community row is locked to serialise concurrent syncs.
    """
    Community.objects.select_for_update().filter(pk=community.pk).first()
    existing = {m.user_id: m for m in CommunityMembership.objects.filter(community=community)}
    stats = report.per_community.setdefault(
        str(community.pk), {"name": community.name, "created": 0, "lapsed": 0, "reactivated": 0, "sources_changed": 0}
    )
    candidates = set(desired)
    candidates |= set(existing) if scope_user_ids is None else (set(existing) & scope_user_ids)

    for user_id in candidates:
        want = desired.get(user_id, Sources())
        membership = existing.get(user_id)
        if membership is None:
            if not (want.via_booking or want.via_oic or want.via_operator):
                continue
            membership, created = CommunityMembership.objects.get_or_create(
                community=community,
                user_id=user_id,
                defaults={
                    "via_booking": want.via_booking,
                    "via_oic": want.via_oic,
                    "via_operator": want.via_operator,
                    "booking_count": want.booking_count,
                    "first_booked_at": want.first_booked_at,
                    "last_booked_at": want.last_booked_at,
                    "status": MembershipStatus.ACTIVE,
                },
            )
            if created:
                report.members_created += 1
                stats["created"] += 1
                _audit(
                    community,
                    user_id,
                    MembershipAuditAction.JOINED,
                    detail={k: getattr(want, k) for k in SOURCE_FIELDS},
                    run=run,
                )
                continue

        changed: list[str] = []
        added, removed = [], []
        # Booking source is sticky: only ever switched on.
        if want.via_booking and not membership.via_booking:
            membership.via_booking = True
            added.append("via_booking")
        for attr in ("via_oic", "via_operator"):
            if getattr(membership, attr) != getattr(want, attr):
                setattr(membership, attr, getattr(want, attr))
                (added if getattr(want, attr) else removed).append(attr)
        if added or removed:
            changed += added + removed
        if want.via_booking:
            for attr in ("booking_count", "first_booked_at", "last_booked_at"):
                if getattr(membership, attr) != getattr(want, attr):
                    setattr(membership, attr, getattr(want, attr))
                    changed.append(attr)

        status_action = None
        if membership.status not in MANUAL_STATUSES:
            target = MembershipStatus.ACTIVE if _has_any_source(membership) else MembershipStatus.LAPSED
            if target != membership.status:
                status_action = (
                    MembershipAuditAction.REACTIVATED if target == MembershipStatus.ACTIVE else MembershipAuditAction.LAPSED
                )
                membership.status = target
                membership.status_changed_at = timezone.now()
                membership.status_changed_by = None
                membership.status_reason = "" if target == MembershipStatus.ACTIVE else "No current booking or staff source"
                changed += ["status", "status_changed_at", "status_changed_by", "status_reason"]

        if not changed:
            report.unchanged += 1
            continue
        membership.save(update_fields=[*dict.fromkeys(changed), "updated_at"])
        if added:
            report.sources_added += len(added)
            stats["sources_changed"] += 1
            _audit(community, user_id, MembershipAuditAction.SOURCE_ADDED, detail={"sources": added}, run=run)
        if removed:
            report.sources_removed += len(removed)
            stats["sources_changed"] += 1
            _audit(community, user_id, MembershipAuditAction.SOURCE_REMOVED, detail={"sources": removed}, run=run)
        if status_action == MembershipAuditAction.LAPSED:
            report.members_lapsed += 1
            stats["lapsed"] += 1
            _audit(community, user_id, status_action, run=run)
        elif status_action == MembershipAuditAction.REACTIVATED:
            report.members_reactivated += 1
            stats["reactivated"] += 1
            _audit(community, user_id, status_action, run=run)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def _run(scope: str, dry_run: bool, triggered_by, source: str, body) -> SyncReport:
    report = SyncReport(dry_run=dry_run, scope=scope)
    run = None
    if not dry_run:
        run = CommunitySyncRun.objects.create(dry_run=False, scope=scope[:64], triggered_by=triggered_by, source=source)
    try:
        if dry_run:
            try:
                with transaction.atomic():
                    body(report, None)
                    raise _Rollback
            except _Rollback:
                pass
        else:
            body(report, run)
    except Exception as exc:  # noqa: BLE001 - recorded in the report and the run
        logger.exception("connect membership sync failed (%s)", scope)
        report.errors.append(str(exc)[:500])
        if run is not None:
            run.status = CommunitySyncRun.Status.FAILED
            run.report = report.as_dict()
            run.finished_at = timezone.now()
            run.save(update_fields=["status", "report", "finished_at"])
        raise
    if run is not None:
        run.status = CommunitySyncRun.Status.DONE
        run.report = report.as_dict()
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "report", "finished_at"])
    return report


def sync_all(*, dry_run: bool = True, group_ids: Iterable[int] | None = None, triggered_by=None, source: str = "command") -> SyncReport:
    """Full sync: communities for every (or the given) group, membership recomputed, orphans archived."""
    gids = None if group_ids is None else sorted({int(g) for g in group_ids})
    scope = "all" if gids is None else "groups:" + ",".join(map(str, gids))

    def body(report: SyncReport, run) -> None:
        groups = EquipmentGroup.objects.all().order_by("pk")
        if gids is not None:
            groups = groups.filter(pk__in=gids)
        groups = list(groups)
        desired = desired_members(group_ids=[g.pk for g in groups])
        for group in groups:
            with transaction.atomic():
                community = ensure_community(group, report)
                apply_community(community, desired.get(group.pk, {}), report=report, run=run)
        if gids is None:
            with transaction.atomic():
                archive_orphaned_communities(report)

    return _run(scope, dry_run, triggered_by, source, body)


def sync_users_in_groups(user_ids: Iterable[int], group_ids: Iterable[int], *, source: str = "signal") -> SyncReport:
    """Incremental sync of some users in some groups (booking / assignment signals). Always applies."""
    uids = {int(u) for u in user_ids if u}
    gids = {int(g) for g in group_ids if g}
    report = SyncReport(dry_run=False, scope=f"users:{len(uids)} groups:{len(gids)}")
    if not uids or not gids:
        return report
    desired = desired_members(group_ids=gids, user_ids=uids)
    for group in EquipmentGroup.objects.filter(pk__in=gids):
        with transaction.atomic():
            community = ensure_community(group, report)
            apply_community(community, desired.get(group.pk, {}), scope_user_ids=uids, report=report)
    return report


def sync_groups(group_ids: Iterable[int], *, source: str = "signal") -> SyncReport:
    """Recompute whole groups (staff assignment changes, equipment moved between groups). Always applies."""
    gids = [g for g in {int(g) for g in group_ids if g}]
    if not gids:
        return SyncReport(dry_run=False, scope="groups:none")
    return sync_all(dry_run=False, group_ids=gids, source=source)


def group_ids_for_equipment(equipment_ids: Iterable[int]) -> set[int]:
    ids = [int(e) for e in equipment_ids if e]
    if not ids:
        return set()
    rows = Equipment.objects.filter(pk__in=ids).annotate(
        gid=Coalesce("equipment_group_id", "parent_equipment__equipment_group_id")
    )
    out = {gid for gid in rows.values_list("gid", flat=True) if gid}
    # Mode children of this equipment roll up to it as well.
    out |= set(
        Equipment.objects.filter(parent_equipment_id__in=ids, equipment_group_id__isnull=False).values_list(
            "equipment_group_id", flat=True
        )
    )
    return out


# ---------------------------------------------------------------------------
# Manual administration (suspend / revoke / restore / add / moderator)
# ---------------------------------------------------------------------------


class MembershipActionError(ValueError):
    pass


MANUAL_ACTIONS = ("suspend", "revoke", "restore", "add", "appoint_moderator", "remove_moderator", "reset_moderator")


@transaction.atomic
def manual_action(community: Community, user, action: str, *, actor, reason: str = "", until=None) -> CommunityMembership:
    """Administrative membership change, always audited. Callers check the actor's authority first."""
    from .. import access

    if action not in MANUAL_ACTIONS:
        raise MembershipActionError("Unknown action.")
    reason = (reason or "").strip()[:500]
    if action in ("suspend", "revoke") and not reason:
        raise MembershipActionError("A reason is required.")
    if community.is_general and not access.is_internal_user(user) and action in ("add", "restore", "appoint_moderator"):
        raise MembershipActionError("General IITR Discussions is open to IIT Roorkee internal users only.")
    if user.pk == getattr(actor, "pk", None) and action in ("restore", "add", "appoint_moderator"):
        raise MembershipActionError("You cannot change your own access or role.")

    Community.objects.select_for_update().filter(pk=community.pk).first()
    membership = CommunityMembership.objects.filter(community=community, user=user).first()
    if membership is None:
        # A group community row is only ever created by a booking / staff source or an explicit add, never by a
        # suspend / restore / moderator action (a suspended row would otherwise grant read access).
        if action in ("remove_moderator", "reset_moderator") or (not community.is_general and action != "add"):
            raise MembershipActionError("This user is not a member of this community.")
        membership = CommunityMembership(community=community, user=user, status=MembershipStatus.ACTIVE)
        if action == "add":
            membership.via_manual = True
        membership.save()
        if action == "add":
            _audit(community, user.pk, MembershipAuditAction.MANUAL_ADD, actor=actor, detail={"reason": reason})
            return membership
    before = {"status": membership.status, "moderator_override": membership.moderator_override, "via_manual": membership.via_manual}
    now = timezone.now()
    if action == "suspend":
        membership.status = MembershipStatus.SUSPENDED
        membership.suspended_until = until
        audit_action = MembershipAuditAction.SUSPENDED
    elif action == "revoke":
        membership.status = MembershipStatus.REVOKED
        membership.suspended_until = None
        audit_action = MembershipAuditAction.REVOKED
    elif action == "restore":
        membership.suspended_until = None
        membership.status = (
            MembershipStatus.ACTIVE if (_has_any_source(membership) or community.is_general) else MembershipStatus.LAPSED
        )
        audit_action = MembershipAuditAction.RESTORED
    elif action == "add":
        membership.via_manual = True
        if membership.status == MembershipStatus.LAPSED:
            membership.status = MembershipStatus.ACTIVE
        audit_action = MembershipAuditAction.MANUAL_ADD
    else:
        membership.moderator_override = {"appoint_moderator": True, "remove_moderator": False, "reset_moderator": None}[action]
        audit_action = MembershipAuditAction.ROLE_CHANGED
    if action in ("suspend", "revoke", "restore"):
        membership.status_reason = reason
        membership.status_changed_by = actor
        membership.status_changed_at = now
    membership.save()
    after = {"status": membership.status, "moderator_override": membership.moderator_override, "via_manual": membership.via_manual}
    _audit(community, user.pk, audit_action, actor=actor, detail={"reason": reason, "before": before, "after": after})
    access.invalidate(user)
    return membership


def lift_expired_suspensions(now=None) -> int:
    """Suspensions with an end date return to their computed status (run by the nightly task)."""
    now = now or timezone.now()
    count = 0
    for membership in CommunityMembership.objects.filter(status=MembershipStatus.SUSPENDED, suspended_until__lte=now).select_related("community"):
        with transaction.atomic():
            membership.status = (
                MembershipStatus.ACTIVE
                if (_has_any_source(membership) or membership.community.is_general)
                else MembershipStatus.LAPSED
            )
            membership.suspended_until = None
            membership.status_changed_at = now
            membership.status_reason = "Suspension ended"
            membership.save(update_fields=["status", "suspended_until", "status_changed_at", "status_reason", "updated_at"])
            _audit(membership.community, membership.user_id, MembershipAuditAction.RESTORED, detail={"reason": "Suspension ended"})
            count += 1
    return count


def member_counts(community_ids: Iterable[int]) -> dict[int, int]:
    rows = (
        CommunityMembership.objects.filter(community_id__in=list(community_ids), status=MembershipStatus.ACTIVE)
        .values("community_id")
        .annotate(n=Count("pk"))
    )
    return {r["community_id"]: r["n"] for r in rows}


__all__ = [
    "SyncReport",
    "apply_community",
    "desired_members",
    "ensure_community",
    "group_ids_for_equipment",
    "manual_action",
    "sync_all",
    "sync_groups",
    "sync_users_in_groups",
]

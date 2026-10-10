"""Automatic group membership computed from bookings.

Rules
-----
* Every booking counts except ``PENDING_PAYMENT`` (an unpaid hold that has not become a booking yet). Cancelled,
  refunded, not-utilized and waitlisted bookings still show the person used or wanted the facility, so they count.
  When a pending-payment booking is paid its status change triggers the sync.
* A booking places its user in: "All booking users", the equipment (mode children roll up to their base
  instrument), the equipment's category (e.g. Electron Microscopy), its equipment group and the internal
  department hosting it (e.g. a centre or the Tinkering Lab). Category / group / lab fall back to the base
  instrument's when the mode itself has none.
* The faculty a booking user books under (owner of the faculty wallet they joined, else their ``supervisor``,
  as ``quota_breakdown.supervisor_of``) is recorded in the same groups as a supervisor
  (``supervised_booking_count``). Supervisor-only members are hidden unless "include supervisors" is chosen.
* Counts and dates are recomputed from bookings every time (never incremented), so a re-run or retry is harmless.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

from django.conf import settings
from django.db.models import Count, Max, Min, Q

from iic_booking.equipment.models import Booking, BookingStatus, Equipment
from iic_booking.users.models import User, UserType

from .models import FacilityUserGroup, FacilityUserGroupMember, GroupKind

logger = logging.getLogger(__name__)

EXCLUDED_STATUSES: tuple[str, ...] = (BookingStatus.PENDING_PAYMENT,)
ALL_KEY = "all"
ALL_NAME = "All booking users"


def auto_membership_enabled() -> bool:
    return bool(getattr(settings, "FACILITY_GROUPS_AUTO_MEMBERSHIP", True))


def counted_bookings():
    return Booking.objects.exclude(status__in=EXCLUDED_STATUSES)


@dataclass(frozen=True)
class GroupSpec:
    auto_key: str
    kind: str
    name: str
    fk_field: Optional[str] = None
    fk_id: Optional[int] = None


ALL_SPEC = GroupSpec(ALL_KEY, GroupKind.ALL, ALL_NAME)


def _specs_for(equipment: Equipment) -> list[GroupSpec]:
    root = equipment.parent_equipment or equipment
    specs = [
        ALL_SPEC,
        GroupSpec(
            f"equipment:{root.pk}",
            GroupKind.EQUIPMENT,
            f"{root.name} ({root.code})" if root.code else root.name,
            "equipment",
            root.pk,
        ),
    ]
    category = equipment.category or root.category
    if category is not None:
        specs.append(
            GroupSpec(f"category:{category.pk}", GroupKind.CATEGORY, category.name or category.code or f"Category {category.pk}", "category", category.pk)
        )
    equipment_group = equipment.equipment_group or root.equipment_group
    if equipment_group is not None:
        specs.append(
            GroupSpec(
                f"equipment_group:{equipment_group.pk}",
                GroupKind.EQUIPMENT_GROUP,
                equipment_group.name,
                "equipment_group",
                equipment_group.pk,
            )
        )
    lab = equipment.internal_department or root.internal_department
    if lab is not None:
        specs.append(GroupSpec(f"lab:{lab.pk}", GroupKind.LAB, lab.name, "lab", lab.pk))
    return specs


_EQUIPMENT_RELATED = (
    "parent_equipment",
    "category",
    "equipment_group",
    "internal_department",
    "parent_equipment__category",
    "parent_equipment__equipment_group",
    "parent_equipment__internal_department",
)


def specs_by_equipment(equipment_ids: Iterable[int]) -> dict[int, list[GroupSpec]]:
    ids = {int(i) for i in equipment_ids if i}
    if not ids:
        return {}
    rows = Equipment.objects.filter(pk__in=ids).select_related(*_EQUIPMENT_RELATED)
    return {eq.pk: _specs_for(eq) for eq in rows}


def ensure_groups(specs: Iterable[GroupSpec], *, create: bool = True) -> dict[str, Optional[FacilityUserGroup]]:
    """Automatic groups for the specs, created (or renamed after an equipment rename) as needed."""
    unique = {s.auto_key: s for s in specs}
    existing = {g.auto_key: g for g in FacilityUserGroup.objects.filter(auto_key__in=list(unique))}
    out: dict[str, Optional[FacilityUserGroup]] = {}
    for key, spec in unique.items():
        group = existing.get(key)
        fk_attr = f"{spec.fk_field}_id" if spec.fk_field else None
        if group is None:
            if not create:
                out[key] = None
                continue
            defaults = {"name": spec.name[:255], "kind": spec.kind}
            if fk_attr:
                defaults[fk_attr] = spec.fk_id
            group, _ = FacilityUserGroup.objects.get_or_create(auto_key=key, defaults=defaults)
        elif create:
            changed = []
            if group.name != spec.name[:255]:
                group.name = spec.name[:255]
                changed.append("name")
            if fk_attr and getattr(group, fk_attr) != spec.fk_id:
                setattr(group, fk_attr, spec.fk_id)
                changed.append(fk_attr)
            if changed:
                group.save(update_fields=[*changed, "updated_at"])
        out[key] = group
    return out


# ---------------------------------------------------------------------------
# Supervisor links (bulk version of quota_breakdown.supervisor_of)
# ---------------------------------------------------------------------------


def supervisor_map(user_ids: Iterable[int]) -> dict[int, int]:
    from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus

    ids = {int(i) for i in user_ids if i}
    if not ids:
        return {}
    users = list(User.objects.filter(pk__in=ids).values_list("pk", "user_type", "supervisor_id"))
    wallet_types = {UserType.STUDENT, UserType.OTHER}
    joined: dict[int, int] = {}
    student_ids = [pk for pk, user_type, _ in users if user_type in wallet_types]
    if student_ids:
        for student_id, owner_id in WalletJoinRequest.objects.filter(
            student_id__in=student_ids, status=WalletJoinRequestStatus.APPROVED
        ).values_list("student_id", "wallet__user_id"):
            joined.setdefault(student_id, owner_id)
    out: dict[int, int] = {}
    for pk, user_type, supervisor_id in users:
        if user_type == UserType.FACULTY:
            continue
        owner = joined.get(pk)
        if owner and owner != pk:
            out[pk] = owner
        elif supervisor_id and supervisor_id != pk:
            out[pk] = supervisor_id
    return out


def supervisees_of(supervisor_id: int) -> set[int]:
    from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus

    candidates = set(User.objects.filter(supervisor_id=supervisor_id).values_list("pk", flat=True))
    candidates |= set(
        WalletJoinRequest.objects.filter(
            wallet__user_id=supervisor_id, status=WalletJoinRequestStatus.APPROVED
        ).values_list("student_id", flat=True)
    )
    links = supervisor_map(candidates)
    return {pk for pk in candidates if links.get(pk) == supervisor_id}


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


@dataclass
class Stats:
    count: int = 0
    first: Optional[datetime] = None
    last: Optional[datetime] = None
    sup_count: int = 0
    sup_last: Optional[datetime] = None


def _min(a, b):
    return b if a is None else (a if b is None else min(a, b))


def _max(a, b):
    return b if a is None else (a if b is None else max(a, b))


def booking_aggregates(user_ids: Optional[Iterable[int]] = None):
    qs = counted_bookings()
    if user_ids is not None:
        qs = qs.filter(user_id__in=list(user_ids))
    return list(
        qs.values("user_id", "equipment_id")
        .annotate(n=Count("pk"), first=Min("created_at"), last=Max("created_at"))
        .order_by()
        .values_list("user_id", "equipment_id", "n", "first", "last")
    )


def rollup(
    rows,
    specs: dict[int, list[GroupSpec]],
    sup_links: dict[int, int],
    *,
    keys: Optional[set[str]] = None,
    bookers: Optional[set[int]] = None,
    supervisors: Optional[set[int]] = None,
) -> dict[tuple[str, int], Stats]:
    out: dict[tuple[str, int], Stats] = {}
    for user_id, equipment_id, n, first, last in rows:
        sup = sup_links.get(user_id)
        for spec in specs.get(equipment_id, ()):
            if keys is not None and spec.auto_key not in keys:
                continue
            if bookers is None or user_id in bookers:
                st = out.setdefault((spec.auto_key, user_id), Stats())
                st.count += n
                st.first = _min(st.first, first)
                st.last = _max(st.last, last)
            if sup and (supervisors is None or sup in supervisors):
                st = out.setdefault((spec.auto_key, sup), Stats())
                st.sup_count += n
                st.sup_last = _max(st.sup_last, last)
    return out


@dataclass
class Plan:
    creates: list[FacilityUserGroupMember] = field(default_factory=list)
    updates: list[FacilityUserGroupMember] = field(default_factory=list)
    deletes: list[int] = field(default_factory=list)
    groups_missing: int = 0

    def summary(self) -> dict:
        return {
            "groups_to_create": self.groups_missing,
            "members_to_create": len(self.creates),
            "members_to_update": len(self.updates),
            "members_to_remove": len(self.deletes),
        }


_BOOKER_FIELDS = ("booking_count", "first_booked_at", "last_booked_at")
_SUPERVISOR_FIELDS = ("supervised_booking_count", "last_supervised_at")


def plan(
    stats: dict[tuple[str, int], Stats],
    groups: dict[str, Optional[FacilityUserGroup]],
    *,
    booker_users: Optional[set[int]] = None,
    supervisor_users: Optional[set[int]] = None,
    full: bool = False,
) -> Plan:
    """Rows to create / update (/ remove when ``full``) so the members match ``stats``.

    Only the fields computed for a user are touched: booker fields for ``booker_users`` and supervisor fields for
    ``supervisor_users`` (None = everyone, used by the full rebuild).
    """
    result = Plan(groups_missing=sum(1 for g in groups.values() if g is None or g.pk is None))
    group_pks = [g.pk for g in groups.values() if g is not None and g.pk]
    existing_qs = FacilityUserGroupMember.objects.filter(group_id__in=group_pks)
    if not full:
        existing_qs = existing_qs.filter(user_id__in={uid for _, uid in stats})
    existing = {(m.group_id, m.user_id): m for m in existing_qs}
    seen: set[tuple[int, int]] = set()
    for (key, user_id), st in stats.items():
        group = groups.get(key)
        values = {
            "booking_count": st.count,
            "first_booked_at": st.first,
            "last_booked_at": st.last,
            "supervised_booking_count": st.sup_count,
            "last_supervised_at": st.sup_last,
        }
        member = existing.get((group.pk, user_id)) if group is not None and group.pk else None
        if member is None:
            result.creates.append(FacilityUserGroupMember(group=group, user_id=user_id, **values))
            continue
        seen.add((group.pk, user_id))
        names: list[str] = []
        if booker_users is None or user_id in booker_users:
            names.extend(_BOOKER_FIELDS)
        if supervisor_users is None or user_id in supervisor_users:
            names.extend(_SUPERVISOR_FIELDS)
        changed = False
        for name in names:
            if getattr(member, name) != values[name]:
                setattr(member, name, values[name])
                changed = True
        if changed:
            result.updates.append(member)
    if full:
        for pair, member in existing.items():
            if pair in seen:
                continue
            if not member.added_manually:
                result.deletes.append(member.pk)
            elif member.booking_count or member.supervised_booking_count:
                for name in (*_BOOKER_FIELDS, *_SUPERVISOR_FIELDS):
                    setattr(member, name, 0 if name.endswith("count") else None)
                result.updates.append(member)
    return result


def apply_plan(result: Plan) -> None:
    if result.creates:
        FacilityUserGroupMember.objects.bulk_create(result.creates, batch_size=500, ignore_conflicts=True)
    if result.updates:
        FacilityUserGroupMember.objects.bulk_update(
            result.updates, [*_BOOKER_FIELDS, *_SUPERVISOR_FIELDS], batch_size=500
        )
    if result.deletes:
        FacilityUserGroupMember.objects.filter(pk__in=result.deletes).delete()


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def sync_booking(booking_or_id) -> Optional[dict]:
    """Record the booking's user (and the faculty they book under) in the booking's automatic groups."""
    booking_id = getattr(booking_or_id, "pk", booking_or_id)
    row = Booking.objects.filter(pk=booking_id).values("status", "user_id", "equipment_id").first()
    if not row or row["status"] in EXCLUDED_STATUSES or not row["equipment_id"]:
        return None
    booking_specs = specs_by_equipment([row["equipment_id"]]).get(row["equipment_id"], [])
    if not booking_specs:
        return None
    groups = ensure_groups(booking_specs)
    booker = row["user_id"]
    supervisor = supervisor_map([booker]).get(booker)
    bookers = {booker}
    supervisors: set[int] = set()
    sup_links: dict[int, int] = {}
    users = {booker}
    if supervisor:
        bookers.add(supervisor)
        supervisors.add(supervisor)
        team = supervisees_of(supervisor) | {booker}
        sup_links = {pk: supervisor for pk in team}
        users |= team | {supervisor}
    rows = booking_aggregates(users)
    specs = specs_by_equipment({r[1] for r in rows})
    stats = rollup(
        rows,
        specs,
        sup_links,
        keys={s.auto_key for s in booking_specs},
        bookers=bookers,
        supervisors=supervisors,
    )
    result = plan(stats, groups, booker_users=bookers, supervisor_users=supervisors)
    apply_plan(result)
    return result.summary()


def rebuild_all(*, apply: bool = False) -> dict:
    """Make every automatic group match all counted bookings. Dry run unless ``apply``."""
    rows = booking_aggregates()
    specs = specs_by_equipment({r[1] for r in rows})
    sup_links = supervisor_map({r[0] for r in rows})
    stats = rollup(rows, specs, sup_links)
    all_specs = {s.auto_key: s for spec_list in specs.values() for s in spec_list}
    groups = ensure_groups(all_specs.values(), create=apply)
    for group in FacilityUserGroup.objects.exclude(kind=GroupKind.CUSTOM).exclude(auto_key__in=list(all_specs)):
        groups[group.auto_key or f"orphan:{group.pk}"] = group
    result = plan(stats, groups, full=True)
    if apply:
        apply_plan(result)
    summary = result.summary()
    summary.update(
        {
            "applied": apply,
            "bookings_counted": sum(r[2] for r in rows),
            "booking_users": len({r[0] for r in rows}),
            "automatic_groups": len(all_specs),
            "supervisor_links": len(set(sup_links.values())),
        }
    )
    return summary


def bookings_scope_q(group: FacilityUserGroup, prefix: str = "") -> Q:
    """Filter for the bookings that make up an automatic group (empty Q for "all" and custom groups)."""
    p = prefix

    def by(field_name: str, value) -> Q:
        if value is None:
            return Q(pk__in=[])
        return Q(**{f"{p}equipment__{field_name}": value}) | Q(
            **{f"{p}equipment__{field_name}__isnull": True, f"{p}equipment__parent_equipment__{field_name}": value}
        )

    if group.kind == GroupKind.EQUIPMENT:
        if group.equipment_id is None:
            return Q(pk__in=[])
        return Q(**{f"{p}equipment_id": group.equipment_id}) | Q(
            **{f"{p}equipment__parent_equipment_id": group.equipment_id}
        )
    if group.kind == GroupKind.CATEGORY:
        return by("category_id", group.category_id)
    if group.kind == GroupKind.EQUIPMENT_GROUP:
        return by("equipment_group_id", group.equipment_group_id)
    if group.kind == GroupKind.LAB:
        return by("internal_department_id", group.lab_id)
    return Q()

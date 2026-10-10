"""Test data: one definition for leaving test records out of dashboards, overviews and reports.

- Test user: ``User.is_test_account``.
- Test equipment: visible to test accounts only (``Equipment.visible_to_test_accounts_only``), marked as test data
  (``TestDataFlag`` kind ``equipment``), in a category marked as test data, or a mode of such an equipment.
- Test category: marked as test data (``TestDataFlag`` kind ``category``).
- Test booking: made by a test user or on test equipment.

Every helper takes ``prefix``, the lookup path from the queryset's model to the user / equipment / booking, e.g.
``exclude_test_bookings(BookingCancellation.objects.all(), "booking__")`` or
``exclude_test_users(Booking.objects.all(), "user__")``.

Marks are read once per ``CACHE_SECONDS`` (cache, no query on a hit) and re-read as soon as ``mark`` / ``unmark``
change them. Before the marks table exists (deploy before Migrate Production) only the built-in flags apply.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from django.core.cache import cache
from django.db import DatabaseError, transaction
from django.db.models import Q, QuerySet

logger = logging.getLogger(__name__)

CACHE_KEY = "equipment:test_data_marks:v1"
CACHE_SECONDS = 300


def marked_ids() -> dict[str, frozenset[int]]:
    """``{"equipment": ids, "category": ids}`` marked as test data."""
    from .testdata_models import TestDataFlag, TestDataKind

    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return cached
    out: dict[str, set[int]] = {str(TestDataKind.EQUIPMENT.value): set(), str(TestDataKind.CATEGORY.value): set()}
    try:
        with transaction.atomic():
            for kind, object_id in TestDataFlag.objects.values_list("kind", "object_id"):
                out.setdefault(str(kind), set()).add(object_id)
    except DatabaseError:
        logger.warning("Test data marks unavailable (table not migrated yet); using built-in flags only")
        return {k: frozenset(v) for k, v in out.items()}
    marks = {k: frozenset(v) for k, v in out.items()}
    cache.set(CACHE_KEY, marks, CACHE_SECONDS)
    return marks


def forget_marks() -> None:
    cache.delete(CACHE_KEY)


# --------------------------------------------------------------------------- Q objects


def q_test_users(prefix: str = "") -> Q:
    return Q(**{f"{prefix}is_test_account": True})


def q_test_categories(prefix: str = "") -> Q:
    """``prefix`` leads to an ``EquipmentCategory`` (``""`` on a category queryset)."""
    ids = sorted(marked_ids()["category"])
    return Q(**{f"{prefix}id__in": ids}) if ids else Q(pk__in=[])


def q_test_equipment(prefix: str = "") -> Q:
    """``prefix`` leads to an ``Equipment`` (``""`` on an equipment queryset, ``"equipment__"`` on bookings)."""
    marks = marked_ids()
    q = Q(**{f"{prefix}visible_to_test_accounts_only": True}) | Q(
        **{f"{prefix}parent_equipment__visible_to_test_accounts_only": True}
    )
    equipment = sorted(marks["equipment"])
    if equipment:
        q |= Q(**{f"{prefix}equipment_id__in": equipment}) | Q(**{f"{prefix}parent_equipment_id__in": equipment})
    categories = sorted(marks["category"])
    if categories:
        q |= Q(**{f"{prefix}category_id__in": categories}) | Q(
            **{f"{prefix}parent_equipment__category_id__in": categories}
        )
    return q


def q_test_bookings(prefix: str = "") -> Q:
    """``prefix`` leads to a ``Booking`` (``""`` on a booking queryset, ``"booking__"`` on cancellations)."""
    return q_test_users(f"{prefix}user__") | q_test_equipment(f"{prefix}equipment__")


# --------------------------------------------------------------------------- queryset helpers


def exclude_test_users(qs: QuerySet, prefix: str = "") -> QuerySet:
    return qs.exclude(q_test_users(prefix))


def exclude_test_categories(qs: QuerySet, prefix: str = "") -> QuerySet:
    return qs.exclude(q_test_categories(prefix)) if marked_ids()["category"] else qs


def exclude_test_equipment(qs: QuerySet, prefix: str = "") -> QuerySet:
    return qs.exclude(q_test_equipment(prefix))


def exclude_test_bookings(qs: QuerySet, prefix: str = "") -> QuerySet:
    return qs.exclude(q_test_users(f"{prefix}user__")).exclude(q_test_equipment(f"{prefix}equipment__"))


def get_test_equipment_ids(candidates: Iterable[int] | None = None) -> set[int]:
    """Ids of test equipment (among ``candidates`` when given)."""
    from .models import Equipment

    qs = Equipment.objects.filter(q_test_equipment())
    if candidates is not None:
        qs = qs.filter(equipment_id__in=list(candidates))
    return set(qs.values_list("equipment_id", flat=True))


def is_test_user(user) -> bool:
    return bool(getattr(user, "is_test_account", False))


def is_test_equipment(equipment) -> bool:
    if equipment is None:
        return False
    eid = getattr(equipment, "equipment_id", None) or getattr(equipment, "pk", None)
    return bool(eid) and eid in get_test_equipment_ids([eid])


# --------------------------------------------------------------------------- marking


def mark(kind: str, obj: Any, *, reason: str = "", by=None) -> bool:
    """Mark ``obj`` (equipment or category) as test data; True when newly marked."""
    from .testdata_models import TestDataFlag

    object_id = getattr(obj, "equipment_id", None) or getattr(obj, "pk", None) or int(obj)
    label = str(getattr(obj, "name", "") or getattr(obj, "code", "") or object_id)[:255]
    _, created = TestDataFlag.objects.get_or_create(
        kind=kind, object_id=object_id, defaults={"label": label, "reason": reason[:255], "flagged_by": by}
    )
    forget_marks()
    return created


def unmark(kind: str, obj: Any) -> bool:
    from .testdata_models import TestDataFlag

    object_id = getattr(obj, "equipment_id", None) or getattr(obj, "pk", None) or int(obj)
    deleted, _ = TestDataFlag.objects.filter(kind=kind, object_id=object_id).delete()
    forget_marks()
    return bool(deleted)

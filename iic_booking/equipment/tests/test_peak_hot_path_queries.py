"""Query-count regression tests for the booking page hot paths used at the weekly slot opening."""

from __future__ import annotations

import os
import time
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.core.signals import request_started
from django.db import connection, reset_queries
from django.test.utils import CaptureQueriesContext

from iic_booking.equipment.models import (
    DynamicInputField,
    DynamicInputFieldType,
    EquipmentManager,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import Wallet, WalletJoinRequest, WalletJoinRequestStatus
from iic_booking.users.repositories.wallet_repository import SubWalletRepository
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

PRINT_SQL = bool(os.environ.get("PEAK_PRINT_SQL"))


@pytest.fixture
def no_portal_lock(monkeypatch):
    from iic_booking.users.legacy_ledger import booking_lock

    monkeypatch.setattr(booking_lock, "booking_is_locked", lambda user: (False, ""))
    monkeypatch.setattr(booking_lock, "department_equipment_booking_blocked", lambda equipment, user: (False, ""))


@pytest.fixture
def hot(egs_factory):
    cache.clear()
    eq = egs_factory.equipment(time_formula="A*60")
    for key, label in (("A", "No. of Samples"), ("B", "Sample type"), ("C", "Remarks")):
        DynamicInputField.objects.create(
            equipment=eq,
            field_key=key,
            field_label=label,
            field_type=DynamicInputFieldType.NUMERIC if key == "A" else DynamicInputFieldType.TEXT,
            options={"min": 1, "max": 10} if key == "A" else {},
            editing_required=False,
        )
    oic = UserFactory(user_type=UserType.MANAGER, department=egs_factory.department, admin_approved=True)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    slots = []
    for day in range(1, 13):
        for hour in (9, 10, 11, 12, 14, 15, 16):
            slots.append(egs_factory.slot(eq, egs_factory.future(days=day, hour=hour)))
    student = egs_factory.student()
    faculty = UserFactory(user_type=UserType.FACULTY, department=egs_factory.department)
    wallet = Wallet.objects.create(user=faculty)
    WalletJoinRequest.objects.create(
        student=student, faculty=faculty, wallet=wallet, status=WalletJoinRequestStatus.APPROVED
    )
    SubWalletRepository.get_or_create(wallet, egs_factory.department).credit(Decimal("10000"), description="Recharge")
    return eq, slots, student, egs_factory.client_for(student)


def _measure(label, fn, *, repeat=3, warm=True):
    if warm:
        fn()  # warm per-process caches the same way a running worker has them
    # The test client fires request_started, which would clear the captured query log.
    request_started.disconnect(reset_queries)
    reset_queries()
    try:
        with CaptureQueriesContext(connection) as ctx:
            t0 = time.perf_counter()
            resp = fn()
            elapsed = (time.perf_counter() - t0) * 1000
        queries = list(ctx.captured_queries)
    finally:
        request_started.connect(reset_queries)
    if PRINT_SQL:
        for q in queries:
            print("   ", q["sql"][:220])
    print(f"{label}: {len(queries)} queries, {elapsed:.0f} ms")
    return resp, len(queries)


def test_equipment_detail_query_budget(hot):
    eq, _slots, _student, client = hot
    resp, n = _measure("equipment detail", lambda: client.get(f"/api/equipments/{eq.pk}/"))
    assert resp.status_code == 200
    assert n <= 28  # 32 before the request memo


def test_slot_availability_query_budget(hot):
    eq, slots, _student, client = hot
    start = slots[0].date
    end = start + timedelta(days=13)
    resp, n = _measure(
        "slots 2 weeks",
        lambda: client.get(f"/api/equipments/{eq.pk}/slots/", {"start_date": start.isoformat(), "end_date": end.isoformat()}),
    )
    assert resp.status_code == 200
    assert n <= 20


def test_charge_calculation_query_budget(hot):
    eq, _slots, _student, client = hot
    resp, n = _measure(
        "charge calculation",
        lambda: client.get(f"/api/equipments/{eq.pk}/calculate/", {"A": 2}),
    )
    assert resp.status_code == 200, resp.data
    assert n <= 16  # 27 before the request memo


def test_catalog_list_query_budget(hot):
    _eq, _slots, _student, client = hot
    resp, n = _measure("catalog list", lambda: client.get("/api/equipments/", {"include_ratings": "1"}))
    assert resp.status_code == 200
    assert n <= 8


def test_booking_create_query_budget(hot, no_portal_lock):
    eq, slots, student, client = hot
    free = iter(slots[20:])

    def book():
        slot = next(free)
        return client.post(
            f"/api/equipments/{eq.pk}/book/",
            {
                "slot_ids": [slot.pk],
                "start_time": slot.start_datetime.isoformat(),
                "end_time": slot.end_datetime.isoformat(),
                "input_values": {"A": 1},
            },
            format="json",
        )

    resp, n = _measure("booking create", book, repeat=1)
    assert resp.status_code in (200, 201), resp.data
    assert n <= 75


def test_slot_payload_fields_after_serializer_fast_paths(hot, no_portal_lock):
    eq, slots, _student, client = hot
    target = slots[30]
    resp = client.post(
        f"/api/equipments/{eq.pk}/book/",
        {
            "slot_ids": [target.pk],
            "start_time": target.start_datetime.isoformat(),
            "end_time": target.end_datetime.isoformat(),
            "input_values": {"A": 1},
        },
        format="json",
    )
    assert resp.status_code in (200, 201), resp.data
    target.refresh_from_db()

    resp = client.get(
        f"/api/equipments/{eq.pk}/slots/",
        {"start_date": target.date.isoformat(), "end_date": target.date.isoformat()},
    )
    assert resp.status_code == 200
    rows = resp.data if isinstance(resp.data, list) else resp.data.get("slots", resp.data.get("results", []))
    by_id = {row["id"]: row for row in rows}
    booked = by_id[target.pk]
    assert booked["status"] == "BOOKED"
    assert booked["status_display"] == target.get_status_display()
    assert booked["real_booking_id"] == target.booking_id
    assert booked["equipment_code"] == eq.code
    free = next(row for row in rows if row["id"] != target.pk)
    assert free["real_booking_id"] is None
    assert free["status_display"] in ("Available", "Home department only")


def test_request_memo_only_active_inside_requests():
    from iic_booking.equipment.request_memo import memo_active, memo_get_or_compute, request_memo

    calls = []
    assert not memo_active()
    memo_get_or_compute("k", lambda: calls.append(1))
    memo_get_or_compute("k", lambda: calls.append(1))
    assert len(calls) == 2
    with request_memo():
        assert memo_active()
        memo_get_or_compute("k", lambda: calls.append(1))
        memo_get_or_compute("k", lambda: calls.append(1))
    assert len(calls) == 3
    assert not memo_active()

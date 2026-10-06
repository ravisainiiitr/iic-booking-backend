"""Change Slot Status dashboard picker (OIC / Main Admin scope) and closing stale equipment notices."""

from __future__ import annotations

import uuid
from datetime import time, timedelta
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.communication.models import Notice
from iic_booking.communication.notice_board_service import public_notices_queryset
from iic_booking.equipment.models import (
    DailySlot,
    Equipment,
    EquipmentManager,
    EquipmentStatus,
    EquipmentTemporaryOIC,
    SlotMaster,
    SlotStatus,
)
from iic_booking.users.models.department import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

PICKER_URL = "/api/equipments/slot-status-picker/"


def _client(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _user(**kwargs):
    return UserFactory(admin_approved=True, **kwargs)


def _department(name: str) -> Department:
    return Department.objects.create(name=f"{name} {uuid.uuid4().hex[:4]}", code=f"D{uuid.uuid4().hex[:5]}")


def _equipment(**kwargs):
    defaults = {
        "name": f"EQ {uuid.uuid4().hex[:4]}",
        "code": f"SS{uuid.uuid4().hex[:5].upper()}",
        "slot_duration_minutes": 60,
        "user_rating_enabled": False,
        "status": EquipmentStatus.ACTIVE,
    }
    defaults.update(kwargs)
    return Equipment.objects.create(**defaults)


def _slot(equipment, start):
    master = SlotMaster.objects.create(
        equipment=equipment,
        slot_number=SlotMaster.objects.filter(equipment=equipment).count() + 1,
        open_time=time(9),
        close_time=time(10),
        is_active=True,
    )
    return DailySlot.objects.create(
        slot_master=master,
        date=timezone.localtime(start).date(),
        start_datetime=start,
        end_datetime=start + timedelta(hours=1),
        status=SlotStatus.AVAILABLE,
    )


def _ids(res):
    return {row["equipment_id"] for row in res.data["equipment"]}


# --- Change Slot Status picker -------------------------------------------------------------------


def test_picker_oic_sees_managed_and_temporary_equipment_only():
    oic = _user(user_type=UserType.MANAGER)
    other_oic = _user(user_type=UserType.MANAGER)
    mine, covering, not_mine, expired = _equipment(), _equipment(), _equipment(), _equipment()
    EquipmentManager.objects.create(equipment=mine, manager=oic)
    EquipmentTemporaryOIC.objects.create(
        equipment=covering, primary_oic=other_oic, temporary_oic=oic, resume_at=timezone.now() + timedelta(days=3)
    )
    EquipmentTemporaryOIC.objects.create(
        equipment=expired, primary_oic=other_oic, temporary_oic=oic, resume_at=timezone.now() - timedelta(days=1)
    )

    res = _client(oic).get(PICKER_URL)
    assert res.status_code == 200, res.data
    assert _ids(res) == {mine.pk, covering.pk}
    assert {o["equipment_id"] for o in res.data["filters"]["equipment_options"]} == {mine.pk, covering.pk}
    assert res.data["filters"]["scope"] == "equipment"
    temp = {row["equipment_id"]: row["temporary_oic"] for row in res.data["equipment"]}
    assert temp == {mine.pk: False, covering.pk: True}

    # Asking for someone else's equipment never widens the scope.
    assert _ids(_client(oic).get(PICKER_URL, {"equipment_id": not_mine.pk})) == set()


def test_picker_main_admin_filters_by_department_then_equipment():
    admin = _user(user_type=UserType.ADMIN)
    iic, chem = _department("IIC"), _department("Chemistry")
    a1, a2 = _equipment(internal_department=iic), _equipment(internal_department=iic)
    b1 = _equipment(internal_department=chem)
    _equipment(internal_department=iic, status=EquipmentStatus.DISPOSED)

    res = _client(admin).get(PICKER_URL, {"department_id": iic.pk})
    assert res.status_code == 200
    assert _ids(res) == {a1.pk, a2.pk}
    assert res.data["filters"]["scope"] == "all"
    assert res.data["filters"]["department_locked"] is False

    res = _client(admin).get(PICKER_URL, {"department_id": iic.pk, "equipment_id": a2.pk})
    assert _ids(res) == {a2.pk}
    assert {o["equipment_id"] for o in res.data["filters"]["equipment_options"]} == {a1.pk, a2.pk}

    assert b1.pk in _ids(_client(admin).get(PICKER_URL))


@pytest.mark.parametrize("user_type", [UserType.DEPT_ADMIN, UserType.OPERATOR, UserType.STUDENT])
def test_picker_refuses_roles_that_cannot_change_slot_status(user_type):
    assert _client(_user(user_type=user_type)).get(PICKER_URL).status_code == 403


def test_temporary_oic_can_change_slots_only_while_covering():
    oic = _user(user_type=UserType.MANAGER)
    primary = _user(user_type=UserType.MANAGER)
    covering, lapsed = _equipment(), _equipment()
    EquipmentTemporaryOIC.objects.create(
        equipment=covering, primary_oic=primary, temporary_oic=oic, resume_at=timezone.now() + timedelta(days=2)
    )
    EquipmentTemporaryOIC.objects.create(
        equipment=lapsed, primary_oic=primary, temporary_oic=oic, resume_at=timezone.now() - timedelta(hours=1)
    )
    start = timezone.now() + timedelta(days=3)
    slot, lapsed_slot = _slot(covering, start), _slot(lapsed, start)

    with patch("iic_booking.users.rbac.user_has_admin_panel_access", return_value=False), patch(
        "config.admin_panel_access_api.user_can_access_admin_module", return_value=False
    ):
        res = _client(oic).post(
            f"/api/admin/equipment/{covering.pk}/bulk-slot-status/",
            {"slot_ids": [slot.id], "status": SlotStatus.BLOCKED},
            format="json",
        )
        assert res.status_code == 200, res.data
        res = _client(oic).post(
            f"/api/admin/equipment/{lapsed.pk}/bulk-slot-status/",
            {"slot_ids": [lapsed_slot.id], "status": SlotStatus.BLOCKED},
            format="json",
        )
        assert res.status_code == 404
    slot.refresh_from_db()
    lapsed_slot.refresh_from_db()
    assert slot.status == SlotStatus.BLOCKED
    assert lapsed_slot.status == SlotStatus.AVAILABLE


def test_oic_cannot_change_operational_status_of_other_equipment():
    oic = _user(user_type=UserType.MANAGER)
    mine, not_mine = _equipment(), _equipment()
    EquipmentManager.objects.create(equipment=mine, manager=oic)

    with patch("iic_booking.equipment.api_views.user_can_see_equipment", return_value=True):
        res = _client(oic).patch(f"/api/equipments/{not_mine.pk}/", {"status": EquipmentStatus.REPAIR}, format="json")
        assert res.status_code == 403
        assert "Officer In-charge" in res.data["error"]
        not_mine.refresh_from_db()
        assert not_mine.status == EquipmentStatus.ACTIVE

        res = _client(oic).patch(f"/api/equipments/{mine.pk}/", {"status": EquipmentStatus.REPAIR}, format="json")
        assert res.status_code == 200, res.data


# --- Stale equipment notices ---------------------------------------------------------------------


def _published_notice(equipment, **kwargs):
    defaults = {
        "title": "Under Maintenance",
        "description": "Down",
        "equipment": equipment,
        "source": Notice.Source.EQUIPMENT_UNAVAILABLE,
        "approval_status": Notice.ApprovalStatus.APPROVED,
        "is_active": True,
    }
    defaults.update(kwargs)
    return Notice.objects.create(**defaults)


def test_operational_via_plain_save_closes_published_notice():
    """Django admin / equipment settings save the model directly; the notice must still close."""
    eq = _equipment(status=EquipmentStatus.REPAIR)
    notice = _published_notice(eq)
    pending = _published_notice(eq, approval_status=Notice.ApprovalStatus.PENDING, is_active=False)

    eq.status = EquipmentStatus.ACTIVE
    eq.save()

    notice.refresh_from_db()
    pending.refresh_from_db()
    assert notice.is_active is False
    assert notice.expiry_date is not None and notice.expiry_date <= timezone.now()
    assert notice.approval_status == Notice.ApprovalStatus.APPROVED
    assert pending.approval_status == Notice.ApprovalStatus.REJECTED
    assert Notice.objects.filter(pk__in=[notice.pk, pending.pk]).count() == 2


def test_status_patch_reports_closed_notice_once():
    admin = _user(user_type=UserType.ADMIN)
    eq = _equipment(status=EquipmentStatus.REPAIR)
    _published_notice(eq)

    res = _client(admin).patch(f"/api/equipments/{eq.pk}/", {"status": EquipmentStatus.ACTIVE}, format="json")
    assert res.status_code == 200, res.data
    assert res.data["notice_board"] == {"notices_closed": 1, "notice_closed_on_operational": True}


def test_closing_keeps_earlier_closed_notices_untouched():
    eq = _equipment(status=EquipmentStatus.REPAIR)
    old_expiry = timezone.now() - timedelta(days=30)
    old = _published_notice(eq, is_active=False, expiry_date=old_expiry)

    eq.status = EquipmentStatus.ACTIVE
    eq.save()

    old.refresh_from_db()
    assert old.expiry_date == old_expiry


def test_public_board_hides_auto_notice_once_equipment_is_operational():
    eq = _equipment(status=EquipmentStatus.REPAIR)
    down = _equipment(status=EquipmentStatus.REPAIR)
    stale = _published_notice(eq)
    current = _published_notice(down)
    manual = _published_notice(eq, source=Notice.Source.MANUAL)
    Equipment.objects.filter(pk=eq.pk).update(status=EquipmentStatus.ACTIVE)

    visible = set(public_notices_queryset().values_list("pk", flat=True))
    assert stale.pk not in visible
    assert {current.pk, manual.pk} <= visible

    res = APIClient().get("/api/notices/")
    assert res.status_code == 200
    assert stale.pk not in {n["notice_id"] for n in res.data["notices"]}


def test_cleanup_command_dry_run_then_apply_is_idempotent():
    operational = _equipment(status=EquipmentStatus.REPAIR)
    still_down = _equipment(status=EquipmentStatus.REPAIR)
    stale = _published_notice(operational)
    stale_draft = _published_notice(operational, approval_status=Notice.ApprovalStatus.DRAFT, is_active=False)
    keep_down = _published_notice(still_down)
    keep_manual = _published_notice(operational, source=Notice.Source.MANUAL)
    # Restored with a queryset update (no signals), as old rows in production were.
    Equipment.objects.filter(pk=operational.pk).update(status=EquipmentStatus.ACTIVE)

    out = StringIO()
    call_command("expire_stale_equipment_notices", stdout=out)
    assert "mode=dry-run" in out.getvalue()
    assert "stale_open_notices=2" in out.getvalue()
    stale.refresh_from_db()
    assert stale.is_active is True

    out = StringIO()
    call_command("expire_stale_equipment_notices", "--apply", stdout=out)
    assert "closed=2" in out.getvalue()
    assert "stale_open_notices_after=0" in out.getvalue()
    for n in (stale, stale_draft, keep_down, keep_manual):
        n.refresh_from_db()
    assert stale.is_active is False
    assert stale_draft.approval_status == Notice.ApprovalStatus.REJECTED
    assert keep_down.is_active is True
    assert keep_manual.is_active is True and keep_manual.expiry_date is None

    out = StringIO()
    call_command("expire_stale_equipment_notices", "--apply", stdout=out)
    assert "stale_open_notices=0" in out.getvalue()
    assert "closed=0" in out.getvalue()
    assert Notice.objects.count() == 4

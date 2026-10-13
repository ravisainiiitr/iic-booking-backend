"""An OIC's equipment-unavailability notice goes on the notice board without Main Admin approval."""

from __future__ import annotations

import importlib
import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.apps import apps as django_apps
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.communication.models import Notice
from iic_booking.communication.notice_board_service import public_notices_queryset
from iic_booking.equipment.models import Equipment, EquipmentManager, EquipmentStatus
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def _client(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _equipment(**kwargs):
    defaults = {
        "name": f"EQ {uuid.uuid4().hex[:4]}",
        "code": f"NB{uuid.uuid4().hex[:5].upper()}",
        "slot_duration_minutes": 60,
        "user_rating_enabled": False,
        "status": EquipmentStatus.ACTIVE,
    }
    defaults.update(kwargs)
    return Equipment.objects.create(**defaults)


def _oic_marks_unavailable():
    oic = UserFactory(admin_approved=True, user_type=UserType.MANAGER)
    equipment = _equipment()
    EquipmentManager.objects.create(equipment=equipment, manager=oic)
    with patch("iic_booking.equipment.api_views.user_can_see_equipment", return_value=True):
        res = _client(oic).patch(f"/api/equipments/{equipment.pk}/", {"status": EquipmentStatus.REPAIR}, format="json")
    assert res.status_code == 200, res.data
    return oic, equipment, res.data["notice_board"]["notice_request_id"]


def test_oic_push_to_notice_board_publishes_without_main_admin():
    UserFactory(admin_approved=True, user_type=UserType.ADMIN)
    oic, equipment, notice_id = _oic_marks_unavailable()
    assert notice_id not in set(public_notices_queryset().values_list("pk", flat=True))

    res = _client(oic).patch(
        f"/api/notice-requests/{notice_id}/complete-expiry/", {"expiry_unlimited": True}, format="json"
    )
    assert res.status_code == 200, res.data

    notice = Notice.objects.get(pk=notice_id)
    assert (notice.approval_status, notice.is_active, notice.needs_oic_expiry) == (
        Notice.ApprovalStatus.APPROVED,
        True,
        False,
    )
    assert notice.reviewed_by == oic
    assert "do not need approval" in notice.review_comment
    assert notice_id in {n["notice_id"] for n in APIClient().get("/api/notices/").data["notices"]}
    assert not Notice.objects.filter(approval_status=Notice.ApprovalStatus.PENDING).exists()


def test_expiry_in_the_past_is_refused():
    oic, _, notice_id = _oic_marks_unavailable()
    past = (timezone.now() - timedelta(hours=1)).isoformat()
    res = _client(oic).patch(f"/api/notice-requests/{notice_id}/complete-expiry/", {"expiry_date": past}, format="json")
    assert res.status_code == 400
    assert Notice.objects.get(pk=notice_id).approval_status == Notice.ApprovalStatus.DRAFT


def test_generic_notice_request_still_needs_main_admin_approval():
    oic = UserFactory(admin_approved=True, user_type=UserType.MANAGER)
    res = _client(oic).post(
        "/api/notice-requests/mine/", {"title": "Lab closed", "description": "Closed on Friday"}, format="json"
    )
    assert res.status_code == 201, res.data
    assert Notice.objects.get(pk=res.data["notice_id"]).approval_status == Notice.ApprovalStatus.PENDING


def test_migration_publishes_pending_equipment_notices_still_relevant():
    down = _equipment(status=EquipmentStatus.REPAIR)
    back = _equipment(status=EquipmentStatus.ACTIVE)

    def pending(equipment, **kwargs):
        return Notice.objects.create(
            title="Under Maintenance",
            description="Down",
            equipment=equipment,
            source=Notice.Source.EQUIPMENT_UNAVAILABLE,
            approval_status=Notice.ApprovalStatus.PENDING,
            is_active=False,
            **kwargs,
        )

    current = pending(down)
    expired = pending(down, expiry_date=timezone.now() - timedelta(days=1))
    operational = pending(back)
    manual = Notice.objects.create(
        title="Generic", description="x", approval_status=Notice.ApprovalStatus.PENDING, is_active=False
    )

    migration = importlib.import_module("iic_booking.communication.migrations.0057_publish_pending_equipment_notices")
    migration.publish_pending(django_apps, None)

    statuses = dict(Notice.objects.filter(pk__in=[current.pk, expired.pk, operational.pk, manual.pk]).values_list("pk", "approval_status"))
    assert statuses == {
        current.pk: Notice.ApprovalStatus.APPROVED,
        expired.pk: Notice.ApprovalStatus.PENDING,
        operational.pk: Notice.ApprovalStatus.PENDING,
        manual.pk: Notice.ApprovalStatus.PENDING,
    }
    assert Notice.objects.get(pk=current.pk).is_active is True

"""Management commands and the equipment-mapping verification action."""

from __future__ import annotations

import pytest
from django.contrib.admin.sites import AdminSite
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import RequestFactory

from iic_booking.istem.admin import IstemEquipmentMappingAdmin
from iic_booking.istem.choices import Operation, SyncState
from iic_booking.istem.models import IstemBookingLink, IstemEquipmentMapping, IstemOutboxJob
from iic_booking.users.tests.factories import UserFactory

from .support import apply_live, make_booking, map_equipment, ready_booking

pytestmark = pytest.mark.django_db


def test_sync_command_in_shadow_mode_queues_and_validates_without_sending(settings, capsys):
    settings.ISTEM_ENABLED, settings.ISTEM_MODE = True, "shadow"
    ready_booking()
    call_command("istem_sync", "--enqueue", "--process")
    out = capsys.readouterr().out
    assert "Queued: 1" in out and "DONE" in out
    link = IstemBookingLink.objects.get()
    assert link.sync_state == SyncState.CREATE_READY and link.create_payload


def test_sync_command_refuses_live_mode_without_connection_settings(settings):
    apply_live(settings, ISTEM_PASSWORD="")
    with pytest.raises(CommandError, match="ISTEM_PASSWORD"):
        call_command("istem_sync", "--process")


def test_sync_command_needs_an_action(settings):
    settings.ISTEM_ENABLED, settings.ISTEM_MODE = True, "shadow"
    with pytest.raises(CommandError):
        call_command("istem_sync")


def test_recover_command_checks_permissions(settings):
    settings.ISTEM_ENABLED, settings.ISTEM_MODE = True, "shadow"
    booking = ready_booking()
    IstemBookingLink.objects.create(booking=booking, sync_state=SyncState.CREATE_UNCERTAIN)
    actor = UserFactory(admin_approved=True)
    with pytest.raises(CommandError, match="Permission denied"):
        call_command("istem_recover", "adopt-ids", "--actor", actor.email, "--booking", str(booking.pk),
                     "--request-id", "55401", "--fbr-no", "1976125722", "--evidence-type", "ISTEM_PORTAL",
                     "--evidence-reference", "portal", "--reason", "Booking found on the I-STEM portal search")


def test_recover_command_refuses_confirmation_while_disabled(settings):
    settings.ISTEM_ENABLED, settings.ISTEM_MODE = True, "shadow"
    booking = ready_booking()
    IstemBookingLink.objects.create(booking=booking, sync_state=SyncState.CREATED, request_id="55401")
    admin_user = UserFactory(admin_approved=True, is_superuser=True)
    with pytest.raises(CommandError, match="disabled"):
        call_command("istem_recover", "request-confirmation", "--actor", admin_user.email, "--booking", str(booking.pk))
    assert not IstemOutboxJob.objects.filter(operation=Operation.CONFIRM_BOOKING).exists()


def _admin_request(user):
    request = RequestFactory().post("/")
    request.user = user
    request._messages = type("M", (), {"add": lambda *a, **k: None})()
    return request


def test_mark_verified_action_records_who_verified():
    booking = make_booking()
    mapping = map_equipment(booking.equipment, verified=False)
    user = UserFactory(admin_approved=True, is_superuser=True, is_staff=True)
    model_admin = IstemEquipmentMappingAdmin(IstemEquipmentMapping, AdminSite())
    model_admin.mark_verified(_admin_request(user), IstemEquipmentMapping.objects.all())
    mapping.refresh_from_db()
    assert mapping.verified_by == user and mapping.verified_at is not None


def test_changing_the_istem_id_clears_verification():
    booking = make_booking()
    mapping = map_equipment(booking.equipment)
    user = UserFactory(admin_approved=True, is_superuser=True, is_staff=True)
    model_admin = IstemEquipmentMappingAdmin(IstemEquipmentMapping, AdminSite())
    mapping.istem_equipment_id = "99999"
    form = type("F", (), {"changed_data": ["istem_equipment_id"]})()
    model_admin.save_model(_admin_request(user), mapping, form, change=True)
    mapping.refresh_from_db()
    assert mapping.verified_at is None and mapping.verified_by is None

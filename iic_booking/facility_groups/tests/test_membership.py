from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from iic_booking.equipment.models import Booking, BookingStatus
from iic_booking.facility_groups import membership
from iic_booking.facility_groups.audience import AudienceFilters, members_qs
from iic_booking.facility_groups.models import FacilityUserGroup, FacilityUserGroupMember, GroupKind
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import Wallet, WalletJoinRequest, WalletJoinRequestStatus

from .conftest import make_booking, make_user

pytestmark = pytest.mark.django_db


def member(key, user):
    return FacilityUserGroupMember.objects.filter(group__auto_key=key, user=user).first()


def test_booking_adds_user_to_equipment_category_lab_and_all_groups(world, run_on_commit):
    run_on_commit(make_booking, world.external, world.fesem)

    keys = set(FacilityUserGroup.objects.values_list("auto_key", flat=True))
    assert keys == {"all", f"equipment:{world.fesem.pk}", f"category:{world.em.pk}", f"lab:{world.lab.pk}"}
    category = FacilityUserGroup.objects.get(auto_key=f"category:{world.em.pk}")
    assert (category.kind, category.name) == (GroupKind.CATEGORY, "Electron Microscopy")
    m = member(f"equipment:{world.fesem.pk}", world.external)
    assert m.booking_count == 1 and m.first_booked_at == m.last_booked_at


def test_counts_grow_and_mode_children_roll_up_to_base_instrument(world, run_on_commit):
    run_on_commit(make_booking, world.external, world.fesem)
    run_on_commit(make_booking, world.external, world.fesem_mode)
    run_on_commit(make_booking, world.external, world.tem)

    assert member(f"equipment:{world.fesem.pk}", world.external).booking_count == 2
    assert not FacilityUserGroup.objects.filter(auto_key=f"equipment:{world.fesem_mode.pk}").exists()
    # The mode has no category / lab of its own: it falls back to the base instrument's.
    assert member(f"category:{world.em.pk}", world.external).booking_count == 3
    assert member(f"lab:{world.lab.pk}", world.external).booking_count == 3
    assert member("all", world.external).booking_count == 3


def test_pending_payment_is_not_counted_until_paid(world, run_on_commit):
    booking = run_on_commit(make_booking, world.external, world.xrd, status=BookingStatus.PENDING_PAYMENT)
    assert not FacilityUserGroupMember.objects.exists()

    booking = Booking.objects.get(pk=booking.pk)
    booking.status = BookingStatus.BOOKED
    run_on_commit(booking.save)
    assert member(f"equipment:{world.xrd.pk}", world.external).booking_count == 1


def test_cancelled_bookings_still_count(world, run_on_commit):
    run_on_commit(make_booking, world.external, world.xrd, status=BookingStatus.CANCELLED)
    assert member(f"equipment:{world.xrd.pk}", world.external).booking_count == 1


def test_supervisor_recorded_and_hidden_unless_requested(world, run_on_commit):
    run_on_commit(make_booking, world.student, world.xrd)
    run_on_commit(make_booking, world.student, world.xrd)

    sup = member(f"equipment:{world.xrd.pk}", world.faculty)
    assert (sup.booking_count, sup.supervised_booking_count) == (0, 2)
    group = FacilityUserGroup.objects.get(auto_key=f"equipment:{world.xrd.pk}")
    assert set(members_qs(group, AudienceFilters()).values_list("user_id", flat=True)) == {world.student.pk}
    with_sup = members_qs(group, AudienceFilters(include_supervisors=True)).values_list("user_id", flat=True)
    assert set(with_sup) == {world.student.pk, world.faculty.pk}


def test_supervisor_from_faculty_wallet_link(world, run_on_commit):
    other_faculty = make_user(user_type=UserType.FACULTY, name="Wallet Owner", department=world.chem)
    wallet, _ = Wallet.objects.get_or_create(user=other_faculty)
    student = make_user(user_type=UserType.STUDENT, name="Joined Student", department=world.chem)
    WalletJoinRequest.objects.create(
        student=student, faculty=other_faculty, wallet=wallet, status=WalletJoinRequestStatus.APPROVED
    )
    run_on_commit(make_booking, student, world.xrd)
    assert member(f"equipment:{world.xrd.pk}", other_faculty).supervised_booking_count == 1


def test_faculty_own_booking_keeps_supervised_count(world, run_on_commit):
    run_on_commit(make_booking, world.student, world.xrd)
    run_on_commit(make_booking, world.faculty, world.xrd)
    m = member(f"equipment:{world.xrd.pk}", world.faculty)
    assert (m.booking_count, m.supervised_booking_count) == (1, 1)


def test_saves_without_status_change_do_not_resync(world, run_on_commit, monkeypatch):
    booking = run_on_commit(make_booking, world.external, world.xrd)
    calls = []
    monkeypatch.setattr(membership, "sync_booking", lambda *a, **k: calls.append(a))
    booking = Booking.objects.get(pk=booking.pk)
    booking.notes = "updated"
    run_on_commit(booking.save)
    assert calls == []


def test_sync_failure_never_breaks_booking(world, run_on_commit, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("table missing")

    monkeypatch.setattr(membership, "sync_booking", boom)
    booking = run_on_commit(make_booking, world.external, world.xrd)
    assert Booking.objects.filter(pk=booking.pk).exists()


def test_auto_membership_switch(world, run_on_commit, settings):
    settings.FACILITY_GROUPS_AUTO_MEMBERSHIP = False
    run_on_commit(make_booking, world.external, world.xrd)
    assert not FacilityUserGroupMember.objects.exists()


def test_backfill_dry_run_then_apply_is_idempotent(world):
    make_booking(world.student, world.fesem)
    make_booking(world.external, world.xrd)
    make_booking(world.external, world.xrd, status=BookingStatus.PENDING_PAYMENT)
    assert not FacilityUserGroupMember.objects.exists()

    dry = membership.rebuild_all(apply=False)
    assert dry["bookings_counted"] == 2
    assert dry["members_to_create"] > 0
    assert not FacilityUserGroup.objects.exists()

    applied = membership.rebuild_all(apply=True)
    assert applied["members_to_create"] == dry["members_to_create"]
    assert member(f"equipment:{world.xrd.pk}", world.external).booking_count == 1
    assert member(f"lab:{world.lab.pk}", world.faculty).supervised_booking_count == 1

    again = membership.rebuild_all(apply=True)
    assert (again["members_to_create"], again["members_to_update"], again["members_to_remove"]) == (0, 0, 0)


def test_backfill_matches_live_sync(world, run_on_commit):
    run_on_commit(make_booking, world.student, world.fesem_mode)
    run_on_commit(make_booking, world.external, world.tem)
    summary = membership.rebuild_all(apply=False)
    assert (summary["members_to_create"], summary["members_to_update"], summary["members_to_remove"]) == (0, 0, 0)


def test_backfill_removes_stale_automatic_members_but_keeps_manual(world):
    booking = make_booking(world.external, world.xrd)
    membership.rebuild_all(apply=True)
    group = FacilityUserGroup.objects.get(auto_key=f"equipment:{world.xrd.pk}")
    FacilityUserGroupMember.objects.create(group=group, user=world.operator, added_manually=True)
    Booking.objects.filter(pk=booking.pk).update(status=BookingStatus.PENDING_PAYMENT)

    summary = membership.rebuild_all(apply=True)
    assert summary["members_to_remove"] >= 1
    assert not member(f"equipment:{world.xrd.pk}", world.external)
    assert member(f"equipment:{world.xrd.pk}", world.operator)


def test_backfill_command_dry_run_by_default(world):
    make_booking(world.external, world.xrd)
    out = StringIO()
    call_command("backfill_facility_groups", stdout=out)
    assert "DRY RUN" in out.getvalue()
    assert "bookings_counted: 1" in out.getvalue()
    assert not FacilityUserGroupMember.objects.exists()

    call_command("backfill_facility_groups", "--apply", stdout=StringIO())
    assert member("all", world.external).booking_count == 1


def test_equipment_rename_renames_group(world, run_on_commit):
    run_on_commit(make_booking, world.external, world.xrd)
    world.xrd.name = "XRD Smartlab"
    world.xrd.save()
    run_on_commit(make_booking, world.external, world.xrd)
    assert FacilityUserGroup.objects.get(auto_key=f"equipment:{world.xrd.pk}").name.startswith("XRD Smartlab")


def test_date_range_filter_uses_booking_date(world, run_on_commit):
    old = run_on_commit(make_booking, world.external, world.xrd)
    Booking.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=60))
    run_on_commit(make_booking, world.student, world.xrd)
    group = FacilityUserGroup.objects.get(auto_key=f"equipment:{world.xrd.pk}")
    recent = AudienceFilters(booked_from=timezone.localdate() - timedelta(days=7))
    assert set(members_qs(group, recent).values_list("user_id", flat=True)) == {world.student.pk}

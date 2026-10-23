"""Database rules of the I-STEM integration tables: one link per booking / user / instrument, each remote
identifier owned by one booking, one live outbox job per key, and audit rows that cannot be orphaned."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError

from iic_booking.equipment.models import Booking, BookingStatus, ChargeProfile, Equipment
from iic_booking.istem.choices import CodeType, JobState, Operation, SyncState
from iic_booking.istem.models import (
    IstemBookingLink,
    IstemCodeMapping,
    IstemEquipmentMapping,
    IstemOutboxJob,
    IstemSyncEvent,
    IstemUserLink,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


def _equipment():
    return Equipment.objects.create(
        name="I-STEM EQ", code=f"IS{uuid.uuid4().hex[:5].upper()}", slot_duration_minutes=60, user_rating_enabled=False
    )


def _booking(user=None, equipment=None):
    equipment = equipment or _equipment()
    profile = ChargeProfile.objects.create(
        equipment=equipment, user_type=UserType.EXTERNAL, primary_unit_charge=Decimal("100.00"), require_istem_fbr=True
    )
    return Booking.objects.create(
        user=user or UserFactory(user_type=UserType.EXTERNAL),
        equipment=equipment,
        charge_profile=profile,
        user_type_snapshot=UserType.EXTERNAL,
        status=BookingStatus.BOOKED,
        total_charge=Decimal("100.00"),
        total_time_minutes=60,
        virtual_booking_id=f"IIC{equipment.code}{uuid.uuid4().hex[:6].upper()}",
    )


def _link(**kwargs):
    return IstemBookingLink.objects.create(booking=kwargs.pop("booking", None) or _booking(), **kwargs)


def _violates():
    """Expect an IntegrityError without breaking the surrounding test transaction."""
    return pytest.raises(IntegrityError)


# --- Booking links ---------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_new_link_starts_not_eligible_with_no_remote_ids():
    link = _link()

    assert link.sync_state == SyncState.NOT_ELIGIBLE
    assert link.request_id is None
    assert link.fbr_no is None
    assert link.confirmation_id is None
    assert link.needs_review is False


@pytest.mark.django_db
def test_a_booking_has_at_most_one_link():
    booking = _booking()
    _link(booking=booking)

    with _violates(), transaction.atomic():
        _link(booking=booking)


@pytest.mark.django_db
@pytest.mark.parametrize("field", ["request_id", "fbr_no", "confirmation_id"])
def test_a_remote_identifier_belongs_to_one_booking(field):
    _link(**{field: "R-1"})

    with _violates(), transaction.atomic():
        _link(**{field: "R-1"})


@pytest.mark.django_db
@pytest.mark.parametrize("missing", [None, ""])
def test_missing_remote_identifiers_never_collide(missing):
    _link(request_id=missing, fbr_no=missing, confirmation_id=missing)
    _link(request_id=missing, fbr_no=missing, confirmation_id=missing)

    assert IstemBookingLink.objects.count() == 2


@pytest.mark.django_db
def test_booking_with_a_link_cannot_be_deleted():
    link = _link()

    with pytest.raises(ProtectedError) as exc:
        link.booking.delete()
    assert link in exc.value.protected_objects


@pytest.mark.django_db
def test_link_with_audit_events_cannot_be_deleted():
    link = _link()
    event = IstemSyncEvent.objects.create(booking_link=link, from_state="", to_state=SyncState.NOT_ELIGIBLE)

    with pytest.raises(ProtectedError) as exc:
        link.delete()
    assert event in exc.value.protected_objects


# --- User links ------------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_user_has_at_most_one_istem_link():
    user = UserFactory()
    IstemUserLink.objects.create(user=user)

    with _violates(), transaction.atomic():
        IstemUserLink.objects.create(user=user)


@pytest.mark.django_db
def test_two_local_users_may_resolve_to_one_istem_user():
    # Create User returns the existing I-STEM user for a known email *or* mobile; this is reviewed, not blocked.
    IstemUserLink.objects.create(user=UserFactory(), public_user_id="PU-1")
    IstemUserLink.objects.create(user=UserFactory(), public_user_id="PU-1")

    assert IstemUserLink.objects.filter(public_user_id="PU-1").count() == 2


# --- Mappings --------------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_instrument_has_at_most_one_equipment_mapping():
    equipment = _equipment()
    IstemEquipmentMapping.objects.create(equipment=equipment, istem_equipment_id="EQ-1")

    with _violates(), transaction.atomic():
        IstemEquipmentMapping.objects.create(equipment=equipment, istem_equipment_id="EQ-2")


@pytest.mark.django_db
def test_global_code_mapping_is_unique_per_type_and_key():
    IstemCodeMapping.objects.create(code_type=CodeType.USER_TYPE, local_key=UserType.EXTERNAL, istem_value="A")

    with _violates(), transaction.atomic():
        IstemCodeMapping.objects.create(code_type=CodeType.USER_TYPE, local_key=UserType.EXTERNAL, istem_value="B")


@pytest.mark.django_db
def test_equipment_override_is_unique_per_instrument():
    equipment = _equipment()
    IstemCodeMapping.objects.create(code_type=CodeType.SERVICE_TYPE, local_key="*", equipment=equipment, istem_value="A")

    with _violates(), transaction.atomic():
        IstemCodeMapping.objects.create(
            code_type=CodeType.SERVICE_TYPE, local_key="*", equipment=equipment, istem_value="B"
        )


@pytest.mark.django_db
def test_global_mapping_and_equipment_overrides_coexist():
    IstemCodeMapping.objects.create(code_type=CodeType.SERVICE_TYPE, local_key="*", istem_value="G")
    IstemCodeMapping.objects.create(code_type=CodeType.SERVICE_TYPE, local_key="*", equipment=_equipment(), istem_value="E1")
    IstemCodeMapping.objects.create(code_type=CodeType.SERVICE_TYPE, local_key="*", equipment=_equipment(), istem_value="E2")

    assert IstemCodeMapping.objects.filter(code_type=CodeType.SERVICE_TYPE).count() == 3


# --- Outbox and audit ------------------------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("live_state", [JobState.PENDING, JobState.CLAIMED, JobState.RETRY_WAIT])
def test_only_one_live_job_per_dedupe_key(live_state):
    link = _link()
    IstemOutboxJob.objects.create(
        operation=Operation.CREATE_BOOKING, booking_link=link, dedupe_key="CREATE_BOOKING:1", state=live_state
    )

    with _violates(), transaction.atomic():
        IstemOutboxJob.objects.create(
            operation=Operation.CREATE_BOOKING, booking_link=link, dedupe_key="CREATE_BOOKING:1"
        )


@pytest.mark.django_db
@pytest.mark.parametrize("finished_state", [JobState.DONE, JobState.CANCELLED, JobState.BLOCKED])
def test_finished_jobs_do_not_block_a_new_one(finished_state):
    link = _link()
    IstemOutboxJob.objects.create(
        operation=Operation.CREATE_BOOKING, booking_link=link, dedupe_key="CREATE_BOOKING:1", state=finished_state
    )
    IstemOutboxJob.objects.create(operation=Operation.CREATE_BOOKING, booking_link=link, dedupe_key="CREATE_BOOKING:1")

    assert IstemOutboxJob.objects.filter(dedupe_key="CREATE_BOOKING:1").count() == 2


@pytest.mark.django_db
def test_job_needs_a_booking_or_user_link():
    with _violates(), transaction.atomic():
        IstemOutboxJob.objects.create(operation=Operation.REFRESH_STATUS, dedupe_key="REFRESH_STATUS:x")


@pytest.mark.django_db
def test_sync_event_needs_a_booking_or_user_link():
    with _violates(), transaction.atomic():
        IstemSyncEvent.objects.create(from_state="", to_state=SyncState.MANUAL_REVIEW)

"""Outbox processing, ambiguous-outcome handling, user mapping, confirmation and administrator recovery, with a
fake transport (no network)."""

from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.core.management.base import CommandError

from iic_booking.equipment.models import BookingStatus
from iic_booking.istem import contract, service
from iic_booking.istem.choices import (
    AttemptOutcome,
    CodeType,
    ErrorCategory,
    EvidenceType,
    JobState,
    Operation,
    RemoteStatus,
    SyncState,
    UserLinkState,
)
from iic_booking.istem.client import IstemClient, clear_token_cache
from iic_booking.istem.errors import IstemConfigError, IstemError
from iic_booking.istem.models import (
    IstemApiAttempt,
    IstemBookingLink,
    IstemCodeMapping,
    IstemOutboxJob,
    IstemSyncEvent,
    IstemUserLink,
)
from iic_booking.istem.outbox import add_job, enqueue_booking_sync
from iic_booking.users.tests.factories import UserFactory

from .support import (
    FAKE_INSTITUTE_ID,
    FAKE_PASSWORD,
    FAKE_TOKEN,
    FakeTransport,
    apply_live,
    json_response,
    link_user,
    listing_response,
    listing_row,
    make_booking,
    map_codes,
    map_equipment,
    ready_booking,
    token_response,
)

pytestmark = pytest.mark.django_db

CREATED = {"status": "Sucess", "message": "Done", "file_path": "x.pdf", "new_service_id": 55401, "fbr_no": "1976125722"}


@pytest.fixture(autouse=True)
def _fresh_tokens():
    clear_token_cache()
    yield
    clear_token_cache()


@pytest.fixture
def live(settings):
    apply_live(settings)
    return settings


@pytest.fixture
def shadow(settings):
    settings.ISTEM_ENABLED, settings.ISTEM_MODE = True, "shadow"
    return settings


def factory(transport):
    return lambda: IstemClient(transport)


def _queue(booking):
    link = enqueue_booking_sync(booking, trigger="test")
    return link, IstemOutboxJob.objects.get(booking_link=link, operation=Operation.CREATE_BOOKING, state=JobState.PENDING)


def _admin(*perms, superuser=False):
    user = UserFactory(admin_approved=True, is_superuser=superuser, is_staff=True)
    for codename in perms:
        user.user_permissions.add(Permission.objects.get(content_type__app_label="istem", codename=codename))
    return type(user).objects.get(pk=user.pk)  # drop the permission cache


# --- disabled / eligibility / dedupe -----------------------------------------------------------------------------

def test_off_mode_writes_nothing_and_processes_nothing(settings):
    settings.ISTEM_ENABLED = False
    booking = ready_booking()
    assert enqueue_booking_sync(booking, trigger="test") is None
    assert service.enqueue_eligible() == 0
    assert not IstemBookingLink.objects.exists()
    assert not IstemOutboxJob.objects.exists()
    with pytest.raises(CommandError):
        call_command("istem_sync", "--process")


@pytest.mark.parametrize("status", [BookingStatus.PENDING_PAYMENT, BookingStatus.WAITLISTED, BookingStatus.CANCELLED, BookingStatus.REFUNDED])
def test_bookings_that_are_not_booked_are_not_queued(shadow, status):
    booking = ready_booking(status=status)
    link = enqueue_booking_sync(booking, trigger="test")
    assert link.sync_state == SyncState.NOT_ELIGIBLE
    assert not IstemOutboxJob.objects.exists()


def test_unpaid_booking_is_not_queued(shadow):
    booking = ready_booking()
    booking.amount_due = 100
    booking.payment_settled_at = None
    booking.save()
    assert enqueue_booking_sync(booking, trigger="test").eligibility_reason == "payment not settled"
    assert not IstemOutboxJob.objects.exists()


def test_bookings_before_the_cut_over_are_not_queued(shadow):
    shadow.ISTEM_SYNC_START_AT = "2999-01-01T00:00:00"
    booking = ready_booking()
    assert enqueue_booking_sync(booking, trigger="test").sync_state == SyncState.NOT_ELIGIBLE


def test_repeated_enqueue_keeps_one_live_job(shadow):
    booking = ready_booking()
    for _ in range(3):
        enqueue_booking_sync(booking, trigger="test")
    assert IstemOutboxJob.objects.filter(operation=Operation.CREATE_BOOKING).count() == 1


def test_discovery_queues_eligible_bookings_only(shadow):
    eligible = ready_booking()
    make_booking(status=BookingStatus.CANCELLED)
    assert service.enqueue_eligible() == 1
    assert IstemOutboxJob.objects.get().booking_link.booking == eligible


# --- shadow mode -------------------------------------------------------------------------------------------------

def test_shadow_mode_validates_and_records_without_any_request(shadow):
    booking = ready_booking()
    link, job = _queue(booking)
    transport = FakeTransport()
    assert service.process_job(job.pk, client_factory=factory(transport)) == JobState.DONE
    link.refresh_from_db()
    assert transport.calls == []
    assert link.sync_state == SyncState.CREATE_READY
    assert link.create_payload["equipment_id"] == "12206"
    assert link.create_payload["booking_gstn"] != "09AAAAA0000A1Z5"  # stored masked
    assert not IstemApiAttempt.objects.exists()


def test_incomplete_mapping_blocks_with_field_list(shadow):
    booking = make_booking()
    map_equipment(booking.equipment)
    map_codes(booking.equipment)
    from .support import link_user

    link_user(booking.user)
    IstemCodeMapping.objects.filter(code_type=CodeType.EQUIPMENT_UOM).delete()
    link, job = _queue(booking)
    assert service.process_job(job.pk) == JobState.BLOCKED
    link.refresh_from_db()
    assert link.sync_state == SyncState.BLOCKED_CONFIG
    assert link.blocked_fields == ["equipment_uom"]
    assert link.needs_review


# --- live Create Booking -----------------------------------------------------------------------------------------

def test_create_booking_success_records_ids_attempt_and_events(live):
    booking = ready_booking()
    link, job = _queue(booking)
    transport = FakeTransport(token_response(), json_response(CREATED))
    assert service.process_job(job.pk, client_factory=factory(transport)) == JobState.DONE

    link.refresh_from_db()
    job.refresh_from_db()
    assert (link.sync_state, link.request_id, link.fbr_no) == (SyncState.CREATED, "55401", "1976125722")
    assert link.remote_status == RemoteStatus.PENDING
    assert not link.needs_review
    assert job.state == JobState.DONE and job.attempt_count == 1
    attempt = IstemApiAttempt.objects.get()
    assert attempt.outcome == AttemptOutcome.SUCCESS
    assert set(transport.calls[1]["data"]) == (
        set(contract.CREATE_BOOKING_MANDATORY_FIELDS) | set(contract.CREATE_BOOKING_DESCRIPTIVE_FIELDS)
    )
    states = list(IstemSyncEvent.objects.filter(booking_link=link).order_by("pk").values_list("to_state", flat=True))
    assert states[-2:] == [SyncState.CREATE_IN_FLIGHT, SyncState.CREATED]


def test_created_booking_is_never_sent_again(live):
    booking = ready_booking()
    link, job = _queue(booking)
    service.process_job(job.pk, client_factory=factory(FakeTransport(token_response(), json_response(CREATED))))
    enqueue_booking_sync(booking, trigger="again")
    assert service.enqueue_eligible() == 0
    assert IstemOutboxJob.objects.filter(state=JobState.PENDING).count() == 0


def test_success_without_fbr_no_is_created_and_flagged(live):
    link, job = _queue(ready_booking())
    body = {k: v for k, v in CREATED.items() if k != "fbr_no"}
    # Not in the Pending listing either.
    service.process_job(job.pk, client_factory=factory(FakeTransport(token_response(), json_response(body), listing_response())))
    link.refresh_from_db()
    assert link.sync_state == SyncState.CREATED
    assert link.fbr_no is None
    assert link.needs_review and "fbr_no" in link.review_reason


def test_success_without_fbr_no_takes_it_from_the_pending_listing(live):
    # [STAGING] Create Booking returns no fbr_no; Get All Bookings ("Pending") has it as fbc_no.
    link, job = _queue(ready_booking())
    body = {k: v for k, v in CREATED.items() if k != "fbr_no"}
    transport = FakeTransport(token_response(), json_response(body), listing_response(listing_row(55401)))
    service.process_job(job.pk, client_factory=factory(transport))
    link.refresh_from_db()
    assert (link.sync_state, link.fbr_no, link.needs_review) == (SyncState.CREATED, "1976055401", False)


@pytest.mark.parametrize("failure", [
    IstemError(ErrorCategory.READ_TIMEOUT, "timeout"),
    IstemError(ErrorCategory.CONNECTION_LOST, "reset"),
    json_response({}, status=502),
    json_response({"status": "Sucess", "message": "Done"}),
])
def test_ambiguous_create_is_uncertain_then_repeated_identically(live, failure):
    booking = ready_booking()
    link, job = _queue(booking)
    transport = FakeTransport(token_response(), failure)
    assert service.process_job(job.pk, client_factory=factory(transport)) == JobState.RETRY_WAIT

    link.refresh_from_db()
    job.refresh_from_db()
    assert link.sync_state == SyncState.CREATE_UNCERTAIN and link.request_id is None
    assert link.needs_review and "identical parameters" in link.review_reason
    assert IstemApiAttempt.objects.get().outcome == AttemptOutcome.UNCERTAIN
    # Other local signals never add a second create, and nothing is sent before the backoff expires.
    enqueue_booking_sync(booking, trigger="again")
    service.enqueue_eligible()
    assert service.process_due_jobs(client_factory=factory(transport)) == {}
    assert transport.paths().count(contract.CREATE_BOOKING_PATH) == 1
    assert IstemOutboxJob.objects.filter(operation=Operation.CREATE_BOOKING).count() == 1

    # When due, the same job repeats exactly the same request; I-STEM returns the existing booking (idempotent).
    IstemOutboxJob.objects.filter(pk=job.pk).update(next_attempt_at=job.created_at)
    existing = {**CREATED, "message": "An identical booking already exists for this request"}
    again = FakeTransport(json_response(existing))
    assert service.process_job(job.pk, client_factory=factory(again)) == JobState.DONE
    assert again.calls[0]["data"] == transport.calls[1]["data"]
    link.refresh_from_db()
    assert (link.sync_state, link.request_id, link.fbr_no) == (SyncState.CREATED, "55401", "1976125722")
    assert IstemApiAttempt.objects.filter(outcome=AttemptOutcome.SUCCESS).get().response_redacted["existing"] is True


def test_uncertain_create_is_not_repeated_after_the_booking_changed(live):
    booking = ready_booking()
    link, job = _queue(booking)
    service.process_job(job.pk, client_factory=factory(FakeTransport(token_response(), IstemError(ErrorCategory.READ_TIMEOUT, "t"))))
    booking.input_values = {"A": "5"}  # booking_no_sample changes: no longer the identical request
    booking.save(update_fields=["input_values"])
    IstemOutboxJob.objects.filter(pk=job.pk).update(next_attempt_at=job.created_at)
    transport = FakeTransport()
    assert service.process_job(job.pk, client_factory=factory(transport)) == JobState.BLOCKED
    assert transport.calls == []
    link.refresh_from_db()
    assert link.sync_state == SyncState.CREATE_UNCERTAIN and "adopt-ids" in link.review_reason


def test_not_sent_retry_of_an_uncertain_create_stays_uncertain(live):
    link, job = _queue(ready_booking())
    service.process_job(job.pk, client_factory=factory(FakeTransport(token_response(), IstemError(ErrorCategory.READ_TIMEOUT, "t"))))
    IstemOutboxJob.objects.filter(pk=job.pk).update(next_attempt_at=job.created_at)
    transport = FakeTransport(IstemError(ErrorCategory.CONNECT_FAILED, "refused"))
    assert service.process_job(job.pk, client_factory=factory(transport)) == JobState.RETRY_WAIT
    link.refresh_from_db()
    assert link.sync_state == SyncState.CREATE_UNCERTAIN  # never downgraded to CREATE_READY


@pytest.mark.parametrize("listed,institute,token_institute,needle", [
    (False, "213", "213", "not found in I-STEM's Equipment List"),
    (True, "999", "213", "verified for institute 999"),
    (True, "213", None, "no institute_id"),
])
def test_create_requires_equipment_listed_for_the_token_institute(live, listed, institute, token_institute, needle):
    booking = make_booking()
    map_equipment(booking.equipment, listed=listed, institute_id=institute)
    map_codes(booking.equipment)
    link_user(booking.user)
    link, job = _queue(booking)
    transport = FakeTransport(token_response(institute_id=token_institute))
    assert service.process_job(job.pk, client_factory=factory(transport)) == JobState.BLOCKED
    assert transport.paths() == [contract.AUTH_TOKEN_PATH]  # authentication only; no Create Booking
    link.refresh_from_db()
    assert link.sync_state == SyncState.BLOCKED_CONFIG and link.blocked_fields == ["equipment_id"]
    assert needle in link.review_reason


@pytest.mark.parametrize("failure", [
    (IstemError(ErrorCategory.CONNECT_FAILED, "refused"),),
    (IstemError(ErrorCategory.TLS, "tls"),),
    # A refused POST was not processed: repeated once with a fresh token; refused again, it waits.
    (json_response({}, status=401), token_response(), json_response({}, status=401)),
])
def test_unsent_or_unauthorised_create_waits_and_retries(live, failure):
    link, job = _queue(ready_booking())
    transport = FakeTransport(token_response(), *failure)
    assert service.process_job(job.pk, client_factory=factory(transport)) == JobState.RETRY_WAIT
    link.refresh_from_db()
    job.refresh_from_db()
    assert link.sync_state == SyncState.CREATE_READY
    assert job.next_attempt_at is not None


def test_retries_stop_at_the_attempt_limit(live):
    live.ISTEM_MAX_ATTEMPTS = 2
    link, job = _queue(ready_booking())
    for expected in (JobState.RETRY_WAIT, JobState.BLOCKED):
        IstemOutboxJob.objects.filter(pk=job.pk).update(next_attempt_at=job.created_at)  # due now
        transport = FakeTransport(token_response(), IstemError(ErrorCategory.CONNECT_FAILED, "refused"))
        assert service.process_job(job.pk, client_factory=factory(transport)) == expected
        clear_token_cache()


def test_definite_refusal_is_failed_for_review(live):
    link, job = _queue(ready_booking())
    body = {"status": False, "message": "This slot is already booked."}
    service.process_job(job.pk, client_factory=factory(FakeTransport(token_response(), json_response(body))))
    link.refresh_from_db()
    assert link.sync_state == SyncState.CREATE_FAILED
    assert "already booked" in link.review_reason
    assert IstemApiAttempt.objects.get().outcome == AttemptOutcome.DEFINITE_FAILURE


def test_live_mode_without_credentials_blocks_without_sending(live):
    live.ISTEM_PASSWORD = ""
    link, job = _queue(ready_booking())
    assert service.process_job(job.pk, client_factory=service.default_client) == JobState.BLOCKED
    job.refresh_from_db()
    assert job.last_error_category == ErrorCategory.CONFIG


def test_booking_no_longer_eligible_at_processing_time_is_cancelled(live):
    booking = ready_booking()
    link, job = _queue(booking)
    booking.status = BookingStatus.CANCELLED
    booking.save(update_fields=["status"])
    assert service.process_job(job.pk, client_factory=factory(FakeTransport())) == JobState.CANCELLED


def test_stored_records_hold_no_credentials_or_tokens(live):
    link, job = _queue(ready_booking())
    service.process_job(job.pk, client_factory=factory(FakeTransport(token_response(), json_response(CREATED))))
    dump = json.dumps([
        list(IstemApiAttempt.objects.values()), list(IstemBookingLink.objects.values()),
        list(IstemOutboxJob.objects.values()), list(IstemSyncEvent.objects.values()),
    ], default=str)
    assert FAKE_PASSWORD not in dump and FAKE_TOKEN not in dump


def test_link_update_failure_after_remote_success_is_recoverable(live, monkeypatch):
    booking = ready_booking()
    link, job = _queue(booking)

    def boom(*args, **kwargs):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(service, "_apply_create_success", boom)
    assert service.process_job(job.pk, client_factory=factory(FakeTransport(token_response(), json_response(CREATED)))) == JobState.BLOCKED
    monkeypatch.undo()
    link.refresh_from_db()
    assert link.sync_state == SyncState.CREATE_IN_FLIGHT
    assert IstemApiAttempt.objects.get().response_redacted["new_service_id"] == "55401"

    with pytest.raises(PermissionError):
        service.recover_create_from_attempt(link, actor=_admin())
    link = service.recover_create_from_attempt(link, actor=_admin("reconcile"))
    assert (link.sync_state, link.request_id, link.fbr_no) == (SyncState.CREATED, "55401", "1976125722")


def test_worker_lost_mid_request_becomes_uncertain(live):
    from datetime import timedelta

    from django.utils import timezone

    link, job = _queue(ready_booking())
    IstemBookingLink.objects.filter(pk=link.pk).update(sync_state=SyncState.CREATE_IN_FLIGHT)
    IstemOutboxJob.objects.filter(pk=job.pk).update(state=JobState.CLAIMED, lease_expires_at=timezone.now() - timedelta(minutes=1))
    IstemApiAttempt.objects.create(job=job, booking_link=link, operation=Operation.CREATE_BOOKING,
                                   started_at=timezone.now() - timedelta(minutes=30))
    assert service.sweep_stale_in_flight() == 1
    link.refresh_from_db()
    job.refresh_from_db()
    assert link.sync_state == SyncState.CREATE_UNCERTAIN
    assert job.state == JobState.RETRY_WAIT  # Create Booking is idempotent: the same job repeats it identically


def test_worker_lost_on_the_last_attempt_is_blocked(live):
    from datetime import timedelta

    from django.utils import timezone

    live.ISTEM_MAX_ATTEMPTS = 1
    link, job = _queue(ready_booking())
    IstemBookingLink.objects.filter(pk=link.pk).update(sync_state=SyncState.CREATE_IN_FLIGHT)
    IstemOutboxJob.objects.filter(pk=job.pk).update(state=JobState.CLAIMED, attempt_count=1,
                                                    lease_expires_at=timezone.now() - timedelta(minutes=1))
    IstemApiAttempt.objects.create(job=job, booking_link=link, operation=Operation.CREATE_BOOKING,
                                   started_at=timezone.now() - timedelta(minutes=30))
    service.sweep_stale_in_flight()
    job.refresh_from_db()
    link.refresh_from_db()
    assert job.state == JobState.BLOCKED and link.sync_state == SyncState.CREATE_UNCERTAIN
    assert "reconcile before any resubmission" in link.review_reason


def test_expired_claim_without_a_request_is_released(live):
    from datetime import timedelta

    from django.utils import timezone

    link, job = _queue(ready_booking())
    IstemOutboxJob.objects.filter(pk=job.pk).update(state=JobState.CLAIMED, lease_expires_at=timezone.now() - timedelta(minutes=1))
    service.sweep_stale_in_flight()
    job.refresh_from_db()
    assert job.state == JobState.PENDING


def test_fbr_execute_jobs_are_never_processed(live):
    link, _ = _queue(ready_booking())
    job = add_job(Operation.EXECUTE_FBR, dedupe_key="EXECUTE_FBR:x", trigger="test", booking_link=link)
    transport = FakeTransport()
    assert service.process_job(job.pk, client_factory=factory(transport)) == JobState.BLOCKED
    assert transport.calls == []


# --- uncertain-create recovery -----------------------------------------------------------------------------------

def _uncertain(live_settings):
    """An uncertain create whose automatic identical retries are used up (one attempt allowed)."""
    live_settings.ISTEM_MAX_ATTEMPTS = 1
    booking = ready_booking()
    link, job = _queue(booking)
    assert service.process_job(job.pk, client_factory=factory(FakeTransport(token_response(), IstemError(ErrorCategory.READ_TIMEOUT, "t")))) == JobState.BLOCKED
    link.refresh_from_db()
    assert link.sync_state == SyncState.CREATE_UNCERTAIN
    live_settings.ISTEM_MAX_ATTEMPTS = 5
    return link


def test_adopting_ids_after_portal_check(live):
    link = _uncertain(live)
    evidence = {"evidence_type": EvidenceType.ISTEM_PORTAL, "evidence_reference": "portal search 2026-10-24",
                "reason": "Booking found on the I-STEM portal under FBR 1976125722"}
    with pytest.raises(PermissionError):
        service.adopt_remote_ids(link, request_id="55401", fbr_no="1976125722", actor=_admin(), **evidence)
    with pytest.raises(ValueError):
        service.adopt_remote_ids(link, request_id="55401", fbr_no="1976125722", actor=_admin("link_manually"),
                                 evidence_type=EvidenceType.ISTEM_PORTAL, evidence_reference="", reason=evidence["reason"])
    link = service.adopt_remote_ids(link, request_id="55401", fbr_no="1976125722", actor=_admin("link_manually"), **evidence)
    assert (link.sync_state, link.request_id) == (SyncState.CREATED, "55401")
    assert IstemSyncEvent.objects.filter(booking_link=link, evidence_type=EvidenceType.ISTEM_PORTAL).exists()


def test_resubmission_needs_a_second_distinct_approver(live):
    link = _uncertain(live)
    actor = _admin("resolve_uncertain", "approve_resubmission")
    evidence = {"evidence_type": EvidenceType.ISTEM_TEAM, "evidence_reference": "email ref 42",
                "reason": "I-STEM team confirmed in writing that no booking exists"}
    with pytest.raises(PermissionError):
        service.mark_not_created(link, actor=actor, approver=actor, **evidence)
    with pytest.raises(PermissionError):
        service.mark_not_created(link, actor=actor, approver=_admin(), **evidence)
    job = service.mark_not_created(link, actor=actor, approver=_admin("approve_resubmission"), **evidence)
    link.refresh_from_db()
    assert link.sync_state == SyncState.CREATE_READY
    assert job.state == JobState.PENDING and job.operation == Operation.CREATE_BOOKING


# --- user mapping ------------------------------------------------------------------------------------------------

def _booking_with_unlinked_user():
    booking = make_booking()
    map_equipment(booking.equipment)
    map_codes(booking.equipment)
    return booking


def test_unlinked_user_parks_the_booking_and_queues_user_mapping(shadow):
    booking = _booking_with_unlinked_user()
    link, job = _queue(booking)
    assert service.process_job(job.pk) == JobState.CANCELLED
    link.refresh_from_db()
    assert link.sync_state == SyncState.USER_PENDING
    assert IstemOutboxJob.objects.filter(operation=Operation.ENSURE_USER, state=JobState.PENDING).exists()


def test_user_creation_disabled_by_default_blocks_without_request(live):
    booking = _booking_with_unlinked_user()
    link, job = _queue(booking)
    service.process_job(job.pk)
    user_job = IstemOutboxJob.objects.get(operation=Operation.ENSURE_USER)
    transport = FakeTransport()
    assert service.process_job(user_job.pk, client_factory=factory(transport)) == JobState.BLOCKED
    assert transport.calls == []
    assert "disabled" in IstemUserLink.objects.get(user=booking.user).review_reason


def _enable_user_creation(settings, user):
    settings.ISTEM_USER_CREATION_ENABLED = True  # institute_id comes from the token response, not settings
    IstemCodeMapping.objects.create(code_type=CodeType.PUBLIC_USER_TYPE, local_key=user.user_type, istem_value="Industry User")


def test_create_user_blocked_on_missing_local_profile_data(live):
    booking = _booking_with_unlinked_user()
    _enable_user_creation(live, booking.user)
    user_link = service.enqueue_user_link(booking.user, trigger="test")
    job = IstemOutboxJob.objects.get(operation=Operation.ENSURE_USER)
    transport = FakeTransport()
    assert service.process_job(job.pk, client_factory=factory(transport)) == JobState.BLOCKED
    assert transport.calls == []
    user_link.refresh_from_db()
    assert "user_organisation" in user_link.review_reason


@pytest.fixture
def complete_profile(monkeypatch):
    """Stand-in for the local fields that do not exist yet (organisation, salutation, address, …)."""
    from iic_booking.istem import mapping

    def collect(user):
        return {
            "user_first_name": "Asha", "user_last_name": "Verma", "user_email": user.email, "user_contactno": "9876543210",
            "user_organisation": "Asha Labs", "user_gender": "Female", "user_salutation": "Dr", "user_address": "12 Station Road",
            "user_city": "Roorkee", "user_district": "Haridwar", "user_state": "Uttarakhand", "user_country": "India",
            "user_pin": "247667", "billing_user_name": "Asha Labs", "billing_address": "12 Station Road, Roorkee",
        }

    monkeypatch.setattr(mapping, "collect_public_user_profile", collect)


def test_create_user_success_links_and_requeues_parked_booking(live, complete_profile, django_capture_on_commit_callbacks):
    booking = _booking_with_unlinked_user()
    _enable_user_creation(live, booking.user)
    link, job = _queue(booking)
    service.process_job(job.pk)
    user_job = IstemOutboxJob.objects.get(operation=Operation.ENSURE_USER)
    body = {"status": "SUCCESS", "message": "Public user created successfully.", "user_id": 35151}
    transport = FakeTransport(token_response(), json_response(body))
    with django_capture_on_commit_callbacks(execute=True):
        assert service.process_job(user_job.pk, client_factory=factory(transport)) == JobState.DONE
    assert transport.calls[1]["data"]["institute_id"] == FAKE_INSTITUTE_ID  # from the token response
    user_link = IstemUserLink.objects.get(user=booking.user)
    assert (user_link.state, user_link.public_user_id) == (UserLinkState.LINKED, "35151")
    link.refresh_from_db()
    assert link.sync_state == SyncState.CREATE_READY
    assert IstemOutboxJob.objects.filter(operation=Operation.CREATE_BOOKING, state=JobState.PENDING).exists()


def test_duplicate_user_response_needs_review_by_default(live, complete_profile):
    booking = _booking_with_unlinked_user()
    _enable_user_creation(live, booking.user)
    service.enqueue_user_link(booking.user, trigger="test")
    job = IstemOutboxJob.objects.get(operation=Operation.ENSURE_USER)
    body = {"status": False, "message": "User already exists with this email", "user_id": 777}
    service.process_job(job.pk, client_factory=factory(FakeTransport(token_response(), json_response(body))))
    user_link = IstemUserLink.objects.get(user=booking.user)
    assert user_link.state == UserLinkState.NEEDS_REVIEW
    assert user_link.public_user_id is None
    assert "777" in user_link.review_reason


def test_duplicate_user_response_maps_when_explicitly_accepted(live, complete_profile):
    live.ISTEM_ACCEPT_DUPLICATE_USER_ID = True
    booking = _booking_with_unlinked_user()
    _enable_user_creation(live, booking.user)
    service.enqueue_user_link(booking.user, trigger="test")
    job = IstemOutboxJob.objects.get(operation=Operation.ENSURE_USER)
    body = {"status": False, "message": "User already exists with this email", "user_id": 777}
    service.process_job(job.pk, client_factory=factory(FakeTransport(token_response(), json_response(body))))
    assert IstemUserLink.objects.get(user=booking.user).public_user_id == "777"


def test_manual_user_link_requires_permission_and_evidence(shadow):
    booking = _booking_with_unlinked_user()
    evidence = {"evidence_type": EvidenceType.ISTEM_PORTAL, "evidence_reference": "user search",
                "reason": "Existing I-STEM account found for this email address"}
    with pytest.raises(PermissionError):
        service.link_user_manually(booking.user, public_user_id="35151", actor=_admin(), **evidence)
    with pytest.raises(ValueError):
        service.link_user_manually(booking.user, public_user_id="abc", actor=_admin("link_manually"), **evidence)
    user_link = service.link_user_manually(booking.user, public_user_id="35151", actor=_admin("link_manually"), **evidence)
    assert user_link.state == UserLinkState.LINKED


# --- confirmation ------------------------------------------------------------------------------------------------

def _created(live_settings):
    link, job = _queue(ready_booking())
    service.process_job(job.pk, client_factory=factory(FakeTransport(token_response(), json_response(CREATED))))
    link.refresh_from_db()
    return link


def test_confirmation_is_disabled_by_default(live):
    link = _created(live)
    with pytest.raises(IstemConfigError):
        service.request_confirmation(link, actor=_admin(superuser=True), reason="test")


def test_confirmation_success_without_confirmation_id_is_uncertain_then_verified(live):
    live.ISTEM_CONFIRM_ENABLED = True
    live.ISTEM_CONFIRM_APPROVED_BY = "OP_IITR"
    link = _created(live)
    with pytest.raises(PermissionError):
        service.request_confirmation(link, actor=_admin(), reason="slot allocated")
    job = service.request_confirmation(link, actor=_admin("request_confirm"), reason="slot allocated")
    body = {"status": "SUCCESS", "message": "Booking confirmed", "result": {"request_id": "55401"}}
    # token cached from Create Booking; the Slot Allocated listing does not have it yet
    transport = FakeTransport(json_response(body), listing_response())
    # [SPEC] a successful Confirm Booking carries confirmation_id; without it (and without a listing) it is uncertain.
    assert service.process_job(job.pk, client_factory=factory(transport)) == JobState.RETRY_WAIT
    assert transport.paths() == [contract.CONFIRM_BOOKING_PATH, contract.GET_ALL_BOOKINGS_PATH]
    sent = transport.calls[0]["data"]
    assert sent["request_id"] == "55401" and sent["approved_by"] == "OP_IITR" and sent["booking_status"] == "Slot Allocated"
    link.refresh_from_db()
    assert link.sync_state == SyncState.CONFIRM_UNCERTAIN

    rows = {"status": "SUCCESS", "result": [{"request_id": "55401", "confirmation_id": "44294"}, {"request_id": "1", "confirmation_id": "2"}]}
    reader = FakeTransport(json_response(rows))  # token still cached
    assert service.verify_confirmation(link, client_factory=factory(reader)) == "44294"
    assert reader.calls[0]["params"] == {"fbr_no": "1976125722", "status_code": "Slot Allocated"}
    link.refresh_from_db()
    assert (link.sync_state, link.confirmation_id) == (SyncState.CONFIRMED, "44294")


def test_ambiguous_confirmation_is_uncertain(live):
    live.ISTEM_CONFIRM_ENABLED = True
    live.ISTEM_CONFIRM_APPROVED_BY = "OP_IITR"
    link = _created(live)
    job = service.request_confirmation(link, actor=_admin("request_confirm"), reason="slot allocated")
    service.process_job(job.pk, client_factory=factory(FakeTransport(IstemError(ErrorCategory.READ_TIMEOUT, "t"))))
    link.refresh_from_db()
    assert link.sync_state == SyncState.CONFIRM_UNCERTAIN


def test_refresh_remote_status_reads_get_fbr_details(live):
    link = _created(live)
    body = {"status": "SUCCESS", "result": [{"fbr_no": "1976125722", "status_code": "Slot Allocated"}]}
    assert service.refresh_remote_status(link, client_factory=factory(FakeTransport(json_response(body)))) == "Slot Allocated"
    link.refresh_from_db()
    assert link.remote_status == RemoteStatus.SLOT_ALLOCATED
    assert link.sync_state == SyncState.CREATED  # read-only: no state transition


def test_stage2_never_calls_fbr_execute(live):
    live.ISTEM_CONFIRM_ENABLED = True
    live.ISTEM_CONFIRM_APPROVED_BY = "OP_IITR"
    link = _created(live)
    booking = link.booking
    booking.status = BookingStatus.COMPLETED
    booking.save(update_fields=["status"])
    service.enqueue_eligible()
    transport = FakeTransport()
    service.process_due_jobs(client_factory=factory(transport))
    assert all("fbr-execute" not in c["url"] for c in transport.calls)
    assert not IstemOutboxJob.objects.filter(operation=Operation.EXECUTE_FBR).exists()


# --- equipment verification (read-only, institute from the token) ------------------------------------------------

def _equipment_page(*ids):
    return json_response({"status": "SUCCESS", "message": "Equipment Found", "totalrecord": len(ids), "records": len(ids),
                          "result": [{"equipment_id": i, "equipment_name": f"Instrument {i}"} for i in ids]})


def test_verify_equipment_records_found_and_missing_for_the_token_institute(live):
    found_booking, missing_booking = make_booking(), make_booking()
    found = map_equipment(found_booking.equipment, istem_id="29907", listed=False)
    missing = map_equipment(missing_booking.equipment, istem_id="40404", listed=False)
    transport = FakeTransport(token_response(institute_id="213"), _equipment_page(29907, 45671))
    summary = service.verify_equipment_mappings(client_factory=factory(transport))
    assert summary["institute_id"] == "213" and summary["listed"] == 2
    assert summary["found"] == [found_booking.equipment.code] and summary["missing"] == [missing_booking.equipment.code]
    assert transport.calls[1]["method"] == "GET" and transport.calls[1]["params"]["institute_id"] == "213"
    assert [c["method"] for c in transport.calls] == ["POST", "GET"]  # token, then the read-only list; nothing else
    found.refresh_from_db()
    missing.refresh_from_db()
    assert (found.remote_found, found.remote_equipment_name, found.remote_institute_id) == (True, "Instrument 29907", "213")
    assert (missing.remote_found, missing.remote_institute_id) == (False, "213")
    attempt = IstemApiAttempt.objects.get(operation=Operation.LIST_EQUIPMENT)
    assert attempt.outcome == AttemptOutcome.SUCCESS and attempt.booking_link is None


def test_verify_equipment_records_nothing_when_the_list_cannot_be_read(live):
    booking = make_booking()
    mapping = map_equipment(booking.equipment, listed=False)
    with pytest.raises(IstemError):
        service.verify_equipment_mappings(client_factory=factory(FakeTransport(token_response(), IstemError(ErrorCategory.READ_TIMEOUT, "t"))))
    mapping.refresh_from_db()
    assert (mapping.remote_checked_at, mapping.remote_found) == (None, None)


def test_verify_equipment_refuses_a_token_without_institute(live):
    map_equipment(make_booking().equipment, listed=False)
    with pytest.raises(IstemError, match="institute_id"):
        service.verify_equipment_mappings(client_factory=factory(FakeTransport(token_response(institute_id=None))))


def test_verified_equipment_lets_create_booking_proceed(live):
    booking = make_booking()
    map_equipment(booking.equipment, istem_id="29907", listed=False)
    map_codes(booking.equipment)
    link_user(booking.user)
    service.verify_equipment_mappings(client_factory=factory(FakeTransport(token_response(), _equipment_page(29907))))
    link, job = _queue(booking)
    transport = FakeTransport(json_response(CREATED))  # token cached from the verification
    assert service.process_job(job.pk, client_factory=factory(transport)) == JobState.DONE
    assert transport.calls[0]["data"]["equipment_id"] == "29907"


def test_verify_equipment_command_requires_reconcile_permission(live, monkeypatch, capsys):
    booking = make_booking()
    map_equipment(booking.equipment, istem_id="29907", listed=False)
    transport = FakeTransport(token_response(), _equipment_page(29907))
    monkeypatch.setattr(service, "IstemClient", lambda: IstemClient(transport))  # used by service.default_client
    with pytest.raises(CommandError, match="istem.reconcile"):
        call_command("istem_recover", "verify-equipment", "--actor", _admin().email)
    call_command("istem_recover", "verify-equipment", "--actor", _admin("reconcile").email)
    out = capsys.readouterr().out
    assert "institute_id=213" in out and booking.equipment.code in out


# --- user link verification (read-only Public User Lookup) -------------------------------------------------------

def _user_found(user_id):
    return json_response({"status": "SUCCESS", "message": "User found", "result": {"user_id": user_id, "user_name": "PU_X"}})


def _lookup_user(phone="9876543210"):
    user = UserFactory(name="Asha Verma", phone_number=phone)
    link_user(user, public_user_id="22798")
    return user


def test_verify_user_confirms_a_link_found_by_email_and_mobile(live):
    user = _lookup_user()
    transport = FakeTransport(token_response(), _user_found(22798), _user_found(22798))
    summary = service.verify_user_link(user, client_factory=factory(transport))
    assert summary == {"public_user_id": "22798", "by_email": ["22798"], "by_mobile": ["22798"], "matches": True}
    assert [c["method"] for c in transport.calls] == ["POST", "GET", "GET"]  # token, then two read-only lookups
    assert transport.calls[1]["params"] == {"user_email": user.email}
    assert transport.calls[2]["params"] == {"user_contactno": "9876543210"}
    assert IstemUserLink.objects.get(user=user).last_verified_at is not None
    attempt = IstemApiAttempt.objects.get(operation=Operation.LOOKUP_USER)
    assert attempt.outcome == AttemptOutcome.SUCCESS
    assert user.email not in json.dumps(attempt.request_redacted)  # personal data is redacted in the ledger


@pytest.mark.parametrize("by_email,by_mobile", [
    (_user_found(40001), _user_found(22798)),
    (_user_found(22798), json_response({"status": "ERROR", "message": "No matching user found"})),
])
def test_verify_user_reports_a_mismatch_without_changing_the_link(live, by_email, by_mobile):
    user = _lookup_user()
    summary = service.verify_user_link(user, client_factory=factory(FakeTransport(token_response(), by_email, by_mobile)))
    assert summary["matches"] is False
    link = IstemUserLink.objects.get(user=user)
    assert (link.public_user_id, link.state, link.last_verified_at) == ("22798", UserLinkState.LINKED, None)


def test_verify_user_checks_email_only_when_there_is_no_usable_mobile(live):
    user = _lookup_user(phone="")
    transport = FakeTransport(token_response(), _user_found(22798))
    assert service.verify_user_link(user, client_factory=factory(transport))["by_mobile"] is None
    assert len(transport.calls) == 2


def test_verify_user_records_a_failed_lookup_and_leaves_the_link_alone(live):
    user = _lookup_user()
    with pytest.raises(IstemError):
        service.verify_user_link(user, client_factory=factory(FakeTransport(token_response(), IstemError(ErrorCategory.READ_TIMEOUT, "t"))))
    assert IstemUserLink.objects.get(user=user).last_verified_at is None
    assert IstemApiAttempt.objects.get(operation=Operation.LOOKUP_USER).outcome != AttemptOutcome.SUCCESS


def test_verify_user_needs_a_linked_id(live):
    with pytest.raises(ValueError, match="no linked"):
        service.verify_user_link(UserFactory(), client_factory=factory(FakeTransport()))


def test_verify_user_command_requires_reconcile_permission(live, monkeypatch, capsys):
    user = _lookup_user()
    transport = FakeTransport(token_response(), _user_found(22798), _user_found(22798))
    monkeypatch.setattr(service, "IstemClient", lambda: IstemClient(transport))
    with pytest.raises(CommandError, match="istem.reconcile"):
        call_command("istem_recover", "verify-user", "--actor", _admin().email, "--user", user.email)
    call_command("istem_recover", "verify-user", "--actor", _admin("reconcile").email, "--user", user.email)
    out = capsys.readouterr().out
    assert "public_user_id=22798" in out and "matches=yes" in out and user.email not in out

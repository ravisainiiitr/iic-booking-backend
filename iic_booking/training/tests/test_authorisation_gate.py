"""Authorisation to book: off by default, server gate at every booking entry point, request flow, operator tools,
grandfathering and expiry reminders."""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from dateutil.relativedelta import relativedelta
from django.utils import timezone

from iic_booking.equipment.models import Booking, BookingStatus, DailySlot
from iic_booking.training import authorisation
from iic_booking.training.models import (
    AuthorisationRequest,
    AwardStatus,
    CertificationAward,
    CertificationLevel,
    EquipmentAuthorisationSetting,
    TrainingAudience,
    TrainingAuditLog,
    TrainingEquipmentSetting,
    TrainingModuleSettings,
)
from iic_booking.users.models.user_type import UserType

from .conftest import API, client_for, internal_rate_profile, make_slots, make_user

pytestmark = pytest.mark.django_db
GATE = authorisation.GATE_CODE
AUTH = f"{API}/authorisation"


@pytest.fixture(autouse=True)
def _no_portal_lock(monkeypatch):
    from iic_booking.users.legacy_ledger import booking_lock

    monkeypatch.setattr(booking_lock, "booking_is_locked", lambda user: (False, ""))
    monkeypatch.setattr(booking_lock, "department_equipment_booking_blocked", lambda equipment, user: (False, ""))


def gate_on(eq, **kwargs):
    return EquipmentAuthorisationSetting.objects.create(equipment=eq, required=True, **kwargs)


def authorise(user, eq, *, months=12, status=AwardStatus.ACTIVE, valid_until=None):
    now = timezone.now()
    return CertificationAward.objects.create(
        user=user,
        equipment=eq,
        level=CertificationLevel.objects.get(code="TRAINED"),
        status=status,
        awarded_at=now - timedelta(days=30),
        valid_until=valid_until if valid_until is not None else now + relativedelta(months=months),
    )


def make_booking(user, eq, *, status=BookingStatus.BOOKED, days_ago=None, **kwargs):
    from iic_booking.equipment.models import ChargeProfile

    profile = ChargeProfile.objects.filter(equipment=eq, user_type=user.user_type).first()
    profile = profile or internal_rate_profile(eq, user_type=user.user_type)
    b = Booking.objects.create(
        user=user, equipment=eq, charge_profile=profile, status=status, total_charge=Decimal("0"),
        total_time_minutes=60, input_values={}, **kwargs,
    )
    if days_ago is not None:
        Booking.objects.filter(pk=b.pk).update(completed_at=timezone.now() - timedelta(days=days_ago))
    return b


def book(actor, eq, **body):
    return client_for(actor).post(f"/api/equipments/{eq.pk}/book/", {"slot_ids": [], **body}, format="json")


def code(resp):
    data = getattr(resp, "data", None) or {}
    return data.get("code") if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Off by default and scope
# ---------------------------------------------------------------------------
def test_off_by_default_nothing_changes(world):
    assert authorisation.evaluate(world.student, world.equipment)["status"] == "NOT_REQUIRED"
    assert code(book(world.student, world.equipment)) != GATE
    EquipmentAuthorisationSetting.objects.create(equipment=world.equipment)  # row exists but required is off
    assert code(book(world.student, world.equipment)) != GATE
    assert authorisation.gated_equipment_ids() == set()


def test_gate_blocks_unauthorised_student_and_reports_status(world):
    gate_on(world.equipment)
    resp = book(world.student, world.equipment)
    assert resp.status_code == 403 and code(resp) == GATE
    st = resp.data["authorisation"]
    assert st["status"] == "NOT_AUTHORISED" and st["can_request"] is True and st["allowed"] is False
    assert "Request training" in resp.data["error"]


def test_authorised_user_passes_and_expired_or_revoked_do_not(world):
    gate_on(world.equipment)
    award = authorise(world.student, world.equipment)
    assert code(book(world.student, world.equipment)) != GATE
    assert authorisation.evaluate(world.student, world.equipment)["status"] == "AUTHORISED"

    CertificationAward.objects.filter(pk=award.pk).update(valid_until=timezone.now() - timedelta(days=1))
    st = authorisation.evaluate(world.student, world.equipment)
    assert st["status"] == "EXPIRED" and not st["allowed"] and st["can_request"]
    assert code(book(world.student, world.equipment)) == GATE

    CertificationAward.objects.filter(pk=award.pk).update(status=AwardStatus.REVOKED, valid_until=timezone.now() + timedelta(days=90))
    assert authorisation.evaluate(world.student, world.equipment)["status"] == "REVOKED"
    assert code(book(world.student, world.equipment)) == GATE


def test_expiring_soon_is_still_allowed(world):
    gate_on(world.equipment)
    authorise(world.student, world.equipment, valid_until=timezone.now() + timedelta(days=10))
    st = authorisation.evaluate(world.student, world.equipment)
    assert st["status"] == "EXPIRING" and st["allowed"] and st["can_request"]


def test_module_off_or_equipment_out_of_scope_disables_gate(world, settings):
    gate_on(world.equipment)
    settings.TRAINING_MODULE_ENABLED = False
    TrainingModuleSettings.objects.filter(pk=1).update(module_enabled=False)
    assert code(book(world.student, world.equipment)) != GATE
    assert authorisation.gated_equipment_ids() == set()

    TrainingModuleSettings.objects.filter(pk=1).update(module_enabled=True)
    TrainingEquipmentSetting.objects.create(equipment=world.other_equipment, enabled=True)
    # Only the other equipment is in Training scope now.
    assert code(book(world.student, world.equipment)) != GATE
    TrainingEquipmentSetting.objects.create(equipment=world.equipment, enabled=True)
    assert code(book(world.student, world.equipment)) == GATE


def test_test_accounts_audience_only_gates_test_accounts(world):
    gate_on(world.equipment)
    TrainingModuleSettings.objects.filter(pk=1).update(audience=TrainingAudience.TEST_ACCOUNTS)
    tester = make_user(user_type=UserType.STUDENT, department=world.dept, is_test_account=True)
    assert code(book(world.student, world.equipment)) != GATE
    assert code(book(tester, world.equipment)) == GATE
    # Badges only for people the gate applies to (and staff).
    assert client_for(world.student).get(f"{AUTH}/gated-equipment/").data["equipment_ids"] == []
    assert client_for(tester).get(f"{AUTH}/gated-equipment/").data["equipment_ids"] == [world.equipment.pk]
    assert client_for(world.oic).get(f"{AUTH}/gated-equipment/").data["equipment_ids"] == [world.equipment.pk]


def test_applies_to_internal_by_default_and_all_when_chosen(world):
    row = gate_on(world.equipment)
    external = make_user(user_type=UserType.EXTERNAL)
    assert authorisation.evaluate(external, world.equipment)["status"] == "NOT_REQUIRED"
    row.applies_to = "ALL"
    row.save()
    assert authorisation.evaluate(external, world.equipment)["status"] == "NOT_AUTHORISED"
    # Staff booking for themselves are never gated.
    assert authorisation.evaluate(world.operator, world.equipment)["status"] == "NOT_REQUIRED"


def test_staff_booking_on_behalf_bypasses_with_audit_note(world):
    gate_on(world.equipment)
    resp = book(world.oic, world.equipment, user_id=world.student.id)
    assert code(resp) != GATE
    log = TrainingAuditLog.objects.get(action="gate.bypassed")
    assert log.actor == world.oic and log.after["user"] == world.student.id and log.after["context"] == "booking"


def test_grace_for_past_completed_bookings(world):
    gate_on(world.equipment, grace_completed_bookings=2, grace_months=12)
    make_booking(world.student, world.equipment, status=BookingStatus.COMPLETED, days_ago=40)
    assert authorisation.evaluate(world.student, world.equipment)["status"] == "NOT_AUTHORISED"
    make_booking(world.student, world.equipment, status=BookingStatus.COMPLETED, days_ago=20)
    st = authorisation.evaluate(world.student, world.equipment)
    assert st["status"] == "GRACE" and st["allowed"]
    assert code(book(world.student, world.equipment)) != GATE


# ---------------------------------------------------------------------------
# Other entry points
# ---------------------------------------------------------------------------
def test_booking_assistant_confirm_and_preflight(world):
    from iic_booking.research_copilot.services.assistant.preflight import account_problems
    from iic_booking.research_copilot.services.v2.mutations import domain_bridge

    gate_on(world.equipment)
    status, data = domain_bridge.call_book_equipment(user=world.student, equipment_id=world.equipment.pk, body={"slot_ids": []})
    assert status == 403 and data["code"] == GATE
    assert any("Authorisation required" in p for p in account_problems(world.student, world.equipment))
    authorise(world.student, world.equipment)
    assert not any("Authorisation" in p for p in account_problems(world.student, world.equipment))


def test_waitlist_join_and_auto_booking(world):
    from iic_booking.equipment.waitlist import add_user_to_waitlist
    from iic_booking.equipment.waitlist_booking import create_booking_for_waitlist_user

    world.equipment.waitlist_queue_depth = 5
    world.equipment.save()
    gate_on(world.equipment)
    assert add_user_to_waitlist(world.equipment, world.student) == (False, None)
    slot = make_slots(world.equipment, hours=[10])[0]
    booking, error = create_booking_for_waitlist_user(world.equipment, world.student, [slot.id])
    assert booking is None and "Authorisation required" in error

    # OIC manual confirmation from the waitlist bypasses (with an audit note).
    with mock.patch("iic_booking.training.authorisation.audit") as audit_mock:
        _, error = create_booking_for_waitlist_user(world.equipment, world.student, [slot.id], created_by=world.oic, staff_override=True)
    assert "Authorisation required" not in (error or "")
    assert audit_mock.call_args.args[1] == "gate.bypassed"

    authorise(world.student, world.equipment)
    added, position = add_user_to_waitlist(world.equipment, world.student)
    assert added is True and position == 1


def test_urgent_request_is_gated_and_allocation_is_staff_bypass(world):
    gate_on(world.equipment)
    resp = client_for(world.student).post(
        "/api/urgent-booking-requests/create/",
        {"equipment_id": world.equipment.pk, "request_type": "REVIEWER_URGENT", "disclaimer_accepted": True,
         "reviewer_comment": "Reviewer asked for an urgent repeat measurement."},
        format="json",
    )
    assert resp.status_code == 403 and code(resp) == GATE
    assert authorisation.check_booking(world.student, world.equipment, actor=world.oic, context="urgent allocation") is None
    assert TrainingAuditLog.objects.filter(action="gate.bypassed", after__context="urgent allocation").exists()


def test_templates_are_flagged(world):
    from iic_booking.equipment.booking_templates import template_booking_block

    gate_on(world.equipment)
    assert "Authorisation required" in (template_booking_block(world.student, world.equipment) or "")
    authorise(world.student, world.equipment)
    assert template_booking_block(world.student, world.equipment) is None


def _booking_with_slots(user, eq):
    b = make_booking(user, eq)
    slots = make_slots(eq, day=timezone.localdate() + timedelta(days=20), hours=[9, 10, 11])
    DailySlot.objects.filter(pk=slots[0].pk).update(booking=b, status="BOOKED")
    return b, slots


def test_user_reschedule_is_gated_and_staff_reschedule_bypasses(world):
    gate_on(world.equipment)
    booking, slots = _booking_with_slots(world.student, world.equipment)
    body = {"start_time": slots[1].start_datetime.isoformat(), "end_time": slots[1].end_datetime.isoformat()}
    resp = client_for(world.student).post(f"/api/bookings/{booking.pk}/user-reschedule/", body, format="json")
    assert resp.status_code == 403 and code(resp) == GATE

    resp = client_for(world.admin).post(f"/api/bookings/{booking.pk}/reschedule/", body, format="json")
    assert code(resp) != GATE, resp.data
    assert resp.status_code == 200, resp.data
    assert TrainingAuditLog.objects.filter(action="gate.bypassed", after__context="staff reschedule").exists()


def test_cross_equipment_target_is_checked(world):
    from iic_booking.equipment.equipment_group_service import equipment_eligibility_error

    gate_on(world.equipment)
    assert "Authorisation required" in equipment_eligibility_error(world.student, world.equipment) or ""
    # Staff listing options for someone else is not blocked (and not audited until a booking actually moves).
    assert "Authorisation" not in (equipment_eligibility_error(world.student, world.equipment, actor=world.oic) or "")
    assert not TrainingAuditLog.objects.filter(action="gate.bypassed").exists()


def test_user_repeat_booking_is_gated(world):
    gate_on(world.equipment)
    booking = make_booking(world.student, world.equipment, status=BookingStatus.COMPLETED, repeat_sample_enabled=True)
    resp = client_for(world.student).post(f"/api/bookings/{booking.pk}/create-repeat-booking/", {}, format="json")
    assert resp.status_code == 403 and code(resp) == GATE


# ---------------------------------------------------------------------------
# Request flow
# ---------------------------------------------------------------------------
def test_request_schedule_approve_then_book(world):
    gate_on(world.equipment, validity_months=6)
    with mock.patch("iic_booking.training.authorisation.notify.send") as send:
        resp = client_for(world.student).post(f"{AUTH}/requests/", {"equipment_id": world.equipment.pk, "message": "Thesis XRD runs"}, format="json")
    assert resp.status_code == 201, resp.data
    recipients = send.call_args.args[1]
    assert world.oic in recipients and world.operator in recipients and world.temp_oic in recipients
    assert send.call_args.args[0] == "training_authorisation_request_email"
    req_id = resp.data["id"]

    dup = client_for(world.student).post(f"{AUTH}/requests/", {"equipment_id": world.equipment.pk}, format="json")
    assert dup.status_code == 409

    inbox = client_for(world.operator).get(f"{AUTH}/requests/", {"scope": "inbox", "status": "open"})
    assert [r["id"] for r in inbox.data["results"]] == [req_id] and inbox.data["results"][0]["can_handle"]
    assert client_for(world.other_oic).post(f"{AUTH}/requests/{req_id}/approve/", {}, format="json").status_code == 403

    start = (timezone.now() + timedelta(days=2)).replace(microsecond=0)
    resp = client_for(world.operator).post(f"{AUTH}/requests/{req_id}/schedule/", {"start_at": start.isoformat(), "location": "Lab 2"}, format="json")
    assert resp.status_code == 200 and resp.data["status"] == "SCHEDULED"
    assert authorisation.evaluate(world.student, world.equipment)["open_request"]["status"] == "SCHEDULED"

    resp = client_for(world.operator).post(f"{AUTH}/requests/{req_id}/approve/", {"note": "Hands-on done"}, format="json")
    assert resp.status_code == 200 and resp.data["status"] == "APPROVED"
    award = CertificationAward.objects.get(pk=resp.data["award_id"])
    assert award.level.code == "TRAINED" and 5 <= (award.valid_until - timezone.now()).days // 30 <= 6
    assert authorisation.evaluate(world.student, world.equipment)["status"] == "AUTHORISED"
    assert code(book(world.student, world.equipment)) != GATE


def test_reject_needs_reason_and_withdraw(world):
    gate_on(world.equipment)
    req = authorisation.create_request(world.student, world.equipment, "please")
    assert client_for(world.oic).post(f"{AUTH}/requests/{req.pk}/reject/", {}, format="json").status_code == 400
    resp = client_for(world.oic).post(f"{AUTH}/requests/{req.pk}/reject/", {"reason": "Do the safety induction first"}, format="json")
    assert resp.data["status"] == "REJECTED" and resp.data["decision_reason"]
    req2 = authorisation.create_request(world.student, world.equipment)
    resp = client_for(world.student).post(f"{AUTH}/requests/{req2.pk}/withdraw/", {}, format="json")
    assert resp.data["status"] == "WITHDRAWN"


def test_request_refused_when_not_required_or_already_authorised(world):
    resp = client_for(world.student).post(f"{AUTH}/requests/", {"equipment_id": world.equipment.pk}, format="json")
    assert resp.status_code == 400 and resp.data["code"] == "not_required"
    gate_on(world.equipment)
    authorise(world.student, world.equipment)
    resp = client_for(world.student).post(f"{AUTH}/requests/", {"equipment_id": world.equipment.pk}, format="json")
    assert resp.data["code"] == "already_authorised"


# ---------------------------------------------------------------------------
# OIC / Lab Operator tools
# ---------------------------------------------------------------------------
def test_setting_only_oic_or_admin_and_audited(world):
    url = f"{AUTH}/equipment/{world.equipment.pk}/"
    assert client_for(world.student).put(url, {"required": True}, format="json").status_code == 403
    assert client_for(world.operator).put(url, {"required": True}, format="json").status_code == 403
    resp = client_for(world.oic).put(url, {"required": True, "validity_months": 12, "applies_to": "ALL"}, format="json")
    assert resp.status_code == 200 and resp.data["required"] and resp.data["live"] and resp.data["applies_to"] == "ALL"
    assert TrainingAuditLog.objects.filter(action="authorisation.setting_updated").count() == 1
    assert client_for(world.student).get(f"{AUTH}/gated-equipment/").data["equipment_ids"] == [world.equipment.pk]
    bad = client_for(world.oic).put(url, {"validity_months": 0}, format="json")
    assert bad.status_code == 400


def test_bulk_grant_revoke_renew_and_csv(world):
    gate_on(world.equipment, validity_months=12)
    url = f"{AUTH}/equipment/{world.equipment.pk}/users/"
    assert client_for(world.operator).post(url, {"user_ids": [world.student.id]}, format="json").status_code == 400  # reason
    resp = client_for(world.operator).post(url, {"user_ids": [world.student.id, world.outsider.id, world.oic.id], "reason": "Batch trained 12 Oct"}, format="json")
    assert resp.status_code == 201 and resp.data["granted"] == 2 and len(resp.data["skipped"]) == 1
    rows = client_for(world.oic).get(url).data["results"]
    assert {r["user"]["id"] for r in rows} == {world.student.id, world.outsider.id}
    award_id = next(r["id"] for r in rows if r["user"]["id"] == world.student.id)

    assert client_for(world.other_oic).post(f"{AUTH}/awards/{award_id}/revoke/", {"reason": "x"}, format="json").status_code == 403
    resp = client_for(world.oic).post(f"{AUTH}/awards/{award_id}/revoke/", {"reason": "Safety breach"}, format="json")
    assert resp.data["status"] == "REVOKED"
    assert code(book(world.student, world.equipment)) == GATE
    assert client_for(world.oic).post(f"{AUTH}/awards/{award_id}/renew/", {"reason": "x"}, format="json").status_code == 400

    other = CertificationAward.objects.get(user=world.outsider, equipment=world.equipment)
    before = other.valid_until
    resp = client_for(world.operator).post(f"{AUTH}/awards/{other.pk}/renew/", {"reason": "Refresher", "months": 6}, format="json")
    other.refresh_from_db()
    assert resp.status_code == 200 and other.valid_until > before
    assert TrainingAuditLog.objects.filter(action__in=("authorisation.revoke", "authorisation.renew")).count() == 2

    csv_resp = client_for(world.operator).get(f"{AUTH}/equipment/{world.equipment.pk}/users/export/")
    body = csv_resp.content.decode()
    assert csv_resp.status_code == 200 and body.startswith("User ID,Name,Email") and "Revoked" in body
    assert client_for(world.student).get(f"{AUTH}/equipment/{world.equipment.pk}/users/export/").status_code == 403


def test_grandfather_preview_and_apply(world):
    gate_on(world.equipment)
    for days in (30, 60):
        make_booking(world.student, world.equipment, status=BookingStatus.COMPLETED, days_ago=days)
    make_booking(world.outsider, world.equipment, status=BookingStatus.COMPLETED, days_ago=10)
    make_booking(world.faculty, world.equipment, status=BookingStatus.COMPLETED, days_ago=500)
    make_booking(world.faculty, world.equipment, status=BookingStatus.COMPLETED, days_ago=600)
    url = f"{AUTH}/equipment/{world.equipment.pk}/grandfather/"

    preview = client_for(world.operator).get(url, {"min_bookings": 2, "months": 12})
    assert preview.status_code == 200 and preview.data["dry_run"] and preview.data["would_authorise"] == 1
    assert [c["user"]["id"] for c in preview.data["candidates"]] == [world.student.id]
    assert not CertificationAward.objects.exists()

    assert client_for(world.operator).post(url, {"min_bookings": 2, "months": 12, "confirm": "GRANDFATHER"}, format="json").status_code == 403
    assert client_for(world.oic).post(url, {"min_bookings": 2, "months": 12}, format="json").data["code"] == "confirm_required"
    resp = client_for(world.oic).post(url, {"min_bookings": 2, "months": 12, "confirm": "GRANDFATHER"}, format="json")
    assert resp.status_code == 200 and resp.data["granted"] == 1
    assert authorisation.evaluate(world.student, world.equipment)["status"] == "AUTHORISED"
    assert authorisation.evaluate(world.outsider, world.equipment)["status"] == "NOT_AUTHORISED"
    assert TrainingAuditLog.objects.filter(action="authorisation.grandfathered").exists()
    again = client_for(world.oic).get(url, {"min_bookings": 2, "months": 12})
    assert again.data["would_authorise"] == 0 and again.data["already_authorised"] == 1


def test_expiry_reminders_30_and_7_days_once_each(world):
    gate_on(world.equipment)
    award = authorise(world.student, world.equipment, valid_until=timezone.now() + timedelta(days=25))
    with mock.patch("iic_booking.training.authorisation.notify.send") as send:
        assert authorisation.send_expiry_reminders()["reminded"] == 1
        assert authorisation.send_expiry_reminders()["reminded"] == 0
        CertificationAward.objects.filter(pk=award.pk).update(valid_until=timezone.now() + timedelta(days=5))
        assert authorisation.send_expiry_reminders()["reminded"] == 1
        assert authorisation.send_expiry_reminders()["reminded"] == 0
    assert send.call_count == 2 and send.call_args.args[0] == "certification_expiring_email"
    # Not gated: no reminders from this step.
    EquipmentAuthorisationSetting.objects.filter(equipment=world.equipment).update(required=False)
    assert authorisation.send_expiry_reminders()["reminded"] == 0


def test_auto_expiry_by_housekeeping(world):
    from iic_booking.training import certification

    gate_on(world.equipment)
    award = authorise(world.student, world.equipment, valid_until=timezone.now() - timedelta(hours=1))
    certification.housekeeping()
    award.refresh_from_db()
    assert award.status == AwardStatus.EXPIRED
    assert authorisation.evaluate(world.student, world.equipment)["status"] == "EXPIRED"


# ---------------------------------------------------------------------------
# Personal, supervisor and bootstrap
# ---------------------------------------------------------------------------
def test_status_me_supervisor_and_menus(world):
    gate_on(world.equipment)
    st = client_for(world.student).get(f"{AUTH}/status/", {"equipment_ids": f"{world.equipment.pk},{world.other_equipment.pk}"}).data["results"]
    assert st[str(world.equipment.pk)]["status"] == "NOT_AUTHORISED" and st[str(world.other_equipment.pk)]["status"] == "NOT_REQUIRED"
    assert client_for(world.student).get(f"{AUTH}/status/", {"equipment_ids": world.equipment.pk, "user_id": world.outsider.id}).status_code == 403
    staff = client_for(world.oic).get(f"{AUTH}/status/", {"equipment_ids": world.equipment.pk, "user_id": world.student.id}).data["results"]
    assert staff[str(world.equipment.pk)]["staff_bypass"] is True

    authorisation.create_request(world.student, world.equipment)
    me = client_for(world.student).get(f"{AUTH}/me/").data
    assert me["equipment"][0]["status"] == "NOT_AUTHORISED" and me["requests"][0]["status"] == "PENDING"
    sup = client_for(world.faculty).get(f"{AUTH}/me/").data["supervisor"]
    row = next(r for r in sup["students"] if r["student"]["id"] == world.student.id)
    assert row["equipment"][0]["status"] == "NOT_AUTHORISED" and row["equipment"][0]["open_request"]["status"] == "PENDING"
    assert all(r["student"]["id"] != world.student2.id for r in sup["students"])

    assert client_for(world.student).get(f"{API}/bootstrap/").data["menus"]["my_authorisations"] is True
    assert client_for(world.operator).get(f"{API}/bootstrap/").data["menus"]["training_authorisations"] is True
    assert client_for(world.student).get(f"{API}/bootstrap/").data["menus"]["training_authorisations"] is False


def test_endpoints_quiet_when_module_off(world, settings):
    gate_on(world.equipment)
    settings.TRAINING_MODULE_ENABLED = False
    TrainingModuleSettings.objects.filter(pk=1).update(module_enabled=False)
    assert client_for(world.student).get(f"{AUTH}/gated-equipment/").data["equipment_ids"] == []
    assert client_for(world.student).get(f"{AUTH}/me/").data["equipment"] == []
    st = client_for(world.student).get(f"{AUTH}/status/", {"equipment_ids": world.equipment.pk}).data["results"]
    assert st[str(world.equipment.pk)]["status"] == "NOT_REQUIRED"
    assert AuthorisationRequest.objects.count() == 0

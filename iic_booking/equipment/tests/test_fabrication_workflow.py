"""Fabrication workflow: reject as not feasible, replace window, automatic cancel, pickup email, FBR in emails."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.core import mail
from django.utils import timezone

from iic_booking.equipment import booking_events, fabrication_workflow, print_3d_notifications
from iic_booking.equipment.fabrication_workflow import (
    PICKUP_EMAIL,
    REJECTED_EMAIL,
    REPLACED_EMAIL,
    expire_fabrication_rejections,
)
from iic_booking.equipment.models import (
    Booking,
    BookingEvent,
    BookingEventType,
    BookingStatus,
    DailySlot,
    EquipmentManager,
    EquipmentOperator,
    IstemFbrStatus,
    LaserCutBatch,
)
from iic_booking.equipment.serializers import BookingListSerializer, BookingSerializer
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

from .fabrication_helpers import acrylic_3mm, funded_student, laser_equipment, laser_part

REASON = "The inner walls are thinner than the laser kerf."


@pytest.fixture
def media_tmp(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    settings.AWS_STORAGE_BUCKET_NAME = ""
    return tmp_path


@pytest.fixture
def sent(monkeypatch):
    """Template emails to users, lab file emails and booking event notifications, recorded instead of queued."""
    calls = SimpleNamespace(user=[], lab=[], events=[])

    def _send_user_email(booking, template_code, context, event):
        calls.user.append(SimpleNamespace(booking=booking, template=template_code, context=context, event=event))

    monkeypatch.setattr(fabrication_workflow, "_send_user_email", _send_user_email)
    monkeypatch.setattr(
        print_3d_notifications,
        "dispatch_fabrication_file_email",
        lambda booking, *, reason="confirmed": calls.lab.append((booking.booking_id, reason)),
    )
    monkeypatch.setattr(booking_events, "_dispatch_booking_event_notification", calls.events.append)
    return calls


@pytest.fixture
def lab(egs_factory, media_tmp):
    eq = laser_equipment(egs_factory, emails=["laser-lab@example.com"])
    acr = acrylic_3mm(eq)
    student, sub = funded_student(egs_factory)
    booking = egs_factory.booking(student, eq, egs_factory.future(), total_charge="202.00")
    batch = LaserCutBatch.objects.create(equipment=eq, user=student, booking=booking, status="COMPLETED")
    laser_part(eq, student, acr, quantity=5, name="old", batch=batch, booking=booking)
    oic = UserFactory(user_type=UserType.MANAGER, department=egs_factory.department, admin_approved=True)
    operator = UserFactory(user_type=UserType.OPERATOR, department=egs_factory.department, admin_approved=True)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    EquipmentOperator.objects.create(equipment=eq, operator=operator)
    admin = UserFactory(user_type=UserType.ADMIN, admin_approved=True)
    return SimpleNamespace(
        factory=egs_factory, eq=eq, acr=acr, student=student, sub=sub, booking=booking, oic=oic,
        operator=operator, admin=admin,
    )


def _reject(lab, user=None, reason=REASON, booking=None):
    booking = booking or lab.booking
    return lab.factory.client_for(user or lab.operator).post(
        f"/api/bookings/{booking.pk}/fabrication-reject/", {"reason": reason}, format="json"
    )


def _replace(lab, user, body, booking=None):
    booking = booking or lab.booking
    return lab.factory.client_for(user).post(f"/api/bookings/{booking.pk}/fabrication-files/", body, format="json")


def _new_files(lab, user=None, **part_kwargs):
    user = user or lab.student
    batch = LaserCutBatch.objects.create(equipment=lab.eq, user=user, status="COMPLETED")
    laser_part(lab.eq, user, lab.acr, batch=batch, **part_kwargs)
    return {"laser_cut_batch_id": str(batch.id)}


def _set_rejected(booking, *, deadline):
    Booking.objects.filter(pk=booking.pk).update(
        fabrication_rejected_at=deadline - timedelta(hours=24),
        fabrication_replace_deadline=deadline,
        fabrication_rejection_reason=REASON,
    )
    booking.refresh_from_db()


# --------------------------------------------------------------------------- reject


@pytest.mark.django_db
@pytest.mark.parametrize("who", ["operator", "oic", "admin"])
def test_lab_staff_of_the_equipment_can_reject(lab, sent, who, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        resp = _reject(lab, getattr(lab, who))
    assert resp.status_code == 200, resp.data
    lab.booking.refresh_from_db()
    assert lab.booking.status == BookingStatus.BOOKED
    assert lab.booking.fabrication_rejected_by == getattr(lab, who)
    assert [e.template for e in sent.user] == [REJECTED_EMAIL]


@pytest.mark.django_db
@pytest.mark.parametrize("who", ["booking_user", "other_operator", "other_oic", "stranger"])
def test_others_cannot_reject(lab, sent, who):
    other_eq = laser_equipment(lab.factory)
    users = {
        "booking_user": lambda: lab.student,
        "other_operator": lambda: UserFactory(user_type=UserType.OPERATOR, admin_approved=True),
        "other_oic": lambda: UserFactory(user_type=UserType.MANAGER, admin_approved=True),
        "stranger": lambda: lab.factory.student(),
    }
    user = users[who]()
    if who == "other_operator":
        EquipmentOperator.objects.create(equipment=other_eq, operator=user)
    if who == "other_oic":
        EquipmentManager.objects.create(equipment=other_eq, manager=user)
    resp = _reject(lab, user)
    assert resp.status_code == 403
    lab.booking.refresh_from_db()
    assert lab.booking.fabrication_rejected_at is None
    assert sent.user == []


@pytest.mark.django_db
def test_reject_requires_a_reason(lab, sent):
    resp = _reject(lab, reason="  too thin ")
    assert resp.status_code == 400
    assert "at least 10 characters" in resp.data["error"]
    lab.booking.refresh_from_db()
    assert lab.booking.fabrication_rejected_at is None


@pytest.mark.django_db
def test_reject_sets_state_deadline_history_and_emails_user(lab, sent, django_capture_on_commit_callbacks):
    lab.eq.fabrication_replace_window_hours = 6
    lab.eq.save(update_fields=["fabrication_replace_window_hours"])
    before = timezone.now()
    with django_capture_on_commit_callbacks(execute=True):
        resp = _reject(lab)
    assert resp.status_code == 200, resp.data
    booking = Booking.objects.get(pk=lab.booking.pk)
    assert booking.status == BookingStatus.BOOKED
    assert booking.fabrication_rejection_reason == REASON
    assert booking.fabrication_rejected_at >= before
    assert booking.fabrication_replace_deadline - booking.fabrication_rejected_at == timedelta(hours=6)

    workflow = resp.data["booking"]["fabrication_workflow"]
    assert workflow["rejected"] is True and workflow["reason"] == REASON
    assert workflow["replace_window_hours"] == 6
    assert resp.data["booking"]["status_display"] == "Rejected – waiting for new files"
    row = BookingListSerializer(booking, context={"request": None}).data
    assert row["status_display"] == "Rejected – waiting for new files"
    assert row["fabrication_replace_deadline"] is not None

    event = BookingEvent.objects.filter(booking=booking, event_type=BookingEventType.COMMENT).latest("created_at")
    assert REASON in event.comment
    assert event.metadata["fabrication_rejection"]["action"] == "rejected"
    assert sent.events == []  # history only, no generic comment email

    (email,) = sent.user
    assert email.template == REJECTED_EMAIL
    assert email.context["rejection_reason"] == REASON
    assert email.context["replace_deadline_display"].endswith(("AM", "PM"))
    assert "/my-bookings?booking=" in email.context["link"]
    assert "old × 5 — Acrylic sheet 3 mm" in email.context["parts_text"]

    # Already rejected, and only Booked bookings.
    assert _reject(lab).status_code == 400
    Booking.objects.filter(pk=booking.pk).update(status=BookingStatus.COMPLETED, fabrication_rejected_at=None)
    assert _reject(lab).status_code == 400


@pytest.mark.django_db
def test_reject_is_only_for_fabrication_equipment(lab, sent):
    eq = lab.factory.equipment()
    other = lab.factory.booking(lab.student, eq, lab.factory.future())
    resp = _reject(lab, lab.admin, booking=other)
    assert resp.status_code == 400
    assert BookingSerializer(other, context={"request": None}).data["fabrication_workflow"] is None


@pytest.mark.django_db
def test_rejected_email_template_renders(lab, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        assert _reject(lab).status_code == 200
    (email,) = [m for m in mail.outbox if lab.student.email in m.to]
    body = email.body
    assert REASON in body
    assert "Booking ID" in body
    assert "Replace files" in (email.alternatives[0][0] if email.alternatives else body)


# --------------------------------------------------------------------------- replace


@pytest.mark.django_db
def test_user_replaces_files_while_rejected_even_after_slot_start(lab, sent, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        assert _reject(lab).status_code == 200
    DailySlot.objects.filter(booking=lab.booking).update(
        start_datetime=timezone.now() - timedelta(minutes=30), end_datetime=timezone.now() + timedelta(minutes=30)
    )
    sent.user.clear()

    detail = BookingSerializer(Booking.objects.get(pk=lab.booking.pk), context={"request": _req(lab.student)}).data
    assert detail["fabrication_files_replaceable"] == {"allowed": True, "reason": None}

    with django_capture_on_commit_callbacks(execute=True):
        resp = _replace(lab, lab.student, _new_files(lab, width="100", height="100", quantity=2, name="new"))
    assert resp.status_code == 200, resp.data
    booking = Booking.objects.get(pk=lab.booking.pk)
    assert booking.status == BookingStatus.BOOKED
    assert booking.fabrication_rejected_at is None and booking.fabrication_replace_deadline is None
    assert booking.total_charge < Decimal("202.00")  # charge recalculated for the new parts
    assert resp.data["booking"]["fabrication_workflow"]["rejected"] is False

    assert [e.template for e in sent.user] == [REPLACED_EMAIL]
    assert sent.lab == [(booking.booking_id, print_3d_notifications.REASON_FILES_REPLACED_AFTER_REJECTION)]
    actions = [
        (e.metadata or {}).get("fabrication_rejection", {}).get("action")
        for e in BookingEvent.objects.filter(booking=booking, event_type=BookingEventType.COMMENT)
    ]
    assert "rejected" in actions and "replaced" in actions

    # Back to normal: the user can no longer change the files.
    resp = _replace(lab, lab.student, _new_files(lab, name="again"))
    assert resp.status_code == 400
    assert "cannot be changed after booking" in resp.data["error"]


@pytest.mark.django_db
def test_user_cannot_replace_after_the_deadline(lab, sent):
    _set_rejected(lab.booking, deadline=timezone.now() - timedelta(minutes=1))
    resp = _replace(lab, lab.student, _new_files(lab, name="late"))
    assert resp.status_code == 400
    assert "time to replace the files has ended" in resp.data["error"]


@pytest.mark.django_db
def test_staff_change_keeps_the_before_slot_rule_and_uses_the_normal_lab_email(
    lab, sent, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        resp = _replace(lab, lab.operator, _new_files(lab, user=lab.operator, name="by-lab"))
    assert resp.status_code == 200, resp.data
    assert sent.lab == [(lab.booking.booking_id, print_3d_notifications.REASON_FILES_UPDATED)]
    assert sent.user == []


@pytest.mark.django_db
def test_replace_recalculates_through_the_shared_charge_service(lab, sent, monkeypatch):
    from iic_booking.equipment import api_views

    calls = []
    real = api_views._recalculate_booking_charge_and_adjust_wallet

    def _spy(request, booking, **kwargs):
        calls.append(booking.pk)
        return real(request, booking, **kwargs)

    monkeypatch.setattr(api_views, "_recalculate_booking_charge_and_adjust_wallet", _spy)
    _set_rejected(lab.booking, deadline=timezone.now() + timedelta(hours=2))
    assert _replace(lab, lab.student, _new_files(lab, name="n")).status_code == 200
    assert calls == [lab.booking.pk]


@pytest.mark.django_db
def test_lab_email_after_rejection_goes_to_operators_oic_and_list_once(lab, settings, media_tmp):
    lab.eq.fabrication_notification_emails = ["laser-lab@example.com", lab.oic.email.upper()]
    lab.eq.save(update_fields=["fabrication_notification_emails"])
    assert print_3d_notifications.send_print_3d_stl_booking_email(
        lab.booking.booking_id, reason=print_3d_notifications.REASON_FILES_REPLACED_AFTER_REJECTION
    )
    (email,) = mail.outbox
    lowered = [e.lower() for e in email.to]
    assert sorted(lowered) == sorted({"laser-lab@example.com", lab.oic.email.lower(), lab.operator.email.lower()})
    assert email.subject.startswith("NEW FILES AFTER REJECTION")
    assert "after the lab rejected it as not feasible" in email.body


# --------------------------------------------------------------------------- expiry


@pytest.mark.django_db
def test_expired_rejection_is_cancelled_with_full_refund_once(lab, sent, django_capture_on_commit_callbacks):
    _set_rejected(lab.booking, deadline=timezone.now() - timedelta(minutes=1))
    balance_before = _balance(lab.sub)
    slot_ids = list(lab.booking.daily_slots.values_list("id", flat=True))

    with django_capture_on_commit_callbacks(execute=True):
        assert expire_fabrication_rejections() == 1
    booking = Booking.objects.get(pk=lab.booking.pk)
    assert booking.status == BookingStatus.REFUNDED
    assert _balance(lab.sub) - balance_before == Decimal("202.00")
    assert not DailySlot.objects.filter(id__in=slot_ids, booking=booking).exists()

    refund_event = BookingEvent.objects.get(booking=booking, event_type=BookingEventType.REFUNDED)
    assert "Files not replaced within 24 hours after rejection" in refund_event.comment
    assert refund_event.created_by is None
    assert sent.events == [refund_event.event_id]  # user + Lab Operators / OIC get the refund email

    (lab_mail,) = [m for m in mail.outbox if "laser-lab@example.com" in m.to]
    assert "Files not replaced within 24 hours after rejection" in lab_mail.body
    assert "₹202.00" in lab_mail.body

    with django_capture_on_commit_callbacks(execute=True):
        assert expire_fabrication_rejections() == 0
    assert BookingEvent.objects.filter(booking=booking, event_type=BookingEventType.REFUNDED).count() == 1
    assert _balance(lab.sub) - balance_before == Decimal("202.00")


@pytest.mark.django_db
def test_rejection_before_the_deadline_is_not_cancelled(lab, sent):
    _set_rejected(lab.booking, deadline=timezone.now() + timedelta(minutes=5))
    assert expire_fabrication_rejections() == 0
    lab.booking.refresh_from_db()
    assert lab.booking.status == BookingStatus.BOOKED


@pytest.mark.django_db
def test_expiry_task_is_registered_and_scheduled():
    import importlib

    from django.apps import apps as django_apps
    from django_celery_beat.models import PeriodicTask

    from iic_booking.equipment import tasks

    assert tasks.expire_fabrication_rejections.name == "equipment.expire_fabrication_rejections"
    # --reuse-db databases may have been flushed by transactional tests, so run the migration step again.
    migration = importlib.import_module("iic_booking.equipment.migrations.0230_fabrication_rejection_workflow")
    migration.create_expire_fabrication_rejections_schedule(django_apps, None)
    migration.create_expire_fabrication_rejections_schedule(django_apps, None)
    task = PeriodicTask.objects.get(task="equipment.expire_fabrication_rejections")
    assert task.enabled and task.interval.every == 10 and task.interval.period == "minutes"


@pytest.mark.django_db
def test_user_can_cancel_a_rejected_booking_with_full_refund_inside_the_cancel_window(lab, sent):
    DailySlot.objects.filter(booking=lab.booking).update(
        start_datetime=timezone.now() + timedelta(hours=1), end_datetime=timezone.now() + timedelta(hours=2)
    )
    resp = lab.factory.client_for(lab.student).post(f"/api/bookings/{lab.booking.pk}/user-cancel/", {}, format="json")
    assert resp.status_code == 400  # normal rule: too close to the slot

    _set_rejected(lab.booking, deadline=timezone.now() + timedelta(hours=3))
    balance_before = _balance(lab.sub)
    resp = lab.factory.client_for(lab.student).post(f"/api/bookings/{lab.booking.pk}/user-cancel/", {}, format="json")
    assert resp.status_code == 200, resp.data
    lab.booking.refresh_from_db()
    assert lab.booking.status == BookingStatus.REFUNDED
    assert _balance(lab.sub) - balance_before == Decimal("202.00")


# --------------------------------------------------------------------------- staff actions / completion


@pytest.mark.django_db
def test_sample_status_changes_are_refused_for_fabrication(lab):
    resp = lab.factory.client_for(lab.admin).post(
        f"/api/bookings/{lab.booking.pk}/sample-trace/set/", {"status": "SAMPLE_ACCEPTED"}, format="json"
    )
    assert resp.status_code == 400
    assert "do not use the sample lifecycle" in resp.data["error"]


@pytest.mark.django_db
def test_rejected_booking_cannot_be_completed(lab, sent):
    _set_rejected(lab.booking, deadline=timezone.now() + timedelta(hours=3))
    resp = lab.factory.client_for(lab.admin).post(f"/api/bookings/{lab.booking.pk}/complete/", {}, format="json")
    assert resp.status_code == 400
    assert "waiting for new files" in resp.data["error"]


@pytest.mark.django_db
def test_completion_sends_pickup_email_for_fabrication(lab, sent, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        resp = lab.factory.client_for(lab.admin).post(f"/api/bookings/{lab.booking.pk}/complete/", {}, format="json")
    assert resp.status_code == 200, resp.data
    (email,) = sent.user
    assert email.template == PICKUP_EMAIL
    assert "old × 5 — Acrylic sheet 3 mm" in email.context["parts_text"]
    assert email.context["pickup_instructions"]
    assert email.context["total_charge"] == "₹202.00"
    assert not lab.booking.sample_trace_events.exists()


@pytest.mark.django_db
def test_pickup_email_template_renders(lab, media_tmp):
    lab.booking.status = BookingStatus.COMPLETED
    lab.booking.save(update_fields=["status"])
    fabrication_workflow.send_pickup_email(Booking.objects.select_related("equipment", "user").get(pk=lab.booking.pk))
    (email,) = mail.outbox
    assert "ready for pickup" in email.subject.lower()
    assert "old × 5" in email.body


@pytest.mark.django_db
def test_other_equipment_keeps_the_standard_completion_email(egs_factory, monkeypatch):
    from iic_booking.equipment import api_views

    eq = egs_factory.equipment()
    booking = egs_factory.booking(egs_factory.student(), eq, egs_factory.future())
    picked = []
    monkeypatch.setattr(fabrication_workflow, "send_pickup_email", picked.append)
    from iic_booking.communication.service import CommunicationService

    templates = []
    real_get = CommunicationService.get_template

    def _get_template(*args, **kwargs):
        templates.append(kwargs.get("template"))
        return real_get(*args, **kwargs)

    monkeypatch.setattr(CommunicationService, "get_template", staticmethod(_get_template))
    api_views._send_completion_email_with_attachments(booking, [])
    assert picked == []
    assert "booking_completed_email" in templates


# --------------------------------------------------------------------------- replace window setting


@pytest.mark.django_db
def test_replace_window_setting_on_the_materials_page(lab):
    client = lab.factory.client_for(lab.oic)
    url = "/api/oic/fabrication-materials/equipment/"
    for bad in (0, 169, "abc"):
        resp = client.patch(url, {"equipment_id": lab.eq.pk, "fabrication_replace_window_hours": bad}, format="json")
        assert resp.status_code == 400, bad
    resp = client.patch(url, {"equipment_id": lab.eq.pk, "fabrication_replace_window_hours": 12}, format="json")
    assert resp.status_code == 200, resp.data
    assert resp.data["equipment"]["fabrication_replace_window_hours"] == 12
    lab.eq.refresh_from_db()
    assert lab.eq.fabrication_replace_window_hours == 12
    rows = client.get(url).data["equipments"]
    assert [r["fabrication_replace_window_hours"] for r in rows if r["equipment_id"] == lab.eq.pk] == [12]


# --------------------------------------------------------------------------- FBR number in emails


def _fbr_booking(lab, *, required: bool, number="FBR-2026-0042"):
    Booking.objects.filter(pk=lab.booking.pk).update(
        istem_fbr_number=number, istem_fbr_status=IstemFbrStatus.PENDING_OIC if required else None
    )
    lab.booking.refresh_from_db()
    return lab.booking


def _render(code, context):
    from iic_booking.communication.models import CommunicationTemplate
    from iic_booking.communication.service import CommunicationService
    from iic_booking.equipment.booking_lab_messages import ensure_email_template

    ensure_email_template(code)
    template = CommunicationTemplate.objects.get(code=code)
    return CommunicationService.render_template(template, context)


@pytest.mark.django_db
def test_fbr_number_is_added_to_booking_emails_when_required(lab):
    from iic_booking.communication.utils import booking_display_id_for_email

    booking = _fbr_booking(lab, required=True)
    ctx = {"user_name": "A", "booking_id": booking_display_id_for_email(booking), "equipment_name": "Laser"}
    rendered = _render("booking_refunded_email", ctx)
    assert "FBR number: FBR-2026-0042" in rendered["message"]
    assert "FBR-2026-0042" in rendered["html_message"]
    assert rendered["message"].count("FBR number") == 1


@pytest.mark.django_db
def test_fbr_number_is_not_added_when_not_required_or_blank(lab):
    from iic_booking.communication.utils import booking_display_id_for_email

    booking = _fbr_booking(lab, required=False)
    ctx = {"user_name": "A", "booking_id": booking_display_id_for_email(booking), "equipment_name": "Laser"}
    rendered = _render("booking_refunded_email", ctx)
    assert "FBR" not in rendered["message"] and "FBR" not in (rendered["html_message"] or "")

    booking = _fbr_booking(lab, required=True, number="")
    rendered = _render("booking_refunded_email", ctx)
    assert "FBR" not in rendered["message"]


@pytest.mark.django_db
def test_fbr_number_follows_the_booking_charge_profile(lab):
    from iic_booking.equipment.fbr_email import booking_fbr_number_for_email

    Booking.objects.filter(pk=lab.booking.pk).update(istem_fbr_number="FBR-77")
    lab.booking.refresh_from_db()
    assert booking_fbr_number_for_email(lab.booking) == ""
    lab.booking.charge_profile.require_istem_fbr = True
    lab.booking.charge_profile.save(update_fields=["require_istem_fbr"])
    lab.booking.refresh_from_db()
    assert booking_fbr_number_for_email(lab.booking) == "FBR-77"


@pytest.mark.django_db
def test_fbr_number_in_event_context_and_lab_and_plain_emails(lab, media_tmp):
    from iic_booking.equipment.maintenance_policy import _fbr_block

    booking = _fbr_booking(lab, required=True)
    ctx = {}
    booking_events.apply_booking_party_to_context(ctx, booking)
    assert ctx["fbr_number"] == "FBR-2026-0042"
    assert _fbr_block(booking) == "FBR number: FBR-2026-0042\n\n"

    assert print_3d_notifications.send_print_3d_stl_booking_email(booking.booking_id)
    assert "FBR number: FBR-2026-0042" in mail.outbox[-1].body

    booking = _fbr_booking(lab, required=False)
    assert _fbr_block(booking) == ""
    ctx = {}
    booking_events.apply_booking_party_to_context(ctx, booking)
    assert ctx["fbr_number"] == ""
    assert print_3d_notifications.send_print_3d_stl_booking_email(booking.booking_id)
    assert "FBR" not in mail.outbox[-1].body


@pytest.mark.django_db
def test_fabrication_emails_include_fbr_when_required(lab, django_capture_on_commit_callbacks):
    _fbr_booking(lab, required=True)
    with django_capture_on_commit_callbacks(execute=True):
        assert _reject(lab).status_code == 200
    (email,) = [m for m in mail.outbox if lab.student.email in m.to]
    assert "FBR number: FBR-2026-0042" in email.body


# --------------------------------------------------------------------------- helpers


def _req(user):
    return SimpleNamespace(user=user)


def _balance(sub):
    sub.refresh_from_db()
    return Decimal(str(sub.balance))

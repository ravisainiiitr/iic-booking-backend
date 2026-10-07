"""Laser booking end to end (upload, edit part, book) and replacing files after booking."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from iic_booking.equipment.fabrication import RESERVED_KEYS
from iic_booking.equipment.input_edit_payment_window import INPUT_EDIT_PAYMENT_GRACE_SECONDS, expire_unpaid_input_edits
from iic_booking.equipment.models import (
    Booking,
    BookingStatus,
    EquipmentManager,
    EquipmentOperator,
    FabricationFileChange,
    LaserCutAnalysis,
    LaserCutBatch,
    PrintAnalysisBatch,
)
from iic_booking.equipment.serializers import BookingSerializer
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

from .fabrication_helpers import (
    acrylic_3mm,
    dxf_bytes,
    funded_student,
    laser_equipment,
    laser_part,
    print_equipment,
    print_material,
    print_part,
)


@pytest.fixture
def media_tmp(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    settings.AWS_STORAGE_BUCKET_NAME = ""
    return tmp_path


@pytest.fixture
def no_portal_lock(monkeypatch):
    from iic_booking.users.legacy_ledger import booking_lock

    monkeypatch.setattr(booking_lock, "booking_is_locked", lambda user: (False, ""))
    monkeypatch.setattr(booking_lock, "department_equipment_booking_blocked", lambda equipment, user: (False, ""))


def _upload(client, eq, name, data, **extra):
    return client.post(
        f"/api/equipments/{eq.pk}/analyze-dxf/",
        {"file": SimpleUploadedFile(name, data, content_type="application/dxf"), **extra},
        format="multipart",
    )


def _book_body(slot, **extra):
    return {
        "slot_ids": [slot.pk],
        "start_time": slot.start_datetime.isoformat(),
        "end_time": slot.end_datetime.isoformat(),
        "input_values": {},
        **extra,
    }


@pytest.mark.django_db
def test_laser_upload_edit_and_book_end_to_end(egs_factory, media_tmp, no_portal_lock):
    eq = laser_equipment(egs_factory, own_charge="250")
    acr = acrylic_3mm(eq)
    student, _sub = funded_student(egs_factory)
    client = egs_factory.client_for(student)

    resp = _upload(client, eq, "bracket.dxf", dxf_bytes(rects=((0, 0, 200, 100),)), material_id=acr.pk)
    assert resp.status_code == 200, resp.data
    batch_id = resp.data["id"]
    item = resp.data["items"][0]
    assert item["part_name"] == "bracket"
    assert Decimal(item["width_mm"]) == Decimal("200")
    assert item["fit_error"] in (None, "")

    resp = client.patch(f"/api/laser-cut-analyses/{item['id']}/", {"quantity": 5}, format="json")
    assert resp.status_code == 200, resp.data
    assert resp.data["quantity"] == 5
    assert resp.data["estimated_material_cost"] == "202.43"

    slot = egs_factory.slot(eq, egs_factory.future(days=2))
    resp = client.post(f"/api/equipments/{eq.pk}/book/", _book_body(slot, laser_cut_batch_id=batch_id), format="json")
    assert resp.status_code in (200, 201), resp.data

    booking = Booking.objects.get(user=student, equipment=eq)
    assert booking.total_charge == Decimal("202")
    assert not booking.own_material
    assert not any(key in (booking.input_values or {}) for key in RESERVED_KEYS)
    assert any(line.get("exact_amount") == "202.43" for line in booking.charge_breakdown)
    assert LaserCutAnalysis.objects.get(pk=item["id"]).booking_id == booking.pk
    assert LaserCutBatch.objects.get(pk=batch_id).booking_id == booking.pk

    data = BookingSerializer(booking, context={"request": None}).data
    assert [p["name"] for p in data["fabrication_parts"]] == ["bracket"]
    assert data["laser_cut_analyses"][0]["quantity"] == 5
    assert data["print_analyses"] == []

    # The same upload cannot be booked twice, and booked parts are locked for direct edits.
    second = egs_factory.slot(eq, egs_factory.future(days=2, hour=14))
    resp = client.post(f"/api/equipments/{eq.pk}/book/", _book_body(second, laser_cut_batch_id=batch_id), format="json")
    assert resp.status_code == 400
    assert client.patch(f"/api/laser-cut-analyses/{item['id']}/", {"quantity": 1}, format="json").status_code == 400


@pytest.mark.django_db
def test_laser_booking_with_own_material_and_missing_upload(egs_factory, media_tmp, no_portal_lock):
    eq = laser_equipment(egs_factory, own_charge="250")
    acr = acrylic_3mm(eq)
    student, _sub = funded_student(egs_factory)
    client = egs_factory.client_for(student)
    slot = egs_factory.slot(eq, egs_factory.future(days=2))

    resp = client.post(f"/api/equipments/{eq.pk}/book/", _book_body(slot), format="json")
    assert resp.status_code == 400
    assert "DXF" in resp.data["error"]

    batch_id = _upload(client, eq, "a.dxf", dxf_bytes(), material_id=acr.pk).data["id"]
    resp = client.post(
        f"/api/equipments/{eq.pk}/book/",
        _book_body(slot, laser_cut_batch_id=batch_id, own_material=True),
        format="json",
    )
    assert resp.status_code in (200, 201), resp.data
    booking = Booking.objects.get(user=student, equipment=eq)
    assert booking.own_material
    assert booking.total_charge == Decimal("250")


@pytest.mark.django_db
def test_part_that_does_not_fit_the_sheet_cannot_be_booked(egs_factory, media_tmp, no_portal_lock):
    eq = laser_equipment(egs_factory)
    acr = acrylic_3mm(eq)
    student, _sub = funded_student(egs_factory)
    client = egs_factory.client_for(student)
    resp = _upload(client, eq, "huge.dxf", dxf_bytes(rects=((0, 0, 3000, 100),)), material_id=acr.pk)
    assert resp.data["items"][0]["fit_error"]
    slot = egs_factory.slot(eq, egs_factory.future(days=2))
    resp = client.post(
        f"/api/equipments/{eq.pk}/book/", _book_body(slot, laser_cut_batch_id=resp.data["id"]), format="json"
    )
    assert resp.status_code == 400
    assert "does not fit" in resp.data["error"]


@pytest.mark.django_db
def test_non_fitting_part_can_still_be_renamed_but_not_moved_to_a_small_sheet(egs_factory, media_tmp):
    eq = laser_equipment(egs_factory)
    acr = acrylic_3mm(eq)
    student, _sub = funded_student(egs_factory)
    client = egs_factory.client_for(student)
    item = _upload(client, eq, "huge.dxf", dxf_bytes(rects=((0, 0, 3000, 100),)), material_id=acr.pk).data["items"][0]

    resp = client.patch(f"/api/laser-cut-analyses/{item['id']}/", {"part_name": "rail", "quantity": 2}, format="json")
    assert resp.status_code == 200, resp.data
    assert resp.data["quantity"] == 2 and resp.data["fit_error"]

    resp = client.patch(f"/api/laser-cut-analyses/{item['id']}/", {"material_id": None}, format="json")
    assert resp.status_code == 200
    resp = client.patch(f"/api/laser-cut-analyses/{item['id']}/", {"material_id": acr.pk}, format="json")
    assert resp.status_code == 400
    assert "does not fit" in resp.data["error"]


@pytest.mark.django_db
def test_oversize_part_is_accepted_when_the_user_brings_own_sheet(egs_factory, media_tmp, no_portal_lock):
    eq = laser_equipment(egs_factory, own_charge="250")
    acr = acrylic_3mm(eq)
    student, _sub = funded_student(egs_factory)
    client = egs_factory.client_for(student)
    batch = _upload(client, eq, "huge.dxf", dxf_bytes(rects=((0, 0, 3000, 1500),)), material_id=acr.pk).data
    assert batch["items"][0]["fit_error"]

    calc = f"/api/equipments/{eq.pk}/calculate/?laser_cut_batch_id={batch['id']}"
    resp = client.get(calc)
    assert resp.status_code == 400 and "does not fit" in resp.data["error"]
    resp = client.get(f"{calc}&own_material=true")
    assert resp.status_code == 200, resp.data

    slot = egs_factory.slot(eq, egs_factory.future(days=2))
    resp = client.post(f"/api/equipments/{eq.pk}/book/", _book_body(slot, laser_cut_batch_id=batch["id"]), format="json")
    assert resp.status_code == 400 and "does not fit" in resp.data["error"]
    resp = client.post(
        f"/api/equipments/{eq.pk}/book/",
        _book_body(slot, laser_cut_batch_id=batch["id"], own_material=True),
        format="json",
    )
    assert resp.status_code in (200, 201), resp.data
    booking = Booking.objects.get(user=student, equipment=eq)
    assert booking.own_material and booking.total_charge == Decimal("250")


@pytest.mark.django_db
def test_own_material_flag_is_ignored_for_size_when_the_equipment_does_not_offer_it(
    egs_factory, media_tmp, no_portal_lock
):
    eq = laser_equipment(egs_factory)
    acr = acrylic_3mm(eq)
    student, _sub = funded_student(egs_factory)
    client = egs_factory.client_for(student)
    batch_id = _upload(client, eq, "huge.dxf", dxf_bytes(rects=((0, 0, 3000, 100),)), material_id=acr.pk).data["id"]
    slot = egs_factory.slot(eq, egs_factory.future(days=2))
    resp = client.post(
        f"/api/equipments/{eq.pk}/book/", _book_body(slot, laser_cut_batch_id=batch_id, own_material=True), format="json"
    )
    assert resp.status_code == 400 and "does not fit" in resp.data["error"]


@pytest.mark.django_db
def test_part_can_move_to_a_small_sheet_while_own_material_is_ticked(egs_factory, media_tmp):
    eq = laser_equipment(egs_factory, own_charge="250")
    acr = acrylic_3mm(eq)
    student, _sub = funded_student(egs_factory)
    client = egs_factory.client_for(student)
    item = _upload(client, eq, "huge.dxf", dxf_bytes(rects=((0, 0, 3000, 100),))).data["items"][0]
    url = f"/api/laser-cut-analyses/{item['id']}/"

    resp = client.patch(url, {"material_id": acr.pk}, format="json")
    assert resp.status_code == 400 and "does not fit" in resp.data["error"]
    resp = client.patch(url, {"material_id": acr.pk, "own_material": True}, format="json")
    assert resp.status_code == 200, resp.data
    # The part still reports the mismatch so the form can re-apply the check if the box is unticked.
    assert resp.data["material_id"] == acr.pk and resp.data["fit_error"]


@pytest.mark.django_db
def test_unitless_upload_offers_unit_change(egs_factory, media_tmp):
    eq = laser_equipment(egs_factory)
    acrylic_3mm(eq)
    student, _sub = funded_student(egs_factory)
    client = egs_factory.client_for(student)
    item = _upload(client, eq, "u.dxf", dxf_bytes(units=None, rects=((0, 0, 20, 10),))).data["items"][0]
    assert item["units_assumed"] is True
    resp = client.patch(f"/api/laser-cut-analyses/{item['id']}/", {"units": "cm"}, format="json")
    assert resp.status_code == 200, resp.data
    assert Decimal(resp.data["width_mm"]) == Decimal("200")

    item2 = _upload(client, eq, "mm.dxf", dxf_bytes(units=4)).data["items"][0]
    resp = client.patch(f"/api/laser-cut-analyses/{item2['id']}/", {"units": "cm"}, format="json")
    assert resp.status_code == 400


# --------------------------------------------------------------------------- re-upload


def _booked_laser(egs_factory, *, start=None, own_charge="250"):
    eq = laser_equipment(egs_factory, own_charge=own_charge)
    acr = acrylic_3mm(eq)
    student, sub = funded_student(egs_factory)
    booking = egs_factory.booking(student, eq, start or egs_factory.future(), total_charge="202.00")
    batch = LaserCutBatch.objects.create(equipment=eq, user=student, booking=booking, status="COMPLETED")
    old = laser_part(eq, student, acr, quantity=5, name="old", batch=batch, booking=booking)
    booking.charge_breakdown = [{"description": "old", "amount": 202.0}]
    booking.save(update_fields=["charge_breakdown"])
    return eq, acr, student, sub, booking, old


def _new_batch(eq, user, material, **part_kwargs):
    batch = LaserCutBatch.objects.create(equipment=eq, user=user, status="COMPLETED")
    part = laser_part(eq, user, material, batch=batch, **part_kwargs)
    return batch, part


def _replace(egs_factory, user, booking, body):
    return egs_factory.client_for(user).post(f"/api/bookings/{booking.pk}/fabrication-files/", body, format="json")


def _mark_rejected(booking, hours=24):
    """Users can only replace files while the lab's "not feasible" rejection is open."""
    now = timezone.now()
    Booking.objects.filter(pk=booking.pk).update(
        fabrication_rejected_at=now,
        fabrication_replace_deadline=now + timedelta(hours=hours),
        fabrication_rejection_reason="The walls are too thin to cut.",
    )
    booking.refresh_from_db()


@pytest.mark.django_db
def test_user_replacing_files_with_higher_charge_gets_pay_window_and_revert_restores_old_files(
    egs_factory, media_tmp
):
    eq, acr, student, _sub, booking, old = _booked_laser(egs_factory)
    batch, new = _new_batch(eq, student, acr, width="400", height="100", quantity=5, name="new")
    _mark_rejected(booking)

    resp = _replace(egs_factory, student, booking, {"laser_cut_batch_id": str(batch.id)})
    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    assert booking.fabrication_rejected_at is None
    old.refresh_from_db()
    new.refresh_from_db()
    assert booking.total_charge == Decimal("405")  # 404.86
    assert booking.charge_recalculation_pending_amount == Decimal("203.00")
    assert booking.charge_recalculation_pay_deadline is not None
    assert old.booking_id is None and old.superseded_booking_id == booking.pk
    assert new.booking_id == booking.pk
    change = FabricationFileChange.objects.get(booking=booking)
    assert [f["part_name"] for f in change.previous_files] == ["old"]
    assert [f["part_name"] for f in change.new_files] == ["new"]
    assert change.charge_before == Decimal("202.00") and change.charge_after == Decimal("405.00")
    assert resp.data["charge_recalculation_summary"]["extra_amount"] == "203.00"

    # A second change waits until the extra amount is paid or the window ends.
    batch2, _p = _new_batch(eq, student, acr, name="third")
    assert _replace(egs_factory, student, booking, {"laser_cut_batch_id": str(batch2.id)}).status_code == 400

    Booking.objects.filter(pk=booking.pk).update(
        charge_recalculation_pay_deadline=timezone.now() - timedelta(seconds=INPUT_EDIT_PAYMENT_GRACE_SECONDS + 1)
    )
    assert expire_unpaid_input_edits() == 1
    booking.refresh_from_db()
    old.refresh_from_db()
    new.refresh_from_db()
    change.refresh_from_db()
    assert booking.total_charge == Decimal("202.00")
    assert booking.charge_recalculation_pending_amount is None
    assert old.booking_id == booking.pk and old.superseded_at is None
    assert new.booking_id is None and new.superseded_booking_id == booking.pk
    assert change.reverted_at is not None
    # Undoing the unpaid change puts the rejection back.
    assert booking.fabrication_rejected_at is not None


@pytest.mark.django_db
def test_part_quantity_and_own_material_can_change_without_new_files(egs_factory, media_tmp):
    eq, _acr, student, _sub, booking, old = _booked_laser(egs_factory)
    oic = UserFactory(user_type=UserType.MANAGER, department=egs_factory.department, admin_approved=True)
    EquipmentManager.objects.create(equipment=eq, manager=oic)

    resp = _replace(
        egs_factory, oic, booking, {"part_updates": [{"analysis_id": str(old.id), "quantity": 10, "part_name": "x"}]}
    )
    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    old.refresh_from_db()
    assert (old.quantity, old.part_name) == (10, "x")
    assert booking.total_charge == Decimal("405")  # 404.86
    # Charge managers are not put on the one-minute payment window.
    assert booking.charge_recalculation_pay_deadline is None

    resp = _replace(egs_factory, oic, booking, {"own_material": True})
    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    assert booking.own_material and booking.total_charge == Decimal("250")

    assert _replace(egs_factory, oic, booking, {"own_material": True}).status_code == 400  # nothing to change


@pytest.mark.django_db
def test_replacing_with_oversize_parts_depends_on_own_material(egs_factory, media_tmp):
    eq, acr, _student, _sub, booking, _old = _booked_laser(egs_factory)
    oic = UserFactory(user_type=UserType.MANAGER, department=egs_factory.department, admin_approved=True)
    EquipmentManager.objects.create(equipment=eq, manager=oic)

    batch, big = _new_batch(eq, oic, acr, width="3000", height="1500", name="big")
    resp = _replace(egs_factory, oic, booking, {"laser_cut_batch_id": str(batch.id)})
    assert resp.status_code == 400 and "does not fit" in resp.data["error"]

    resp = _replace(egs_factory, oic, booking, {"laser_cut_batch_id": str(batch.id), "own_material": True})
    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    big.refresh_from_db()
    assert booking.own_material and big.booking_id == booking.pk

    # Unticking own material re-applies the sheet check.
    resp = _replace(egs_factory, oic, booking, {"own_material": False})
    assert resp.status_code == 400 and "does not fit" in resp.data["error"]
    booking.refresh_from_db()
    assert booking.own_material


@pytest.mark.django_db
def test_reupload_permissions_and_time_limits(egs_factory, media_tmp):
    eq, acr, student, _sub, booking, _old = _booked_laser(egs_factory)
    stranger = egs_factory.student()
    batch, _p = _new_batch(eq, student, acr, name="n")
    body = {"laser_cut_batch_id": str(batch.id)}

    assert _replace(egs_factory, stranger, booking, body).status_code in (403, 404)

    operator = UserFactory(user_type=UserType.OPERATOR, department=egs_factory.department, admin_approved=True)
    EquipmentOperator.objects.create(equipment=eq, operator=operator)
    resp = egs_factory.client_for(operator).get(f"/api/bookings/{booking.pk}/fabrication-files/")
    assert resp.status_code == 200
    assert resp.data["can_replace"] is True
    op_batch, _op = _new_batch(eq, operator, acr, name="by-operator")
    resp = _replace(egs_factory, operator, booking, {"laser_cut_batch_id": str(op_batch.id)})
    assert resp.status_code == 200, resp.data

    # The booking user cannot change files unless the lab rejected them.
    resp = _replace(egs_factory, student, booking, body)
    assert resp.status_code == 400
    assert "cannot be changed after booking" in resp.data["error"]

    # Once the slot has started, files are locked for lab staff.
    eq2, acr2, student2, _s2, started, _o2 = _booked_laser(egs_factory, start=timezone.now() - timedelta(minutes=5))
    EquipmentOperator.objects.create(equipment=eq2, operator=operator)
    batch2, _p2 = _new_batch(eq2, operator, acr2, name="late")
    resp = _replace(egs_factory, operator, started, {"laser_cut_batch_id": str(batch2.id)})
    assert resp.status_code == 400
    assert "before the booked slot starts" in resp.data["error"]

    # Only while BOOKED.
    _mark_rejected(booking)
    Booking.objects.filter(pk=booking.pk).update(status=BookingStatus.COMPLETED)
    batch3, _p3 = _new_batch(eq, student, acr, name="done")
    resp = _replace(egs_factory, student, booking, {"laser_cut_batch_id": str(batch3.id)})
    assert resp.status_code == 400


@pytest.mark.django_db
def test_print_reupload_updates_weight_and_time_and_checks_slots(egs_factory, media_tmp):
    eq = print_equipment(egs_factory, own_charge="100")
    pla = print_material(eq)
    student, _sub = funded_student(egs_factory)
    booking = egs_factory.booking(student, eq, egs_factory.future(), slot_count=2, total_charge="15.00")
    old = print_part(eq, student, pla, weight="10", minutes=30, name="old", booking=booking)
    booking.print_analysis = old
    booking.input_values = {"A": 10, "B": "PLA-FDM", "C": 30}
    booking.save(update_fields=["print_analysis", "input_values"])

    batch = PrintAnalysisBatch.objects.create(equipment=eq, user=student, material=pla, status="COMPLETED")
    new = print_part(eq, student, pla, weight="20", minutes=40, quantity=2, name="new", batch=batch)
    _mark_rejected(booking)
    resp = _replace(egs_factory, student, booking, {"print_analysis_batch_id": str(batch.id)})
    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    assert booking.input_values["A"] == 40 and booking.input_values["C"] == 80
    assert not any(key in booking.input_values for key in RESERVED_KEYS)
    assert booking.print_analysis_batch_id == batch.id
    assert booking.total_charge == Decimal("58")  # 40 g x 1.44 = 57.6
    old.refresh_from_db()
    assert old.superseded_booking_id == booking.pk

    Booking.objects.filter(pk=booking.pk).update(  # extra amount paid
        charge_recalculation_pending_amount=None,
        charge_recalculation_pay_deadline=None,
        charge_recalculation_revert_snapshot=None,
    )
    _mark_rejected(booking)
    resp = _replace(egs_factory, student, booking, {"part_updates": [{"analysis_id": str(new.id), "quantity": 5}]})
    assert resp.status_code == 400
    assert "more than the booked slot" in resp.data["error"]


@pytest.mark.django_db
def test_laser_bookings_are_full_cancel_only_and_not_waitlist_bookable(egs_factory, media_tmp, monkeypatch):
    from iic_booking.equipment import waitlist_booking
    from iic_booking.equipment.booking_cancellation import CancellationValidationError, parse_cancellation_request

    eq, _acr, student, _sub, _booking, _old = _booked_laser(egs_factory)
    booking = egs_factory.booking(student, eq, egs_factory.future(days=4), slot_count=2)
    slot_ids = list(booking.daily_slots.values_list("id", flat=True))
    with pytest.raises(CancellationValidationError, match="cancelled in full"):
        parse_cancellation_request({"slot_ids": slot_ids[:1]}, booking)
    assert parse_cancellation_request({"slot_ids": slot_ids}, booking)["mode"] == "full_slots"

    monkeypatch.setattr(waitlist_booking, "create_booking_event", lambda **kwargs: None)
    free = egs_factory.slot(eq, egs_factory.future(days=5))
    created, err = waitlist_booking.create_booking_for_waitlist_user(eq, student, [free.pk])
    assert created is None and err

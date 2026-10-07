"""Quantity Required (input A) of 3D print and 2D laser cutting bookings: it multiplies the whole job, the
preview equals the stored charge, older bookings count as 1, and the data command adds the input once."""

from __future__ import annotations

from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command

from iic_booking.equipment.fabrication import (
    PRINT_WEIGHT_KEY,
    QUANTITY_LABEL,
    QUANTITY_MARKER_KEY,
    RESERVED_KEYS,
    ensure_fabrication_quantity_inputs,
    job_quantity_from_values,
    parse_job_quantity,
)
from iic_booking.equipment.models import (
    Booking,
    DynamicInputField,
    EquipmentProfileType,
    PrintAnalysisBatch,
)
from iic_booking.equipment.serializers import BookingSerializer

from .fabrication_helpers import (
    acrylic_3mm,
    dxf_bytes,
    funded_student,
    laser_equipment,
    print_equipment,
    print_material,
    print_part,
)

LASER = EquipmentProfileType.LASER_CUT_2D
PRINT = EquipmentProfileType.PRINT_3D


@pytest.fixture(autouse=True)
def media_tmp(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    settings.AWS_STORAGE_BUCKET_NAME = ""
    return tmp_path


@pytest.fixture
def no_portal_lock(monkeypatch):
    from iic_booking.users.legacy_ledger import booking_lock

    monkeypatch.setattr(booking_lock, "booking_is_locked", lambda user: (False, ""))
    monkeypatch.setattr(booking_lock, "department_equipment_booking_blocked", lambda equipment, user: (False, ""))


def _book_body(slot, **extra):
    return {
        "slot_ids": [slot.pk],
        "start_time": slot.start_datetime.isoformat(),
        "end_time": slot.end_datetime.isoformat(),
        **extra,
    }


def _laser_batch(client, eq, quantity=5):
    from django.core.files.uploadedfile import SimpleUploadedFile

    acr = acrylic_3mm(eq)
    resp = client.post(
        f"/api/equipments/{eq.pk}/analyze-dxf/",
        {
            "file": SimpleUploadedFile("bracket.dxf", dxf_bytes(rects=((0, 0, 200, 100),)), content_type="application/dxf"),
            "material_id": acr.pk,
        },
        format="multipart",
    )
    assert resp.status_code == 200, resp.data
    item = resp.data["items"][0]
    assert client.patch(f"/api/laser-cut-analyses/{item['id']}/", {"quantity": quantity}, format="json").status_code == 200
    return resp.data["id"]


def _print_batch(eq, user, *, quantity=1):
    """One STL: ceil(10.2) = 11 g and 30 min per copy, PLA at 1.44/g."""
    pla = print_material(eq)
    batch = PrintAnalysisBatch.objects.create(equipment=eq, user=user, material=pla, status="COMPLETED")
    print_part(eq, user, pla, weight="10.2", minutes=30, quantity=quantity, batch=batch)
    return batch


# --------------------------------------------------------------------------- parsing and legacy rule


def test_parse_job_quantity_accepts_whole_numbers_from_1_to_1000():
    assert [parse_job_quantity(v) for v in (1, 2.0, "3", " 4 ", 1000)] == [1, 2, 3, 4, 1000]
    assert [parse_job_quantity(v) for v in (0, -1, 1.5, "abc", None, True, 1001, "inf", "nan")] == [None] * 9


def test_quantity_counts_only_for_bookings_saved_with_it():
    assert job_quantity_from_values(PRINT, {"A": 40, "B": "PLA", "C": 30}) == 1  # older 3D print: A was weight
    assert job_quantity_from_values(LASER, {"A": 5}) == 1  # input of the profile the equipment had before
    assert job_quantity_from_values(PRINT, {"A": 3, QUANTITY_MARKER_KEY: True}) == 3
    assert job_quantity_from_values(LASER, {QUANTITY_MARKER_KEY: True}) == 1
    assert job_quantity_from_values(EquipmentProfileType.SAMPLE, {"A": 3, QUANTITY_MARKER_KEY: True}) == 1


# --------------------------------------------------------------------------- laser


@pytest.mark.django_db
def test_laser_quantity_multiplies_material_share_and_preview_equals_stored_charge(egs_factory, no_portal_lock):
    eq = laser_equipment(egs_factory)
    student, _sub = funded_student(egs_factory)
    client = egs_factory.client_for(student)
    batch_id = _laser_batch(client, eq, quantity=5)
    calc = f"/api/equipments/{eq.pk}/calculate/?laser_cut_batch_id={batch_id}"

    one = client.get(calc)
    default = client.get(f"{calc}&A=1")
    three = client.get(f"{calc}&A=3")
    assert one.status_code == default.status_code == three.status_code == 200, three.data
    assert Decimal(one.data["applied_charge"]) == Decimal(default.data["applied_charge"]) == Decimal("202")
    # 5 parts x 3 sets: (200 x 100 x 15 / 2438.4 x 1219.2) x 6018 = 607.29
    assert Decimal(three.data["applied_charge"]) == Decimal("607")
    assert "× 5 × 3 sets" in three.data["charge_breakdown"][0]["description"]

    slot = egs_factory.slot(eq, egs_factory.future(days=2))
    resp = client.post(
        f"/api/equipments/{eq.pk}/book/",
        _book_body(slot, laser_cut_batch_id=batch_id, input_values={"A": 3}),
        format="json",
    )
    assert resp.status_code in (200, 201), resp.data
    booking = Booking.objects.get(user=student, equipment=eq)
    assert booking.total_charge == Decimal(three.data["applied_charge"])
    assert booking.input_values["A"] == 3 and booking.input_values[QUANTITY_MARKER_KEY] is True
    assert not any(key in booking.input_values for key in RESERVED_KEYS)

    data = BookingSerializer(booking, context={"request": None}).data
    assert data["fabrication_quantity"] == 3
    assert data["fabrication_parts"][0]["job_quantity"] == 3


@pytest.mark.django_db
def test_laser_quantity_defaults_to_1_and_rejects_invalid_values(egs_factory, no_portal_lock):
    eq = laser_equipment(egs_factory)
    student, _sub = funded_student(egs_factory)
    client = egs_factory.client_for(student)
    batch_id = _laser_batch(client, eq, quantity=1)
    calc = f"/api/equipments/{eq.pk}/calculate/?laser_cut_batch_id={batch_id}"
    for bad in ("0", "1.5", "1001"):
        resp = client.get(f"{calc}&A={bad}")
        assert resp.status_code == 400 and QUANTITY_LABEL in resp.data["error"], bad

    slot = egs_factory.slot(eq, egs_factory.future(days=2))
    resp = client.post(f"/api/equipments/{eq.pk}/book/", _book_body(slot, laser_cut_batch_id=batch_id, input_values={}), format="json")
    assert resp.status_code in (200, 201), resp.data
    booking = Booking.objects.get(user=student, equipment=eq)
    assert booking.input_values["A"] == 1 and booking.input_values[QUANTITY_MARKER_KEY] is True


@pytest.mark.django_db
def test_editing_quantity_after_booking_reprices_the_laser_job(egs_factory, no_portal_lock):
    eq = laser_equipment(egs_factory)
    ensure_fabrication_quantity_inputs(eq)
    student, _sub = funded_student(egs_factory)
    client = egs_factory.client_for(student)
    batch_id = _laser_batch(client, eq, quantity=5)
    slot = egs_factory.slot(eq, egs_factory.future(days=2))
    resp = client.post(
        f"/api/equipments/{eq.pk}/book/", _book_body(slot, laser_cut_batch_id=batch_id, input_values={"A": 1}), format="json"
    )
    assert resp.status_code in (200, 201), resp.data
    booking = Booking.objects.get(user=student, equipment=eq)
    assert booking.total_charge == Decimal("202")

    bad = client.patch(f"/api/bookings/{booking.pk}/input-values/", {"input_values": {"A": 0}}, format="json")
    assert bad.status_code == 400
    resp = client.patch(f"/api/bookings/{booking.pk}/input-values/", {"input_values": {"A": 2}}, format="json")
    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    assert booking.input_values["A"] == 2
    assert booking.total_charge == Decimal("405")  # 202.43 x 2 = 404.86
    assert booking.charge_recalculation_pending_amount == Decimal("203.00")


# --------------------------------------------------------------------------- 3D print


@pytest.mark.django_db
def test_print_quantity_multiplies_weight_and_time_and_preview_equals_stored_charge(egs_factory, no_portal_lock):
    eq = print_equipment(egs_factory, hourly_rate="60.00")
    student, _sub = funded_student(egs_factory)
    client = egs_factory.client_for(student)
    batch = _print_batch(eq, student)
    calc = f"/api/equipments/{eq.pk}/calculate/?print_analysis_batch_id={batch.id}"

    one = client.get(calc)
    two = client.get(f"{calc}&A=2")
    assert one.status_code == two.status_code == 200, two.data
    # 11 g x 1.44 = 15.84 + 30 min at 60/h -> 45.84 -> 46
    assert (Decimal(one.data["applied_charge"]), one.data["total_time_minutes"]) == (Decimal("46"), 30)
    # 22 g x 1.44 = 31.68 + 60 min at 60/h -> 91.68 -> 92
    assert (Decimal(two.data["applied_charge"]), two.data["total_time_minutes"]) == (Decimal("92"), 60)
    assert two.data["input_values"][PRINT_WEIGHT_KEY] == 22
    assert two.data["charge_breakdown"][0]["description"] == "gear: 11 g × 1 × 2 sets PLA (FDM) @ 1.44/g"

    slot = egs_factory.slot(eq, egs_factory.future(days=2))
    resp = client.post(
        f"/api/equipments/{eq.pk}/book/",
        _book_body(slot, print_analysis_batch_id=str(batch.id), input_values={"A": 2}),
        format="json",
    )
    assert resp.status_code in (200, 201), resp.data
    booking = Booking.objects.get(user=student, equipment=eq)
    assert booking.total_charge == Decimal(two.data["applied_charge"])
    assert booking.total_time_minutes == 60
    assert booking.input_values["A"] == 2 and booking.input_values["C"] == 60
    assert not any(key in booking.input_values for key in RESERVED_KEYS)


@pytest.mark.django_db
def test_page_loaded_before_quantity_sends_weight_in_a_and_is_priced_as_one_copy(egs_factory):
    eq = print_equipment(egs_factory, hourly_rate="60.00")
    ensure_fabrication_quantity_inputs(eq)  # A now has a 1..1000 limit
    student, _sub = funded_student(egs_factory)
    batch = _print_batch(eq, student)
    resp = egs_factory.client_for(student).get(
        f"/api/equipments/{eq.pk}/calculate/?print_analysis_batch_id={batch.id}&A=1200&B=PLA-FDM&C=30"
    )
    assert resp.status_code == 200, resp.data
    assert (Decimal(resp.data["applied_charge"]), resp.data["total_time_minutes"]) == (Decimal("46"), 30)


def _booked_print(egs_factory, *, quantity, slot_count=2):
    eq = print_equipment(egs_factory, hourly_rate="60.00")
    ensure_fabrication_quantity_inputs(eq)
    owner, _sub = funded_student(egs_factory)
    pla = print_material(eq)
    booking = egs_factory.booking(
        owner,
        eq,
        egs_factory.future(),
        slot_count=slot_count,
        total_charge="0.00",
        input_values={"A": quantity, QUANTITY_MARKER_KEY: True, "B": "PLA-FDM", "C": 30 * quantity},
    )
    part = print_part(eq, owner, pla, weight="10.2", minutes=30, booking=booking)
    booking.print_analysis = part
    booking.save(update_fields=["print_analysis"])
    return eq, owner, booking, part


@pytest.mark.django_db
def test_print_quantity_edit_must_fit_the_booked_slots(egs_factory):
    _eq, owner, booking, _part = _booked_print(egs_factory, quantity=1, slot_count=1)
    resp = egs_factory.client_for(owner).patch(
        f"/api/bookings/{booking.pk}/input-values/", {"input_values": {"A": 3}}, format="json"
    )
    assert resp.status_code == 400
    assert "more than the booked slot" in resp.data["error"]
    booking.refresh_from_db()
    assert booking.input_values["A"] == 1


@pytest.mark.django_db
def test_print_actuals_are_the_total_of_all_copies(egs_factory):
    from iic_booking.equipment.models import EquipmentManager
    from iic_booking.users.models.user_type import UserType
    from iic_booking.users.tests.factories import UserFactory

    eq, _owner, booking, part = _booked_print(egs_factory, quantity=2)
    oic = UserFactory(user_type=UserType.MANAGER, department=egs_factory.department, admin_approved=True)
    EquipmentManager.objects.create(equipment=eq, manager=oic)

    resp = egs_factory.client_for(oic).patch(
        f"/api/bookings/{booking.pk}/print-actuals/",
        {"analysis_id": str(part.id), "actual_weight_grams": 25, "actual_time_minutes": 70},
        format="json",
    )
    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    assert booking.input_values["A"] == 2  # still the quantity
    assert booking.input_values["C"] == 70  # actual time is not multiplied again
    # 25 g x 1.44 = 36.00 + 70 min at 60/h = 70 -> 106
    assert booking.total_charge == Decimal("106.00")


@pytest.mark.django_db
def test_older_print_booking_shows_quantity_1_and_keeps_its_charge(egs_factory):
    eq = print_equipment(egs_factory)
    owner = egs_factory.student()
    booking = egs_factory.booking(
        owner, eq, egs_factory.future(), total_charge="15.00", input_values={"A": 10, "B": "PLA-FDM", "C": 30}
    )
    data = BookingSerializer(booking, context={"request": None}).data
    assert data["input_values"]["A"] == 1
    assert data["fabrication_quantity"] == 1
    assert data["input_fields"][0] == {"field_key": "A", "field_label": QUANTITY_LABEL, "field_type": "NUMERIC"}
    booking.refresh_from_db()
    assert booking.input_values["A"] == 10 and booking.total_charge == Decimal("15.00")

    from iic_booking.equipment.input_display import booking_input_summary_text

    assert booking_input_summary_text(booking).startswith(f"{QUANTITY_LABEL}: 1")


# --------------------------------------------------------------------------- data command and hook


@pytest.mark.django_db
def test_command_adds_quantity_once_for_every_user_type_and_reports_conflicts(egs_factory):
    printer = print_equipment(egs_factory)
    for user_type in ("student", "faculty"):
        DynamicInputField.objects.create(
            equipment=printer, user_type=user_type, field_key="D", field_label="Project", field_type="TEXT"
        )
    laser = laser_equipment(egs_factory)
    conflict = laser_equipment(egs_factory)
    DynamicInputField.objects.create(
        equipment=conflict, user_type="", field_key="A", field_label="No. of Parts", field_type="NUMERIC"
    )
    other = egs_factory.equipment()

    out = StringIO()
    call_command("add_fabrication_quantity_input", stdout=out)
    assert "MODE=dry-run" in out.getvalue()
    assert not DynamicInputField.objects.filter(field_label=QUANTITY_LABEL).exists()

    out = StringIO()
    call_command("add_fabrication_quantity_input", "--apply", stdout=out)
    rows = DynamicInputField.objects.filter(field_key="A", field_label=QUANTITY_LABEL)
    assert sorted(rows.filter(equipment=printer).values_list("user_type", flat=True)) == ["", "faculty", "student"]
    assert list(rows.filter(equipment=laser).values_list("user_type", flat=True)) == [""]
    assert not rows.filter(equipment__in=[conflict, other]).exists()
    row = rows.filter(equipment=printer, user_type="student").get()
    assert (row.field_type, row.is_required, row.default_value, row.help_text) == ("NUMERIC", True, "1", "1\n1000\n1")
    assert DynamicInputField.objects.get(equipment=conflict, field_key="A").field_label == "No. of Parts"
    assert "conflicts=shared:NUMERIC" in out.getvalue()
    assert "conflicts=1" in out.getvalue()

    out = StringIO()
    call_command("add_fabrication_quantity_input", "--apply", stdout=out)
    assert DynamicInputField.objects.filter(field_key="A", field_label=QUANTITY_LABEL).count() == 4
    assert "created=0 already_present=4 conflicts=1" in out.getvalue()


@pytest.mark.django_db
def test_switching_equipment_to_a_fabrication_profile_adds_quantity_once(egs_factory):
    from iic_booking.equipment.serializers import _add_fabrication_quantity_input

    eq = laser_equipment(egs_factory)
    _add_fabrication_quantity_input(eq, EquipmentProfileType.HOUR)
    assert DynamicInputField.objects.filter(equipment=eq, field_key="A", field_label=QUANTITY_LABEL).count() == 1

    DynamicInputField.objects.filter(equipment=eq).delete()
    _add_fabrication_quantity_input(eq, LASER)  # saved again on the same profile: removal is respected
    assert not DynamicInputField.objects.filter(equipment=eq).exists()

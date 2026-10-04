"""IITR Startup gets the IITR Student charges via copy_user_type_charges (idempotent, reversible)."""

from __future__ import annotations

import uuid
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.forms.models import model_to_dict

from iic_booking.equipment import charge_copy
from iic_booking.equipment.calculators import ChargeCalculationEngine, TimeCalculationEngine
from iic_booking.equipment.equipment_group_service import resolve_charge_profile
from iic_booking.equipment.models import (
    ChargeCopyBatch,
    ChargeProfile,
    ChargeProfilePricingProfile as PP,
    DynamicInputField,
    Equipment,
    MultiParamDefinition,
)
from iic_booking.users.models import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

STUDENT = UserType.STUDENT
STARTUP = UserType.STARTUP_INCUBATED_IITR


def _profile(eq, user_type, pricing=PP.STANDARD, **kw):
    values = {
        "profile_type": "SAMPLE",
        "primary_unit_charge": Decimal("987.65"),
        "secondary_unit_charge": Decimal("432.10"),
        "breakpoint": Decimal("5"),
        "time_formula": "A * 30",
        "display_text": "Per sample",
    }
    values.update(kw)
    return ChargeProfile.objects.create(equipment=eq, user_type=user_type, pricing_profile=pricing, **values)


@pytest.fixture
def world():
    tag = uuid.uuid4().hex[:5]
    dept = Department.objects.create(name=f"Dept {tag}", code=f"CC{tag}", equipment_visibility_enabled=True)
    sample = Equipment.objects.create(
        name=f"XRD {tag}", code=f"XRD{tag}", internal_department=dept, user_rating_enabled=False,
        slot_duration_minutes=30,
    )
    _profile(sample, STUDENT)
    _profile(sample, STUDENT, PP.DISCOUNTED, primary_unit_charge=0, secondary_unit_charge=0)
    _profile(sample, STUDENT, PP.PI, primary_unit_charge=Decimal("500"))
    _profile(sample, UserType.FACULTY, primary_unit_charge=Decimal("1200"))
    _profile(sample, UserType.EXTERNAL, primary_unit_charge=Decimal("3000"))
    for key, label in (("A", "Number of samples"), ("B", "Scan range")):
        DynamicInputField.objects.create(
            equipment=sample, user_type=STUDENT, field_key=key, field_label=label, field_type="NUMERIC",
            is_required=True, help_text="1\n50\n1",
        )
    DynamicInputField.objects.create(
        equipment=sample, user_type="", field_key="A", field_label="Samples (legacy)", field_type="NUMERIC"
    )

    multi = Equipment.objects.create(
        name=f"AFM {tag}", code=f"AFM{tag}", internal_department=dept, user_rating_enabled=False,
        slot_duration_minutes=60,
    )
    _profile(multi, STUDENT, profile_type="MULTI_PARAM", primary_unit_charge=0, secondary_unit_charge=0)
    for code, charge in (("S1", "250.00"), ("S2", "450.00")):
        MultiParamDefinition.objects.create(
            equipment=multi, user_type=STUDENT, param_name=code, param_code=code,
            unit_time_minutes=60, unit_charge=Decimal(charge),
        )
    faculty_only = Equipment.objects.create(
        name=f"SEM {tag}", code=f"SEM{tag}", internal_department=dept, user_rating_enabled=False
    )
    _profile(faculty_only, UserType.FACULTY)
    return {"dept": dept, "sample": sample, "multi": multi, "faculty_only": faculty_only}


def _snapshot_other_rows():
    rows = {}
    for model in (ChargeProfile, DynamicInputField, MultiParamDefinition):
        for obj in model.objects.exclude(user_type=STARTUP).order_by("pk"):
            data = model_to_dict(obj)
            rows[(model.__name__, obj.pk)] = (data, getattr(obj, "updated_at", None))
    return rows


def _charge(user, equipment, inputs):
    profile = resolve_charge_profile(user, equipment)
    assert profile is not None
    minutes = TimeCalculationEngine.calculate_time(profile, inputs, equipment.slot_duration_minutes)
    total, _breakdown = ChargeCalculationEngine.calculate_charge(profile, inputs, minutes)
    return profile, total


def test_dry_run_plans_without_writing(world):
    plan = charge_copy.plan_copy()
    summary = charge_copy.summarize(plan)
    assert summary["equipment_with_source_rates"] == 2
    assert summary["charge_profiles"] == 3
    assert summary["charge_profiles_by_variant"] == {"standard": 2, "discounted": 1}
    assert summary["input_fields"] == 2
    assert summary["param_definitions"] == 2
    sample = next(e for e in plan if e["code"] == world["sample"].code)
    assert any("PI rate not copied" in s for s in sample["skipped"])
    assert not ChargeProfile.objects.filter(user_type=STARTUP).exists()
    assert not ChargeCopyBatch.objects.exists()


def test_startup_pricing_equals_student_after_copy(world):
    student = UserFactory(user_type=STUDENT)
    startup = UserFactory(user_type=STARTUP)
    assert resolve_charge_profile(startup, world["sample"]) is None

    batch, _plan = charge_copy.apply_copy()
    assert batch is not None

    for inputs in ({"A": 3, "B": 10}, {"A": 9, "B": 10}):
        student_profile, student_total = _charge(student, world["sample"], inputs)
        startup_profile, startup_total = _charge(startup, world["sample"], inputs)
        assert startup_profile.user_type == STARTUP and student_profile.user_type == STUDENT
        assert startup_total == student_total > 0

    copied = ChargeProfile.objects.get(equipment=world["sample"], user_type=STARTUP, pricing_profile=PP.STANDARD)
    source = ChargeProfile.objects.get(equipment=world["sample"], user_type=STUDENT, pricing_profile=PP.STANDARD)
    for name in ("profile_type", "primary_unit_charge", "secondary_unit_charge", "breakpoint", "time_formula",
                 "charge_formula", "display_text", "is_active", "require_istem_fbr", "show_charge_breakdown"):
        assert getattr(copied, name) == getattr(source, name), name
    assert ChargeProfile.objects.filter(
        equipment=world["sample"], user_type=STARTUP, pricing_profile=PP.DISCOUNTED, primary_unit_charge=0
    ).exists()
    assert not ChargeProfile.objects.filter(user_type=STARTUP, pricing_profile=PP.PI).exists()
    assert not ChargeProfile.objects.filter(equipment=world["faculty_only"], user_type=STARTUP).exists()

    labels = list(
        DynamicInputField.objects.filter(equipment=world["sample"], user_type=STARTUP)
        .order_by("field_key").values_list("field_label", "help_text")
    )
    assert labels == [("Number of samples", "1\n50\n1"), ("Scan range", "1\n50\n1")]
    options = dict(
        MultiParamDefinition.objects.filter(equipment=world["multi"], user_type=STARTUP)
        .values_list("param_code", "unit_charge")
    )
    assert options == {"S1": Decimal("250.00"), "S2": Decimal("450.00")}


def test_copy_is_idempotent_and_leaves_other_rows_untouched(world):
    before = _snapshot_other_rows()
    first, _ = charge_copy.apply_copy()
    counts = {
        model: model.objects.filter(user_type=STARTUP).count()
        for model in (ChargeProfile, DynamicInputField, MultiParamDefinition)
    }
    second, plan = charge_copy.apply_copy()

    assert first is not None and second is None
    assert ChargeCopyBatch.objects.count() == 1
    assert charge_copy.summarize(plan)["charge_profiles"] == 0
    assert {
        model: model.objects.filter(user_type=STARTUP).count()
        for model in (ChargeProfile, DynamicInputField, MultiParamDefinition)
    } == counts
    assert _snapshot_other_rows() == before


def test_existing_startup_rows_are_never_overwritten(world):
    own = _profile(world["sample"], STARTUP, primary_unit_charge=Decimal("111.00"))
    DynamicInputField.objects.create(
        equipment=world["sample"], user_type=STARTUP, field_key="A", field_label="Own field", field_type="NUMERIC"
    )
    plan = charge_copy.plan_copy()
    sample = next(e for e in plan if e["code"] == world["sample"].code)
    assert [cp.pricing_profile for cp in sample["charge_profiles"]] == [PP.DISCOUNTED]
    assert any(f"already exists (id {own.pk})" in s for s in sample["skipped"])
    assert sample["input_fields"] == []

    charge_copy.apply_copy()
    own.refresh_from_db()
    assert own.primary_unit_charge == Decimal("111.00")
    assert list(
        DynamicInputField.objects.filter(equipment=world["sample"], user_type=STARTUP).values_list("field_label", flat=True)
    ) == ["Own field"]


def test_rollback_removes_only_the_batch_rows(world):
    own = _profile(world["faculty_only"], STARTUP)
    before = _snapshot_other_rows()
    batch, _ = charge_copy.apply_copy()

    report = charge_copy.rollback_copy(batch.pk)

    assert report["charge_profiles_deleted"] == 3
    assert report["input_fields_deleted"] == 2
    assert report["param_definitions_deleted"] == 2
    assert list(ChargeProfile.objects.filter(user_type=STARTUP)) == [own]
    assert not DynamicInputField.objects.filter(user_type=STARTUP).exists()
    assert _snapshot_other_rows() == before
    batch.refresh_from_db()
    assert batch.rolled_back_at is not None
    with pytest.raises(ValueError):
        charge_copy.rollback_copy(batch.pk)


def test_command_dry_run_apply_and_rollback_print_no_amounts(world):
    out = StringIO()
    call_command("copy_user_type_charges", stdout=out)
    dry = out.getvalue()
    assert "DRY RUN" in dry and world["sample"].code in dry and "987" not in dry and "432" not in dry
    assert not ChargeProfile.objects.filter(user_type=STARTUP).exists()

    out = StringIO()
    call_command("copy_user_type_charges", "--apply", stdout=out)
    applied = out.getvalue()
    batch = ChargeCopyBatch.objects.get()
    assert f"BATCH {batch.pk} recorded" in applied and "987" not in applied
    assert ChargeProfile.objects.filter(user_type=STARTUP).count() == 3

    out = StringIO()
    call_command("copy_user_type_charges", "--rollback", str(batch.pk), stdout=out)
    assert "ROLLED BACK" in out.getvalue()
    assert not ChargeProfile.objects.filter(user_type=STARTUP).exists()


def test_booking_assistant_treats_iitr_startup_as_internal():
    from iic_booking.research_copilot.services.assistant import daily

    assert STARTUP not in daily.EXTERNAL_TYPES
    assert UserType.EXTERNAL_STARTUP_MSME in daily.EXTERNAL_TYPES

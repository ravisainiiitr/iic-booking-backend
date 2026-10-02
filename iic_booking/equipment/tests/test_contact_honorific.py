"""Per-equipment honorific (Mr. / Mrs. / Ms. / Miss / Dr. / Prof.) for Officers in Charge and Lab Operators."""

from __future__ import annotations

import pytest
from django.contrib import admin

from iic_booking.equipment.admin import EquipmentManagerInline, EquipmentOperatorInline
from iic_booking.equipment.booking_events import apply_lab_visit_details_to_context
from iic_booking.equipment.models import ContactHonorific, Equipment, EquipmentManager, EquipmentOperator
from iic_booking.equipment.serializers import (
    EquipmentManagerSerializer,
    EquipmentManagerWriteSerializer,
    EquipmentOperatorSerializer,
    _create_related,
    _sync_related,
)
from iic_booking.research_copilot.services.assistant.info import contacts as assistant_contacts
from iic_booking.users.display import compose_honorific_name, name_with_honorific, strip_name_honorifics
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Dr. Shriniwas Yadav", "Shriniwas Yadav"),
        ("dr shriniwas yadav", "shriniwas yadav"),
        ("Prof. Dr. Kalpana", "Kalpana"),
        ("Professor Ravi Saini", "Ravi Saini"),
        ("MRS. Asha Rani", "Asha Rani"),
        ("Miss Neha", "Neha"),
        ("Ms Neha", "Neha"),
        ("Mr.Kamal Singh", "Kamal Singh"),
        ("Drishti Sharma", "Drishti Sharma"),
        ("Mrinal Sen", "Mrinal Sen"),
        ("  Kamal   Singh Gotyan ", "Kamal Singh Gotyan"),
        ("Dr.", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_strip_name_honorifics(name, expected):
    assert strip_name_honorifics(name) == expected


def test_compose_honorific_name_cases():
    assert compose_honorific_name("Dr. Shriniwas Yadav", "Prof.") == "Prof. Shriniwas Yadav"
    assert compose_honorific_name("Dr. Shriniwas Yadav", "Dr.") == "Dr. Shriniwas Yadav"
    assert compose_honorific_name("Neha Verma", "Ms.") == "Ms. Neha Verma"
    assert compose_honorific_name("Prof. Kalpana", "Miss") == "Miss Kalpana"
    assert compose_honorific_name("Dr. Shriniwas Yadav", "") == ""
    assert compose_honorific_name("Dr.", "Prof.") == ""


class _U:
    def __init__(self, name, email="x@iitr.ac.in", user_type=None):
        self.name = name
        self.email = email
        self.user_type = user_type


def test_name_with_honorific_blank_keeps_default():
    user = _U("Dr. Shriniwas Yadav")
    assert name_with_honorific(user, "", default="Dr. Shriniwas Yadav (today)") == "Dr. Shriniwas Yadav (today)"
    assert name_with_honorific(user, None, default="kept") == "kept"
    assert name_with_honorific(None, "Prof.", default="nobody") == "nobody"
    assert name_with_honorific(_U("", email="a@b.c"), "Dr.", default="a@b.c") == "a@b.c"


def test_choices_cover_requested_titles():
    assert [value for value, _ in ContactHonorific.choices] == ["", "Mr.", "Mrs.", "Ms.", "Miss", "Dr.", "Prof."]


def test_admin_inlines_show_honorific_right_after_person():
    assert EquipmentManagerInline.fields[:3] == ["manager", "honorific", "office_address"]
    assert EquipmentOperatorInline.fields[:2] == ["operator", "honorific"]
    assert EquipmentOperatorInline.fields.index("honorific") < EquipmentOperatorInline.fields.index("office_address")
    field = EquipmentManager._meta.get_field("honorific")
    assert field.blank and field.default == ""
    assert "Leave blank to use the automatic title" in str(field.help_text)


@pytest.mark.django_db
def test_admin_inline_form_renders_honorific(rf, admin_user):
    request = rf.get("/")
    request.user = admin_user
    inline = EquipmentManagerInline(Equipment, admin.site)
    formset_class = inline.get_formset(request)
    assert "honorific" in formset_class.form.base_fields
    op_inline = EquipmentOperatorInline(Equipment, admin.site)
    assert "honorific" in op_inline.get_formset(request).form.base_fields


@pytest.mark.django_db
def test_serializers_expose_honorific_and_compose_names(egs_factory):
    eq = egs_factory.equipment()
    oic = UserFactory(user_type=UserType.MANAGER, name="Dr. Shriniwas Yadav")
    operator = UserFactory(user_type=UserType.OPERATOR, name="Kalpana")
    mgr_row = EquipmentManager.objects.create(equipment=eq, manager=oic, honorific="Prof.")
    op_row = EquipmentOperator.objects.create(equipment=eq, operator=operator, honorific="Ms.")

    mgr_data = EquipmentManagerSerializer(mgr_row).data
    assert mgr_data["honorific"] == "Prof."
    assert mgr_data["manager_name"] == "Prof. Shriniwas Yadav"
    op_data = EquipmentOperatorSerializer(op_row, context={}).data
    assert op_data["honorific"] == "Ms."
    assert op_data["operator_name"] == "Ms. Kalpana"


@pytest.mark.django_db
def test_blank_honorific_keeps_current_names(egs_factory):
    eq = egs_factory.equipment()
    oic = UserFactory(user_type=UserType.MANAGER, name="Dr. Shriniwas Yadav")
    mgr_row = EquipmentManager.objects.create(equipment=eq, manager=oic)
    data = EquipmentManagerSerializer(mgr_row).data
    assert data["honorific"] == ""
    assert data["manager_name"] == oic.get_display_name() == "Dr. Shriniwas Yadav"


@pytest.mark.django_db
def test_explicit_honorific_overrides_faculty_auto_prefix(egs_factory):
    eq = egs_factory.equipment()
    faculty = UserFactory(user_type=UserType.FACULTY, name="Ravi Saini")
    row = EquipmentManager.objects.create(equipment=eq, manager=faculty)
    assert EquipmentManagerSerializer(row).data["manager_name"] == "Prof. Ravi Saini"
    row.honorific = "Dr."
    row.save()
    assert EquipmentManagerSerializer(row).data["manager_name"] == "Dr. Ravi Saini"


@pytest.mark.django_db
def test_write_paths_store_and_preserve_honorific(egs_factory):
    eq = egs_factory.equipment()
    oic = UserFactory(user_type=UserType.MANAGER)
    operator = UserFactory(user_type=UserType.OPERATOR)
    _create_related(
        eq,
        {
            "equipment_managers": [{"manager": oic, "honorific": "Dr."}],
            "equipment_operators": [{"operator": operator, "role": "PRIMARY", "honorific": "Mr."}],
        },
    )
    assert EquipmentManager.objects.get(equipment=eq).honorific == "Dr."
    assert EquipmentOperator.objects.get(equipment=eq).honorific == "Mr."

    _sync_related(eq, {"equipment_managers": [{"manager": oic, "office_address": "Room 1"}]})
    assert EquipmentManager.objects.get(equipment=eq).honorific == "Dr."

    _sync_related(eq, {"equipment_managers": [{"manager": oic, "honorific": ""}]})
    assert EquipmentManager.objects.get(equipment=eq).honorific == ""


@pytest.mark.django_db
def test_write_serializer_validates_honorific():
    bad = EquipmentManagerWriteSerializer(data={"manager": 1, "honorific": "Sir"})
    assert not bad.is_valid()
    assert "honorific" in bad.errors
    blank = EquipmentManagerWriteSerializer(data={"manager": 1, "honorific": ""})
    blank.is_valid()
    assert "honorific" not in blank.errors


@pytest.mark.django_db
def test_email_context_and_assistant_use_honorific(egs_factory):
    eq = egs_factory.equipment()
    oic = UserFactory(user_type=UserType.MANAGER, name="Dr. Shriniwas Yadav", email="oic@iitr.ac.in")
    operator = UserFactory(user_type=UserType.OPERATOR, name="Kalpana", email="op@iitr.ac.in")
    EquipmentManager.objects.create(equipment=eq, manager=oic, honorific="Prof.")
    EquipmentOperator.objects.create(equipment=eq, operator=operator, honorific="Dr.")

    ctx = apply_lab_visit_details_to_context({}, eq)
    assert ctx["oic_contact"].splitlines()[0] == "Prof. Shriniwas Yadav"
    assert ctx["lab_incharge_contact"].splitlines()[0] == "Dr. Kalpana"

    names = [c["name"] for c in assistant_contacts(eq)]
    assert names == ["Prof. Shriniwas Yadav", "Dr. Kalpana"]

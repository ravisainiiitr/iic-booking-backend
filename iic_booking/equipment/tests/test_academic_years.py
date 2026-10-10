import uuid
from datetime import date, timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment import academic_years
from iic_booking.equipment.models import Equipment, EquipmentManager, EquipmentOperatingTACall, Semester
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def _user(user_type, **kwargs):
    return UserFactory(
        user_type=user_type,
        email=f"ay{uuid.uuid4().hex[:10]}@iitr.ac.in",
        email_verified=True,
        admin_approved=True,
        **kwargs,
    )


def _client(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


@pytest.fixture
def oic_equipment():
    tag = uuid.uuid4().hex[:4].upper()
    equipment = Equipment.objects.create(name=f"AY EQ {tag}", code=f"AYEQ{tag}", status="ACTIVE")
    oic = _user(UserType.MANAGER)
    EquipmentManager.objects.create(equipment=equipment, manager=oic)
    return oic, equipment


def _payload(equipment, **extra):
    return {
        "equipment_id": equipment.equipment_id,
        "number_of_operators_required": 2,
        "nomination_deadline": (timezone.localdate() + timedelta(days=10)).isoformat(),
        **extra,
    }


def test_labels_follow_july_to_june_year():
    assert academic_years.current_label(date(2026, 6, 30)) == "2025-26"
    assert academic_years.current_label(date(2026, 7, 1)) == "2026-27"
    assert academic_years.current_label(date(2099, 12, 1)) == "2099-00"
    assert academic_years.bounds("2026-27") == (date(2026, 7, 1), date(2027, 6, 30))
    with pytest.raises(academic_years.AcademicYearError):
        academic_years.parse_label("2026-28")


def test_semesters_endpoint_offers_academic_years_without_any_semester_rows():
    Semester.objects.all().delete()
    res = _client(_user(UserType.MANAGER)).get("/api/semesters/?active_only=1")
    assert res.status_code == 200
    assert res.data["semesters"] == []
    labels = [o["label"] for o in res.data["academic_years"]]
    current = academic_years.current_label()
    assert labels[:2] == [current, academic_years.label_for_start_year(academic_years.start_year_for(timezone.localdate()) + 1)]
    assert res.data["academic_years"][0]["is_current"] is True
    assert all(o["semester_id"] is None and o["available"] for o in res.data["academic_years"])


def test_existing_active_semester_is_mapped_to_its_academic_year():
    Semester.objects.all().delete()
    current = academic_years.current_label()
    start, end = academic_years.bounds(current)
    sem = Semester.objects.create(name=f"{current} Odd", code=f"{current}-ODD", start_date=start, end_date=end)
    res = _client(_user(UserType.MANAGER)).get("/api/semesters/?active_only=1")
    by_label = {o["label"]: o for o in res.data["academic_years"]}
    assert by_label[current]["semester_id"] == sem.id


def test_create_call_with_academic_year_creates_full_year_semester(oic_equipment):
    Semester.objects.all().delete()
    oic, equipment = oic_equipment
    label = academic_years.current_label()
    res = _client(oic).post("/api/ta-nomination-calls/", _payload(equipment, academic_year=label), format="json")
    assert res.status_code == 201, res.data
    call = EquipmentOperatingTACall.objects.get(pk=res.data["ta_call"]["id"])
    assert call.semester.code == f"AY-{label}"
    assert call.semester.is_active
    assert res.data["ta_call"]["academic_year_name"] == label
    again = _client(oic).post("/api/ta-nomination-calls/", _payload(equipment, academic_year=label), format="json")
    assert again.status_code == 201
    assert Semester.objects.filter(code=f"AY-{label}").count() == 1


def test_create_call_reuses_matching_semester_and_rejects_closed_or_far_years(oic_equipment):
    Semester.objects.all().delete()
    oic, equipment = oic_equipment
    label = academic_years.current_label()
    start, end = academic_years.bounds(label)
    sem = Semester.objects.create(name=f"{label} Even", code=f"{label}-EVEN", start_date=start, end_date=end)
    res = _client(oic).post("/api/ta-nomination-calls/", _payload(equipment, academic_year=label), format="json")
    assert res.status_code == 201
    assert res.data["ta_call"]["semester_id"] == sem.id

    sem.is_active = False
    sem.save()
    closed = _client(oic).post("/api/ta-nomination-calls/", _payload(equipment, academic_year=label), format="json")
    assert closed.status_code == 400
    assert "closed" in closed.data["error"]

    far = _client(oic).post("/api/ta-nomination-calls/", _payload(equipment, academic_year="2001-02"), format="json")
    assert far.status_code == 400


def test_legacy_semester_id_still_works(oic_equipment):
    oic, equipment = oic_equipment
    sem = Semester.objects.create(name="Legacy", code=f"LEG{uuid.uuid4().hex[:4]}", start_date=date(2026, 7, 1), end_date=date(2027, 6, 30))
    res = _client(oic).post("/api/ta-nomination-calls/", _payload(equipment, semester_id=sem.id), format="json")
    assert res.status_code == 201
    assert res.data["ta_call"]["semester_id"] == sem.id

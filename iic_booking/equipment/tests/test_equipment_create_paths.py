"""Creating new equipment from the portal Admin form and from Django admin."""

from __future__ import annotations

import uuid

import pytest
from rest_framework.test import APIClient

from iic_booking.equipment.models import ChargeProfile, Equipment, SlotMaster
from iic_booking.users.models.department import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def _admin():
    return UserFactory(user_type=UserType.ADMIN, is_staff=True, is_superuser=True, admin_approved=True)


def _dept():
    tag = uuid.uuid4().hex[:6].upper()
    return Department.objects.create(
        name=f"CRE-{tag}", code=f"CR{tag[:4]}", department_type="internal",
        equipment_booking_enabled=True, equipment_visibility_enabled=True,
    )


def _portal_payload(code, dept):
    """Shape of EquipmentForm.handleSubmit for a brand-new equipment with one charge row and slot."""
    return {
        "name": "New Instrument",
        "code": code,
        "description": None,
        "important_instruction": None,
        "make": "",
        "show_make_on_card": False,
        "model_information": "",
        "show_model_on_card": False,
        "booking_email_extra_text": "",
        "completion_email_extra_text": "",
        "print_3d_stl_notification_email": "",
        "istem_portal_url": "",
        "istem_fbr_status_url": "",
        "status": "ACTIVE",
        "location": None,
        "office_address": "",
        "alternate_phone_number": "",
        "latitude": None,
        "longitude": None,
        "google_maps_url": None,
        "profile_type": "SAMPLE",
        "category": None,
        "equipment_group": None,
        "parent_equipment": None,
        "enable_multi_mode": False,
        "internal_department": dept.pk,
        "visibility_group": None,
        "slot_duration_minutes": 60,
        "slot_tolerance_minutes": 0,
        "slots_per_day": 8,
        "reschedule_hours_threshold": 24,
        "results_base_location": "D:\\Results",
        "split_booking_enabled": False,
        "auto_slot_selection_default": None,
        "weekly_view_display": "TIME",
        "weekly_view_time_from": None,
        "weekly_view_time_to": None,
        "weekly_view_max_rows": None,
        "weekly_view_default_days": None,
        "slot_window_reference_weekday": None,
        "slot_window_reference_time": None,
        "external_slot_quota_percent": 0,
        "waitlist_queue_depth": 0,
        "max_urgent_requests": None,
        "max_rush_relief_requests_per_week": None,
        "max_surcharge_urgent_requests_per_week": None,
        "booking_not_utilize_window_hours": 24,
        "skip_quota_check": False,
        "enable_charge_recalculation": False,
        "user_rating_enabled": True,
        "sample_preparation_by_user": False,
        "urgent_peak_window_minutes": None,
        "operator_absent_disruption_after_booking_end_hours": None,
        "operator_unavailable_after_booking_end_hours": 24,
        "show_lifecycle_countdowns": True,
        "sample_submission_lead_hours": 24,
        "atmosphere_sensitive_sample_enabled": False,
        "sample_collect_deadline_hours": 72,
        "repeat_sample_request_days": None,
        "repeat_sample_disclaimer": "",
        "enable_remote_analysis": False,
        "remote_analysis_enabled_from_status": "COMPLETED",
        "analysis_workspace_retention_days": 90,
        "analysis_session_limit": 5,
        "analysis_access_duration": 72,
        "analysis_auto_archive": True,
        "analysis_profile": "",
        "analysis_requires_sample_acceptance": False,
        "analysis_requires_experiment_completion": True,
        "analysis_notes": "",
        "equipment_managers": [],
        "equipment_operators": [],
        "equipment_pis": [],
        "equipment_specifications": [],
        "equipment_publications": [],
        "equipment_accessories": [],
        "equipment_additional_accessories": [],
        "slot_masters": [{"slot_number": 1, "open_time": "09:00", "close_time": "10:00", "is_active": True}],
        "charge_profiles": [
            {
                "user_type": "student",
                "profile_type": "SAMPLE",
                "primary_unit_charge": "100.00",
                "secondary_unit_charge": "0.00",
                "breakpoint": None,
                "time_formula": "A * 30",
                "charge_formula": "",
                "display_text": "",
                "is_active": True,
            }
        ],
        "pi_charge_profiles": [],
        "input_fields": [],
        "print_materials": [],
        "param_definitions": [],
    }


def test_portal_admin_form_creates_equipment():
    dept = _dept()
    client = APIClient()
    client.force_authenticate(user=_admin())
    code = f"NEW{uuid.uuid4().hex[:5].upper()}"
    res = client.post("/api/admin/equipment/", _portal_payload(code, dept), format="json")
    assert res.status_code == 201, getattr(res, "data", res.content[:2000])
    eq = Equipment.objects.get(code=code)
    assert ChargeProfile.objects.filter(equipment=eq, pricing_profile="standard").count() == 1
    assert SlotMaster.objects.filter(equipment=eq).count() == 1
    assert eq.operator_absent_disruption_after_booking_end_hours == 48


def test_portal_admin_form_creates_equipment_with_staff_pi_and_inputs():
    dept = _dept()
    oic = UserFactory(user_type=UserType.MANAGER, department=dept, admin_approved=True)
    operator = UserFactory(user_type=UserType.OPERATOR, department=dept, admin_approved=True)
    pi = UserFactory(user_type=UserType.FACULTY, admin_approved=True)
    client = APIClient()
    client.force_authenticate(user=_admin())
    code = f"NEW{uuid.uuid4().hex[:5].upper()}"
    payload = _portal_payload(code, dept)
    payload["equipment_managers"] = [{"manager": oic.pk}]
    payload["equipment_operators"] = [{"operator": operator.pk, "role": "PRIMARY"}]
    payload["equipment_pis"] = [{"faculty": pi.pk, "is_active": True}]
    payload["input_fields"] = [
        {"user_type": "", "field_key": "A", "field_label": "Number of samples", "field_type": "NUMBER",
         "is_required": True, "default_value": "", "options": [], "help_text": ""},
    ]
    payload["pi_charge_profiles"] = [
        {"user_type": "student", "profile_type": "SAMPLE", "primary_unit_charge": "50.00",
         "secondary_unit_charge": "0.00", "breakpoint": None, "time_formula": "A * 30",
         "charge_formula": "", "display_text": "", "is_active": True},
    ]
    res = client.post("/api/admin/equipment/", payload, format="json")
    assert res.status_code == 201, getattr(res, "data", res.content[:2000])
    eq = Equipment.objects.get(code=code)
    assert eq.equipment_managers.filter(manager=oic).exists()
    assert ChargeProfile.objects.filter(equipment=eq, pricing_profile="pi").count() == 1


def test_portal_admin_form_update_null_keeps_existing_value():
    dept = _dept()
    client = APIClient()
    client.force_authenticate(user=_admin())
    code = f"NEW{uuid.uuid4().hex[:5].upper()}"
    payload = _portal_payload(code, dept)
    payload["operator_absent_disruption_after_booking_end_hours"] = 12
    assert client.post("/api/admin/equipment/", payload, format="json").status_code == 201
    eq = Equipment.objects.get(code=code)
    res = client.patch(
        f"/api/admin/equipment/{eq.pk}/",
        {"operator_absent_disruption_after_booking_end_hours": None},
        format="json",
    )
    assert res.status_code == 200, getattr(res, "data", res.content[:2000])
    eq.refresh_from_db()
    assert eq.operator_absent_disruption_after_booking_end_hours == 12


def _admin_add_post_data(code, dept):
    """Minimal Django admin add-form POST: main fields plus empty management forms for every inline."""
    from django.contrib.admin.sites import site
    from django.test import RequestFactory

    admin_user = _admin()
    ma = site._registry[Equipment]
    req = RequestFactory().get("/admin/equipment/equipment/add/")
    req.user = admin_user
    data = {}
    form_cls = ma.get_form(req, None)
    form = form_cls()
    for name, field in form.fields.items():
        initial = form.get_initial_for_field(field, name)
        if initial is None or initial == "":
            continue
        if isinstance(initial, bool):
            if initial:
                data[name] = "on"
            continue
        data[name] = getattr(initial, "pk", initial)
    data.update({
        "name": "Admin Created",
        "code": code,
        "internal_department": dept.pk,
        "profile_type": "SAMPLE",
        "status": "ACTIVE",
        "slot_duration_minutes": 60,
        "slots_per_day": 8,
    })
    for formset, _inline in ma.get_formsets_with_inlines(req, None):
        prefix = formset.get_default_prefix()
        data[f"{prefix}-TOTAL_FORMS"] = "0"
        data[f"{prefix}-INITIAL_FORMS"] = "0"
        data[f"{prefix}-MIN_NUM_FORMS"] = "0"
        data[f"{prefix}-MAX_NUM_FORMS"] = "1000"
    return admin_user, data


def test_django_admin_add_creates_equipment():
    dept = _dept()
    code = f"ADM{uuid.uuid4().hex[:5].upper()}"
    admin_user, data = _admin_add_post_data(code, dept)
    res = _admin_post(admin_user, "/admin/equipment/equipment/add/", data, lambda ma, req: ma.add_view(req))
    _assert_admin_saved(res)
    assert Equipment.objects.filter(code=code).exists()


def _add_inline_rows(data, prefix_of, rows_by_model):
    for model, rows in rows_by_model.items():
        prefix = prefix_of[model]
        data[f"{prefix}-TOTAL_FORMS"] = str(len(rows))
        for i, row in enumerate(rows):
            for key, value in row.items():
                data[f"{prefix}-{i}-{key}"] = value


def _inline_prefixes(user):
    from django.contrib.admin.sites import site
    from django.test import RequestFactory

    ma = site._registry[Equipment]
    req = RequestFactory().get("/admin/equipment/equipment/add/")
    req.user = user
    return {inline.model: fs.get_default_prefix() for fs, inline in ma.get_formsets_with_inlines(req, None)}


def test_django_admin_add_with_charge_profiles_and_slots():
    dept = _dept()
    code = f"ADM{uuid.uuid4().hex[:5].upper()}"
    admin_user, data = _admin_add_post_data(code, dept)
    charge = {
        "profile_type": "SAMPLE", "is_active": "on", "pricing_profile": "standard",
        "show_charge_breakdown": "on", "primary_unit_charge": "100.00", "secondary_unit_charge": "0.00",
        "breakpoint": "", "time_formula": "A * 30", "charge_formula": "", "display_text": "",
    }
    _add_inline_rows(data, _inline_prefixes(admin_user), {
        ChargeProfile: [
            {**charge, "user_type": "student"},
            {**charge, "user_type": "__pi__:faculty", "primary_unit_charge": "40.00"},
        ],
        SlotMaster: [
            {"slot_number": "1", "slot_name": "Morning", "open_time": "09:00", "close_time": "10:00", "is_active": "on"},
        ],
    })
    res = _admin_post(admin_user, "/admin/equipment/equipment/add/", data, lambda ma, req: ma.add_view(req))
    _assert_admin_saved(res)
    eq = Equipment.objects.get(code=code)
    assert ChargeProfile.objects.filter(equipment=eq, user_type="faculty", pricing_profile="pi").exists()
    assert SlotMaster.objects.filter(equipment=eq).count() == 1


def _source_equipment_with_config():
    from iic_booking.equipment.models import DynamicInputField, EquipmentManager, EquipmentPI

    dept = _dept()
    oic = UserFactory(user_type=UserType.MANAGER, department=dept, admin_approved=True)
    pi = UserFactory(user_type=UserType.FACULTY, admin_approved=True)
    src = Equipment.objects.create(
        name="Source XRD", code=f"SRC{uuid.uuid4().hex[:5].upper()}", internal_department=dept,
        profile_type="SAMPLE", status="ACTIVE", asset_serial_number="SN-1", dsa_hostname="lab-pc-1",
        operator_absent_disruption_after_booking_end_hours=36,
    )
    EquipmentManager.objects.create(equipment=src, manager=oic)
    EquipmentPI.objects.create(equipment=src, faculty=pi)
    for pp in ("standard", "discounted", "pi"):
        ChargeProfile.objects.create(
            equipment=src, user_type="student", pricing_profile=pp, profile_type="SAMPLE",
            primary_unit_charge="100.00", secondary_unit_charge="0.00", time_formula="A * 30",
        )
    SlotMaster.objects.create(equipment=src, slot_number=1, open_time="09:00", close_time="10:00")
    DynamicInputField.objects.create(equipment=src, field_key="A", field_label="Samples", field_type="NUMBER")
    return src


def test_duplicate_equipment_copies_configuration_only():
    from iic_booking.equipment.duplicate import duplicate_equipment
    from iic_booking.equipment.models import DailySlot, DynamicInputField, EquipmentManager, EquipmentPI

    src = _source_equipment_with_config()
    new, warnings = duplicate_equipment(src)
    assert warnings == []
    assert new.pk != src.pk
    assert new.code == f"{src.code}-COPY"
    assert new.name == "Source XRD (Copy)"
    assert new.status == "INACTIVE"
    assert new.internal_department_id == src.internal_department_id
    assert new.operator_absent_disruption_after_booking_end_hours == 36
    assert new.asset_serial_number in ("", None)
    assert new.dsa_hostname in ("", None)
    assert EquipmentManager.objects.filter(equipment=new).count() == 1
    assert EquipmentPI.objects.filter(equipment=new).count() == 1
    assert ChargeProfile.objects.filter(equipment=new).count() == 3
    assert SlotMaster.objects.filter(equipment=new).count() == 1
    assert DynamicInputField.objects.filter(equipment=new).count() == 1
    assert not DailySlot.objects.filter(slot_master__equipment=new).exists()
    assert ChargeProfile.objects.filter(equipment=src).count() == 3

    second, _ = duplicate_equipment(src)
    assert second.code == f"{src.code}-COPY2"


def test_duplicate_equipment_gives_copy_its_own_image(monkeypatch):
    from django.core.files.base import ContentFile
    from django.core.files.storage import InMemoryStorage

    from iic_booking.equipment.duplicate import duplicate_equipment

    storage = InMemoryStorage()
    monkeypatch.setattr(Equipment._meta.get_field("image"), "storage", storage)
    src = _source_equipment_with_config()
    src.image.save("photo.jpg", ContentFile(b"jpeg-bytes"), save=True)

    new, warnings = duplicate_equipment(src)
    new.refresh_from_db()
    assert warnings == []
    assert new.image.name and new.image.name != src.image.name
    assert storage.open(new.image.name).read() == b"jpeg-bytes"

    no_image, _ = duplicate_equipment(src, copy_image=False)
    no_image.refresh_from_db()
    assert not no_image.image


def test_duplicate_equipment_rejects_existing_code():
    from iic_booking.equipment.duplicate import duplicate_equipment

    src = _source_equipment_with_config()
    with pytest.raises(ValueError):
        duplicate_equipment(src, code=src.code.lower())


def _admin_get(user, path, call):
    from django.contrib.admin.sites import site
    from django.contrib.messages.storage.fallback import FallbackStorage
    from django.contrib.sessions.backends.db import SessionStore
    from django.test import RequestFactory

    req = RequestFactory().get(path)
    req.user = user
    req.session = SessionStore()
    req._messages = FallbackStorage(req)
    return call(site._registry[Equipment], req)


def test_admin_change_page_has_duplicate_button_and_duplicate_view_works():
    src = _source_equipment_with_config()
    admin_user = _admin()
    change = _admin_get(admin_user, f"/admin/equipment/equipment/{src.pk}/change/",
                        lambda ma, req: ma.change_view(req, str(src.pk)))
    change.render()
    assert f"/{src.pk}/duplicate/" in change.content.decode()

    page = _admin_get(admin_user, f"/admin/equipment/equipment/{src.pk}/duplicate/",
                      lambda ma, req: ma.duplicate_view(req, str(src.pk)))
    page.render()
    assert f"{src.code}-COPY" in page.content.decode()

    res = _admin_post(
        admin_user, f"/admin/equipment/equipment/{src.pk}/duplicate/",
        {"code": "XRD-NEW-2", "name": "XRD Unit 2"},
        lambda ma, req: ma.duplicate_view(req, str(src.pk)),
    )
    new = Equipment.objects.get(code="XRD-NEW-2")
    assert res.status_code == 302
    assert res["Location"].endswith(f"/{new.pk}/change/")
    assert new.name == "XRD Unit 2"
    assert ChargeProfile.objects.filter(equipment=new).count() == 3


def test_admin_duplicate_action_redirects_to_single_copy():
    src = _source_equipment_with_config()
    admin_user = _admin()
    res = _admin_post(
        admin_user, "/admin/equipment/equipment/", {},
        lambda ma, req: ma.duplicate_selected(req, Equipment.objects.filter(pk=src.pk)),
    )
    new = Equipment.objects.get(code=f"{src.code}-COPY")
    assert res.status_code == 302
    assert res["Location"].endswith(f"/{new.pk}/change/")


def test_admin_duplicate_view_shows_database_error_instead_of_500(monkeypatch):
    from django.db import IntegrityError

    import iic_booking.equipment.duplicate as duplicate_module

    def fail(*args, **kwargs):
        raise IntegrityError('null value in column "x" violates not-null constraint')

    monkeypatch.setattr(duplicate_module, "duplicate_equipment", fail)
    src = _source_equipment_with_config()
    before = Equipment.objects.count()
    res = _admin_post(
        _admin(), f"/admin/equipment/equipment/{src.pk}/duplicate/",
        {"code": "XRD-FAIL-1", "name": "XRD Fail"},
        lambda ma, req: ma.duplicate_view(req, str(src.pk)),
    )
    res.render()
    assert res.status_code == 200
    assert "could not be saved" in res.content.decode()
    assert Equipment.objects.count() == before


def _admin_post(user, path, data, call):
    from django.contrib.admin.sites import site
    from django.contrib.messages.storage.fallback import FallbackStorage
    from django.contrib.sessions.backends.db import SessionStore
    from django.test import RequestFactory

    req = RequestFactory().post(path, data)
    req.user = user
    req.session = SessionStore()
    req._messages = FallbackStorage(req)
    req._dont_enforce_csrf_checks = True
    return call(site._registry[Equipment], req)


def _assert_admin_saved(res):
    if res.status_code == 200:
        errors = {}
        ctx = getattr(res, "context_data", None) or {}
        if ctx.get("adminform"):
            errors["form"] = ctx["adminform"].form.errors
        for fs in ctx.get("inline_admin_formsets", []) or []:
            if fs.formset.errors or fs.formset.non_form_errors():
                errors[fs.formset.prefix] = [fs.formset.errors, fs.formset.non_form_errors()]
        pytest.fail(f"admin form re-rendered with errors: {errors}")
    assert res.status_code == 302, res.status_code

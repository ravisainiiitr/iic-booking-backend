"""Disruption transparency: who started / ended (role), public slot hover, expected recovery, procurement request."""

from __future__ import annotations

from datetime import datetime, timedelta
from io import StringIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.utils import timezone

from iic_booking.equipment.disruption_service import (
    RECOVERY_DELAYED_TEXT,
    RECOVERY_UNKNOWN_TEXT,
    public_equipment_notice,
    recovery_text,
)
from iic_booking.equipment.models import (
    DailySlot,
    DisruptionEvent,
    EquipmentStatus,
    EquipmentTemporaryOIC,
    SlotStatus,
)
from iic_booking.equipment.serializers import EquipmentListSerializer
from iic_booking.equipment.tests.test_disruption_log import (
    LIST_URL,
    _bulk,
    _client,
    _department,
    _equipment,
    _future_weekday,
    _oic_for,
    _slots,
    _user,
)
from iic_booking.users.models.user_type import UserType

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _no_holidays():
    from iic_booking.equipment.models import Holiday

    Holiday.objects.all().delete()


# --- Started by / Ended by -------------------------------------------------------------------------------


def test_started_and_ended_by_are_recorded_with_role_at_the_time():
    eq = _equipment()
    oic = _oic_for(eq)
    temp = _user(UserType.MANAGER)
    EquipmentTemporaryOIC.objects.create(
        equipment=eq, primary_oic=oic, temporary_oic=temp, resume_at=timezone.now() + timedelta(days=2)
    )
    s9, s10, *_ = _slots(eq, _future_weekday())
    assert _bulk(oic, eq, status="UNDER_MAINTENANCE", slot_ids=[s9.pk, s10.pk]).status_code == 200
    event = DisruptionEvent.objects.get(equipment=eq)
    assert (event.started_by_id, event.started_by_role) == (oic.pk, "OIC")

    assert _bulk(temp, eq, status="AVAILABLE", slot_ids=[s9.pk, s10.pk]).status_code == 200
    event.refresh_from_db()
    assert (event.ended_by_id, event.ended_by_role) == (temp.pk, "TEMP_OIC")

    row = _client(oic).get(LIST_URL).data["results"][0]
    assert row["started_by_role_display"] == "OIC" and row["ended_by_role_display"] == "Temp OIC"
    assert row["started_at"] and row["ended_at"]

    admin = _user(UserType.ADMIN)
    _bulk(admin, eq, status="OPERATOR_ABSENT", slot_ids=[s9.pk])
    assert DisruptionEvent.objects.get(equipment=eq, disruption_type="OPERATOR_ABSENT").started_by_role == "MAIN_ADMIN"


def test_backfill_derives_started_by_from_slot_change_log_without_printing_names():
    eq = _equipment()
    oic = _oic_for(eq)
    s9, *_ = _slots(eq, _future_weekday())
    _bulk(oic, eq, status="OPERATOR_ABSENT", slot_ids=[s9.pk])
    event = DisruptionEvent.objects.get(equipment=eq)
    DisruptionEvent.objects.filter(pk=event.pk).update(started_by=None, started_by_role="")
    whole = DisruptionEvent.objects.create(
        equipment=eq, disruption_type="UNDER_MAINTENANCE", scope="EQUIPMENT", start_at=timezone.now()
    )

    out = StringIO()
    call_command("backfill_disruption_people", stdout=out)
    assert "mode=DRY RUN" in out.getvalue() and "started_by_derived=1" in out.getvalue()
    assert DisruptionEvent.objects.get(pk=event.pk).started_by_id is None

    out = StringIO()
    call_command("backfill_disruption_people", "--apply", stdout=out)
    text = out.getvalue()
    event.refresh_from_db()
    assert (event.started_by_id, event.started_by_role) == (oic.pk, "OIC")
    assert DisruptionEvent.objects.get(pk=whole.pk).started_by_id is None
    assert "started_by_not_derivable=1" in text
    assert oic.email not in text and (oic.name or "@@") not in text


# --- Public hover -----------------------------------------------------------------------------------------


def _slot_rows(user, eq, day):
    url = f"/api/equipments/{eq.pk}/slots/?start_date={day}&end_date={day}"
    res = (_client(user) if user else _client(None)).get(url)
    return res, {r["id"]: r for r in (res.data.get("slots") or [])} if res.status_code == 200 else {}


def test_public_slot_hover_shows_reason_and_recovery_but_no_staff_details():
    eq = _equipment()
    oic = _oic_for(eq)
    day = _future_weekday(1)
    s9, s10, s11, s12 = _slots(eq, day)
    _bulk(oic, eq, status="OPERATOR_ABSENT", slot_ids=[s9.pk], disruption_reason="Operator on medical leave",
          resolution_action="internal note")
    DailySlot.objects.filter(pk=s10.pk).update(status=SlotStatus.UNDER_MAINTENANCE)  # no recorded event
    DailySlot.objects.filter(pk=s11.pk).update(status=SlotStatus.BLOCKED, blocked_label="Holiday style block")
    _bulk(oic, eq, status="NOT_AVAILABLE", slot_ids=[s12.pk])

    _, rows = _slot_rows(_user(UserType.STUDENT), eq, day)
    pub = rows[s9.pk]["disruption_public"]
    assert pub["label"] == "Operator absent" and pub["reason"] == "Operator on medical leave"
    assert pub["recovery_text"] == RECOVERY_UNKNOWN_TEXT
    assert set(pub) == {"type", "label", "reason", "expected_recovery_at", "recovery_status", "recovery_text"}
    assert "disruption_reason" not in rows[s9.pk] and "disruption_event_id" not in rows[s9.pk]
    assert rows[s10.pk]["disruption_public"]["reason"] == "The equipment is under maintenance."
    assert "disruption_public" not in rows[s11.pk] and "disruption_public" not in rows[s12.pk]

    staff_row = _slot_rows(oic, eq, day)[1][s9.pk]
    assert staff_row["disruption_reason"] == "Operator on medical leave" and staff_row["disruption_public"]

    anon, anon_rows = _slot_rows(None, eq, day)
    if anon.status_code == 200 and s9.pk in anon_rows:
        assert anon_rows[s9.pk]["disruption_public"]["reason"] == "Operator on medical leave"

    _client(oic).post(f"{LIST_URL}{DisruptionEvent.objects.get(equipment=eq).pk}/delete/", {}, format="json")
    _, rows = _slot_rows(_user(UserType.STUDENT), eq, day)
    assert rows[s9.pk]["disruption_public"]["reason"] == "The operator is not available at this time."


# --- Expected recovery ------------------------------------------------------------------------------------


def test_recovery_wording():
    now = timezone.make_aware(datetime(2025, 10, 10, 9, 0))
    assert recovery_text(None, now) == "Recovery date not yet announced"
    assert recovery_text(timezone.make_aware(datetime(2025, 10, 13, 10, 0)), now) == "Expected back: Mon 13 Oct, 10:00"
    assert recovery_text(timezone.make_aware(datetime(2025, 10, 10, 8, 0)), now) == "Recovery delayed — update awaited"


def test_expected_recovery_on_equipment_notice_editable_and_logged():
    eq = _equipment()
    oic = _oic_for(eq)
    assert public_equipment_notice(eq) is None
    expected = (timezone.localtime() + timedelta(days=3)).replace(hour=10, minute=0, second=0, microsecond=0)
    res = _client(oic).patch(
        f"/api/equipments/{eq.pk}/",
        {"status": "REPAIR", "disruption_reason": "Detector failure", "expected_recovery_at": expected.isoformat()},
        format="json",
    )
    assert res.status_code == 200, res.data
    eq.refresh_from_db()
    event = DisruptionEvent.objects.get(equipment=eq)
    assert event.expected_recovery_at == expected
    notice = EquipmentListSerializer(eq).data["disruption_notice"]
    assert notice["message"] == f"Under maintenance · Expected back: {expected:%a} {expected.day} {expected:%b}, 10:00"
    assert notice["reason"] == "Detector failure" and "started_by" not in str(notice)

    res = _client(oic).patch(f"{LIST_URL}{event.pk}/", {"expected_recovery_at": ""}, format="json")
    assert res.status_code == 200 and res.data["recovery_text"] == RECOVERY_UNKNOWN_TEXT
    assert any(t["kind"] == "recovery" for t in res.data["timeline"])
    assert public_equipment_notice(eq)["message"] == "Under maintenance · Recovery date not yet announced"

    past = timezone.now() - timedelta(hours=1)
    _client(oic).patch(f"{LIST_URL}{event.pk}/", {"expected_recovery_at": past.isoformat()}, format="json")
    assert public_equipment_notice(eq)["recovery_text"] == RECOVERY_DELAYED_TEXT
    eq.refresh_from_db()
    assert eq.status == EquipmentStatus.REPAIR  # no automatic status change

    assert _client(oic).patch(f"{LIST_URL}{event.pk}/", {"expected_recovery_at": "soon"}, format="json").status_code == 400

    _client(oic).patch(f"/api/equipments/{eq.pk}/", {"status": "ACTIVE"}, format="json")
    eq.refresh_from_db()
    assert public_equipment_notice(eq) is None


def test_slot_disruption_records_expected_recovery_from_the_dialog():
    eq = _equipment()
    oic = _oic_for(eq)
    s9, *_ = _slots(eq, _future_weekday())
    when = timezone.now() + timedelta(days=20)
    _bulk(oic, eq, status="UNDER_MAINTENANCE", slot_ids=[s9.pk], expected_recovery_at=when.isoformat())
    assert DisruptionEvent.objects.get(equipment=eq).expected_recovery_at == when.replace(microsecond=when.microsecond)


# --- Procurement request ----------------------------------------------------------------------------------


def _enable_procurement(dept):
    from iic_booking.procurement_management import config_service
    from iic_booking.procurement_management import constants as pc

    admin = _user(UserType.ADMIN)
    config_service.update_config(admin, dept, {"module_enabled": True, "pilot_mode": False})
    config_service.assign_role(admin, dept, _user(UserType.OPERATOR, department=dept), pc.ModuleRole.OC_STORES)
    dept.head = _user(UserType.HOD, department=dept)
    dept.save(update_fields=["head"])


def _closed_event(eq, oic, settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    s9, *_ = _slots(eq, _future_weekday())
    _bulk(oic, eq, status="UNDER_MAINTENANCE", slot_ids=[s9.pk])
    _bulk(oic, eq, status="AVAILABLE", slot_ids=[s9.pk])
    event = DisruptionEvent.objects.get(equipment=eq)
    pdf = SimpleUploadedFile("service.pdf", b"%PDF-1.4 service", content_type="application/pdf")
    assert _client(oic).post(f"{LIST_URL}{event.pk}/service-report/", {"file": pdf}, format="multipart").status_code == 201
    return event


PAYLOAD = {
    "category": "CONSUMABLE",
    "items": [
        {"name": "Vacuum pump oil", "quantity": 2, "estimated_cost": 1500, "recommended_by_service_person": True},
        {"name": "O-ring kit", "quantity": 1, "estimated_cost": 800, "notes": "Viton"},
    ],
    "notes": "Engineer advised replacement within a month.",
}


def test_procurement_request_hidden_when_department_module_off(settings, tmp_path):
    eq = _equipment(internal_department=_department("NoPM"))
    oic = _oic_for(eq)
    event = _closed_event(eq, oic, settings, tmp_path)
    opts = _client(oic).get(f"{LIST_URL}procurement-options/", {"equipment": eq.pk}).data
    assert opts == {"available": False, "categories": []}
    assert _client(oic).get(f"{LIST_URL}{event.pk}/").data["procurement"]["available"] is False
    assert _client(oic).post(f"{LIST_URL}{event.pk}/procurement-request/", PAYLOAD, format="json").status_code == 403


def test_procurement_request_raised_submitted_and_linked(settings, tmp_path):
    from iic_booking.procurement_management.models import ProcurementDocument, PurchaseRequest

    dept = _department("IIC")
    _enable_procurement(dept)
    eq = _equipment(internal_department=dept)
    oic = _oic_for(eq)
    event = _closed_event(eq, oic, settings, tmp_path)

    opts = _client(oic).get(f"{LIST_URL}procurement-options/", {"equipment": eq.pk}).data
    assert opts["available"] is True
    assert {c["value"] for c in opts["categories"]} == {"CONSUMABLE", "MINOR_ASSET", "MAJOR_ASSET"}
    assert _client(_user(UserType.STUDENT)).post(
        f"{LIST_URL}{event.pk}/procurement-request/", PAYLOAD, format="json"
    ).status_code == 403
    bad = _client(oic).post(f"{LIST_URL}{event.pk}/procurement-request/", {**PAYLOAD, "items": []}, format="json")
    assert bad.status_code == 400

    res = _client(oic).post(f"{LIST_URL}{event.pk}/procurement-request/", PAYLOAD, format="json")
    assert res.status_code == 201, res.data
    info = res.data["request"]
    pr = PurchaseRequest.objects.get(pk=info["id"])
    assert pr.equipment_id == eq.pk and pr.requested_by_id == oic.pk and pr.lines.count() == 2
    assert f"disruption #{event.pk}" in pr.justification
    assert info["submitted"] is True and pr.status != "DRAFT", info.get("submit_error")
    assert info["reports_attached"] == 1
    assert ProcurementDocument.objects.filter(purchase_request=pr).count() == 1
    event.refresh_from_db()
    assert event.procurement_request_ids == [pr.pk]
    linked = res.data["disruption"]["procurement_requests"]
    assert linked == [{"id": pr.pk, "number": pr.number, "status": pr.status, "status_display": pr.get_status_display()}]
    assert any(t["kind"] == "procurement" for t in res.data["disruption"]["timeline"])
    assert _client(oic).get(LIST_URL).data["results"][0]["procurement_requests"][0]["number"] == pr.number

import re
from decimal import Decimal

import pytest

from iic_booking.procurement_management import constants as c
from iic_booking.procurement_management.models import Item, ProcurementAuditLog, PurchaseRequest, Vendor

from .conftest import API, category, client_for, config_of, line, make_equipment, new_request

pytestmark = pytest.mark.django_db

VALID_GSTIN = "27AAPFU0939F1ZV"


class TestMastersPermissions:
    def test_anyone_with_role_reads_categories(self, world):
        res = client_for(world.operator).get(f"{API}/categories/?department_id={world.dept.pk}")
        assert res.status_code == 200
        codes = {row["code"] for row in res.json()["results"]}
        assert {"CONSUMABLE", "MAJOR_ASSET"} <= codes

    def test_other_department_hidden(self, world):
        res = client_for(world.operator).get(f"{API}/categories/?department_id={world.other_dept.pk}")
        assert res.status_code == 404

    def test_only_admin_edits_categories(self, world):
        cat = category(world.dept, "CONSUMABLE")
        res = client_for(world.office).patch(f"{API}/categories/{cat.pk}/", {"approval_exempt": True}, format="json")
        assert res.status_code == 403
        res = client_for(world.admin).patch(f"{API}/categories/{cat.pk}/", {"approval_exempt": True}, format="json")
        assert res.status_code == 200
        assert ProcurementAuditLog.objects.filter(action="category.updated", object_id=str(cat.pk)).exists()

    def test_only_admin_edits_request_types(self, world):
        rt_id = client_for(world.oic).get(f"{API}/request-types/?department_id={world.dept.pk}").json()["results"][0]["id"]
        assert client_for(world.office).patch(f"{API}/request-types/{rt_id}/", {"requires_oic": False}, format="json").status_code == 403
        res = client_for(world.admin).patch(
            f"{API}/request-types/{rt_id}/", {"procurement_steps": ["PAYMENT", "INDENT"]}, format="json"
        )
        assert res.status_code == 200
        assert res.json()["procurement_steps"] == ["INDENT", "PAYMENT"]

    def test_vendor_needs_masters_permission(self, world):
        body = {"department_id": world.dept.pk, "name": "Acme Scientific"}
        assert client_for(world.oic).post(f"{API}/vendors/", body, format="json").status_code == 403
        res = client_for(world.office).post(f"{API}/vendors/", body, format="json")
        assert res.status_code == 201
        assert res.json()["code"].startswith("VEN/")
        res = client_for(world.stores).post(f"{API}/vendors/", {**body, "name": "Beta Labs"}, format="json")
        assert res.status_code == 201


class TestVendorValidation:
    def test_gstin_checksum_and_pan_derivation(self, world):
        cl = client_for(world.office)
        bad = cl.post(f"{API}/vendors/", {"department_id": world.dept.pk, "name": "X", "gstin": "27AAPFU0939F1ZA"}, format="json")
        assert bad.status_code == 400 and bad.json()["code"] == "invalid_gstin"
        ok = cl.post(f"{API}/vendors/", {"department_id": world.dept.pk, "name": "X", "gstin": VALID_GSTIN.lower()}, format="json")
        assert ok.status_code == 201
        assert ok.json()["gstin"] == VALID_GSTIN
        assert ok.json()["pan"] == "AAPFU0939F"

    def test_pan_mismatch_and_duplicate_gstin(self, world):
        cl = client_for(world.office)
        res = cl.post(
            f"{API}/vendors/", {"department_id": world.dept.pk, "name": "X", "gstin": VALID_GSTIN, "pan": "ABCDE1234F"},
            format="json",
        )
        assert res.json()["code"] == "pan_gstin_mismatch"
        cl.post(f"{API}/vendors/", {"department_id": world.dept.pk, "name": "X", "gstin": VALID_GSTIN}, format="json")
        dup = cl.post(f"{API}/vendors/", {"department_id": world.dept.pk, "name": "Y", "gstin": VALID_GSTIN}, format="json")
        assert dup.status_code == 400 and dup.json()["code"] == "duplicate"

    def test_archive_is_soft_and_needs_reason(self, world):
        cl = client_for(world.office)
        vid = cl.post(f"{API}/vendors/", {"department_id": world.dept.pk, "name": "Old"}, format="json").json()["id"]
        assert cl.post(f"{API}/vendors/{vid}/archive/", {}, format="json").status_code == 400
        assert cl.post(f"{API}/vendors/{vid}/archive/", {"reason": "duplicate"}, format="json").status_code == 200
        v = Vendor.objects.get(pk=vid)
        assert v.is_archived and not v.active
        listed = cl.get(f"{API}/vendors/?department_id={world.dept.pk}").json()["results"]
        assert vid not in [row["id"] for row in listed]


class TestItems:
    def test_create_item_with_category_and_gst(self, world):
        cl = client_for(world.stores)
        gst = cl.get(f"{API}/gst-rates/?department_id={world.dept.pk}").json()["results"]
        g18 = next(g for g in gst if g["rate"] == "18.00")
        res = cl.post(
            f"{API}/items/",
            {
                "department_id": world.dept.pk,
                "name": "Nitrile gloves",
                "category_id": category(world.dept, "CONSUMABLE").pk,
                "uom": "Box",
                "hsn_sac": "4015",
                "default_gst_rate_id": g18["id"],
                "reorder_level": "5",
            },
            format="json",
        )
        assert res.status_code == 201, res.json()
        body = res.json()
        assert body["code"].startswith("ITM/")
        assert body["default_gst_rate"] == "18.00"
        assert body["reorder_level"] == "5.000"

    def test_item_category_must_belong_to_department(self, world):
        other_cat = category(world.other_dept, "CONSUMABLE")
        res = client_for(world.stores).post(
            f"{API}/items/", {"department_id": world.dept.pk, "name": "X", "category_id": other_cat.pk}, format="json"
        )
        assert res.status_code == 400

    def test_gst_split_must_add_up(self, world):
        cl = client_for(world.office)
        res = cl.post(f"{API}/gst-rates/", {"department_id": world.dept.pk, "rate": "3"}, format="json")
        assert res.status_code == 201
        assert res.json()["cgst_rate"] == "1.50"
        bad = cl.patch(f"{API}/gst-rates/{res.json()['id']}/", {"cgst_rate": "2"}, format="json")
        assert bad.status_code == 400


class TestRequestCreation:
    def test_operator_creates_draft_department_from_equipment(self, world):
        r = new_request(world.operator, equipment=world.equipment, lines=[line("100.00", "3", "18")])
        assert r.department == world.dept
        assert r.status == c.RequestStatus.DRAFT
        assert r.raised_as_role == c.ModuleRole.LAB_OPERATOR
        assert re.fullmatch(r"REQ/\d{4}-\d{2}/\d{5}", r.number)
        assert r.estimated_total == Decimal("354.00")
        assert ProcurementAuditLog.objects.filter(action="request.created", object_id=str(r.pk)).exists()

    def test_department_cannot_be_chosen_directly(self, world):
        r = new_request(world.operator, equipment=world.equipment, department_id=world.other_dept.pk)
        assert r.department == world.dept

    def test_operator_needs_equipment(self, world):
        res = new_request(world.operator, expect=400)
        assert res.json()["code"] == "equipment_required"
        res = new_request(world.operator, department_id=world.dept.pk, expect=400)
        assert res.json()["code"] == "equipment_required"

    def test_multi_department_user_names_department_without_equipment(self, world):
        res = new_request(world.admin, rt="GENERAL_OFFICE", expect=400)
        assert res.json()["code"] == "department_ambiguous"
        r = new_request(world.admin, rt="GENERAL_OFFICE", department_id=world.other_dept.pk)
        assert r.department == world.other_dept
        assert new_request(world.office, rt="GENERAL_OFFICE").department == world.dept
        new_request(world.office, rt="GENERAL_OFFICE", department_id=world.other_dept.pk, expect=404)

    def test_cannot_raise_for_other_labs_equipment(self, world):
        res = new_request(world.operator, equipment=world.equipment2, expect=403)
        assert res.status_code == 403

    def test_cannot_raise_in_other_department(self, world):
        new_request(world.operator, equipment=world.other_equipment, expect=403)

    def test_disabled_department_refused(self, world):
        cfg = config_of(world.dept)
        cfg.module_enabled = False
        cfg.save()
        new_request(world.operator, equipment=world.equipment, expect=403)

    def test_sub_feature_switch_enforced(self, world):
        cfg = config_of(world.dept)
        cfg.consumables_enabled = False
        cfg.save()
        res = new_request(world.operator, equipment=world.equipment, cat=category(world.dept, "CONSUMABLE"), expect=400)
        assert res.json()["code"] == "feature_disabled"

    def test_auditor_cannot_raise(self, world):
        res = client_for(world.auditor).post(
            f"{API}/requests/", {"title": "x", "request_type": "CONSUMABLE", "lines": [line()]}, format="json"
        )
        assert res.status_code == 403

    def test_invalid_money_rejected(self, world):
        for bad in ("-1", "1.005", "abc"):
            res = new_request(world.operator, equipment=world.equipment, lines=[line(bad)], expect=400)
            assert res.json()["code"] == "invalid_number"

    def test_line_uses_item_defaults(self, world):
        cl = client_for(world.stores)
        gst = cl.get(f"{API}/gst-rates/?department_id={world.dept.pk}").json()["results"]
        g12 = next(g for g in gst if g["rate"] == "12.00")
        item_id = cl.post(
            f"{API}/items/",
            {"department_id": world.dept.pk, "name": "Pipette tips", "category_id": category(world.dept, "CONSUMABLE").pk,
             "uom": "Pack", "default_gst_rate_id": g12["id"]},
            format="json",
        ).json()["id"]
        r = new_request(world.operator, equipment=world.equipment, lines=[{"item_id": item_id, "quantity": "2", "estimated_unit_price": "500"}])
        ln = r.lines.get()
        assert ln.description == "Pipette tips" and ln.uom == "Pack" and ln.gst_rate == Decimal("12.00")
        assert r.estimated_total == Decimal("1120.00")
        assert Item.objects.get(pk=item_id).department == world.dept


class TestRequestVisibilityAndEdit:
    def test_visibility(self, world):
        r = new_request(world.operator, equipment=world.equipment)
        assert client_for(world.operator).get(f"{API}/requests/{r.pk}/").status_code == 200
        assert client_for(world.oic).get(f"{API}/requests/{r.pk}/").status_code == 200
        assert client_for(world.office).get(f"{API}/requests/{r.pk}/").status_code == 200
        assert client_for(world.operator2).get(f"{API}/requests/{r.pk}/").status_code == 404
        assert client_for(world.oic2).get(f"{API}/requests/{r.pk}/").status_code == 404
        assert client_for(world.other_oic).get(f"{API}/requests/{r.pk}/").status_code == 404
        ids = [row["id"] for row in client_for(world.operator2).get(f"{API}/requests/").json()["results"]]
        assert r.pk not in ids

    def test_only_requester_edits_draft(self, world):
        r = new_request(world.operator, equipment=world.equipment)
        res = client_for(world.oic).patch(f"{API}/requests/{r.pk}/", {"title": "Hacked"}, format="json")
        assert res.status_code == 403
        res = client_for(world.operator).patch(
            f"{API}/requests/{r.pk}/", {"title": "Updated", "lines": [line("50.00", "2")]}, format="json"
        )
        assert res.status_code == 200
        r.refresh_from_db()
        assert r.title == "Updated" and r.estimated_total == Decimal("100.00")
        log = ProcurementAuditLog.objects.filter(action="request.updated", object_id=str(r.pk)).get()
        assert log.old_value["title"] == "Request" and log.new_value["title"] == "Updated"

    def test_detail_shows_actions(self, world):
        r = new_request(world.operator, equipment=world.equipment)
        body = client_for(world.operator).get(f"{API}/requests/{r.pk}/").json()
        assert set(body["available_actions"]) >= {"edit", "submit", "cancel"}
        assert body["estimated_total"] == "1000.00"
        assert client_for(world.oic).get(f"{API}/requests/{r.pk}/").json()["available_actions"] == []

    def test_requests_are_never_hard_deleted(self, world):
        from iic_booking.procurement_management.models import ImmutableRecordError

        r = new_request(world.operator, equipment=world.equipment)
        with pytest.raises(ImmutableRecordError):
            PurchaseRequest.objects.get(pk=r.pk).delete()

    def test_office_without_equipment_uses_its_only_department(self, world):
        r = new_request(world.office, rt="GENERAL_OFFICE")
        assert r.department == world.dept
        assert r.raised_as_role == c.ModuleRole.OFFICE

    def test_office_can_raise_for_any_department_equipment(self, world):
        r = new_request(world.office, equipment=make_equipment(world.dept))
        assert r.raised_as_role == c.ModuleRole.OFFICE

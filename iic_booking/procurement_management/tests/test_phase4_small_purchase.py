import json
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from iic_booking.procurement_management import config_service
from iic_booking.procurement_management import constants as c
from iic_booking.procurement_management.models import (
    Invoice,
    NumberSequence,
    ProcurementAuditLog,
    ProcurementDocument,
    ProcurementRecord,
)
from iic_booking.users.models.user_type import UserType

from .conftest import API, act, category, client_for, config_of, line, make_user, new_request, pdf_upload

pytestmark = pytest.mark.django_db
PRS = c.ProcurementRecordStatus
RS = c.RequestStatus


def invoice_block(price="1999.00", gst="0", qty="1", **extra):
    return {
        "vendor_name": extra.pop("vendor_name", "Local Scientific Store"),
        "invoice_number": extra.pop("invoice_number", f"B-{uuid.uuid4().hex[:6]}"),
        "invoice_date": timezone.localdate().isoformat(),
        "lines": [{"description": "Beakers", "quantity": qty, "unit_price": price, "gst_rate": gst}],
        **extra,
    }


def direct_body(world, price="1999.00", gst="0", cat="CONSUMABLE", **extra):
    return {
        "department_id": world.dept.pk,
        "category_id": category(world.dept, cat).pk,
        "title": "Glassware",
        "invoice": invoice_block(price, gst),
        **extra,
    }


def post_sp(user, body, files=None, expect=201):
    cl = client_for(user)
    if files is not None:
        res = cl.post(f"{API}/small-purchases/", {"payload": json.dumps(body), "files": files}, format="multipart")
    else:
        res = cl.post(f"{API}/small-purchases/", body, format="json")
    assert res.status_code == expect, res.json()
    return res


@pytest.fixture
def office2(world):
    user = make_user(user_type=UserType.FINANCE, name="Office Two")
    config_service.assign_role(world.admin, world.dept, user, c.ModuleRole.OFFICE)
    return user


class TestThreshold:
    @pytest.mark.parametrize("price,ok", [("1999.00", True), ("2000.00", True), ("2001.00", False)])
    def test_boundary(self, world, price, ok):
        before = ProcurementRecord.objects.count()
        res = post_sp(world.office, direct_body(world, price), expect=201 if ok else 400)
        if ok:
            assert res.json()["is_small_purchase"] is True
            assert res.json()["origin"] == c.RequestOrigin.DIRECT_PURCHASE
            assert res.json()["number"].startswith("SP/")
        else:
            assert res.json()["code"] == "above_small_purchase_threshold"
            assert res.json()["threshold"] == "2000.00"
            assert ProcurementRecord.objects.count() == before
            assert not Invoice.objects.filter(total_amount=Decimal("2001.00")).exists()

    def test_rejected_entry_consumes_no_number(self, world):
        post_sp(world.office, direct_body(world, "5000.00"), expect=400)
        assert not NumberSequence.objects.filter(prefix="SP").exists()

    def test_threshold_includes_gst(self, world):
        res = post_sp(world.office, direct_body(world, "1700.00", "18"), expect=400)
        assert res.json()["total"] == "2006.00"
        ok = post_sp(world.office, direct_body(world, "1000.00", "12"))
        assert ok.json()["invoices"][0]["total_amount"] == "1120.00"
        assert ok.json()["invoices"][0]["cgst_amount"] == "60.00"
        assert ok.json()["invoices"][0]["sgst_amount"] == "60.00"

    def test_threshold_is_configurable(self, world):
        config_service.update_config(world.admin, world.dept, {"small_purchase_threshold": "5000.00"})
        post_sp(world.office, direct_body(world, "4999.99"))
        config_service.update_config(world.admin, world.dept, {"small_purchase_threshold": "1500.00"})
        post_sp(world.office, direct_body(world, "1999.00"), expect=400)

    def test_category_rules(self, world):
        res = post_sp(world.office, direct_body(world, "100.00", cat="MAJOR_ASSET"), expect=400)
        assert res.json()["code"] == "category_not_allowed"
        cat = category(world.dept, "NON_CONSUMABLE")
        cat.approval_exempt = True
        cat.save()
        post_sp(world.office, direct_body(world, "9000.00", cat="NON_CONSUMABLE"))


class TestPermissions:
    def test_only_office_with_permission(self, world):
        post_sp(world.oic, direct_body(world), expect=403)
        post_sp(world.stores, direct_body(world), expect=403)
        clerk = make_user(user_type=UserType.FINANCE)
        config_service.assign_role(world.admin, world.dept, clerk, c.ModuleRole.OFFICE, ["invoices"])
        post_sp(clerk, direct_body(world), expect=403)

    def test_direct_entry_switch(self, world):
        config_service.update_config(world.admin, world.dept, {"allow_office_direct_purchase_entry": False})
        assert post_sp(world.office, direct_body(world), expect=400).json()["code"] == "direct_entry_disabled"

    def test_other_department_refused(self, world):
        body = direct_body(world)
        body["department_id"] = world.other_dept.pk
        post_sp(world.office, body, expect=404)

    def test_future_dates_refused(self, world):
        body = direct_body(world)
        body["invoice"]["invoice_date"] = (timezone.localdate() + timedelta(days=2)).isoformat()
        post_sp(world.office, body, expect=400)


class TestBillsAndCompletion:
    def test_multipart_capture_with_pages_completes(self, world):
        res = post_sp(world.office, direct_body(world), files=[pdf_upload("p1.pdf"), pdf_upload("p2.pdf")])
        body = res.json()
        assert body["status"] == PRS.COMPLETED
        docs = ProcurementDocument.objects.filter(procurement_record_id=body["id"]).order_by("page_number")
        assert [d.page_number for d in docs] == [1, 2]
        assert docs[0].page_group and docs[0].page_group == docs[1].page_group
        assert all(d.invoice_id for d in docs)
        assert ProcurementAuditLog.objects.filter(action="purchase.small_recorded", object_id=str(body["id"])).exists()

    def test_bill_document_required_before_completion(self, world):
        body = post_sp(world.office, direct_body(world)).json()
        assert body["status"] == PRS.INVOICED
        assert body["blockers"] == ["bill_document_missing"]
        res = client_for(world.office).post(f"{API}/records/{body['id']}/complete/", {}, format="json")
        assert res.status_code == 400 and res.json()["blockers"] == ["bill_document_missing"]
        up = client_for(world.office).post(
            f"{API}/records/{body['id']}/documents/", {"file": pdf_upload(), "doc_type": "INVOICE"}, format="multipart"
        )
        assert up.status_code == 201
        assert ProcurementRecord.objects.get(pk=body["id"]).status == PRS.COMPLETED

    def test_invoice_optional_when_configured(self, world):
        config_service.update_config(world.admin, world.dept, {"require_invoice": False})
        assert post_sp(world.office, direct_body(world)).json()["status"] == PRS.COMPLETED

    def test_asset_category_waits_for_asset_entry(self, world):
        body = post_sp(world.office, direct_body(world, "1500.00", cat="MINOR_ASSET"), files=[pdf_upload()]).json()
        assert body["status"] == PRS.INVOICED
        assert body["blockers"] == ["asset_entry_missing"]

    def test_duplicate_bill_refused(self, world):
        b1 = direct_body(world)
        b1["invoice"]["invoice_number"] = "INV-77"
        post_sp(world.office, b1)
        b2 = direct_body(world)
        b2["invoice"]["invoice_number"] = "inv-77"
        assert post_sp(world.office, b2, expect=400).json()["code"] == "duplicate_invoice"


class TestAgainstApprovedRequest:
    def _approved_small(self, world, price="1000.00"):
        r = new_request(world.operator, equipment=world.equipment, lines=[line(price)], submit=True)
        assert r.is_small_purchase is (Decimal(price) <= Decimal("2000.00"))
        act(world.oic, r, "approve")
        act(world.stores, r, "approve")
        assert r.status == RS.APPROVED
        return r

    def test_record_against_request_completes_request(self, world):
        r = self._approved_small(world)
        assert "record_purchase" in client_for(world.office).get(f"{API}/requests/{r.pk}/").json()["available_actions"]
        body = {"purchase_request_id": r.pk, "invoice": invoice_block("1050.00")}
        res = post_sp(world.office, body, files=[pdf_upload()])
        assert res.json()["status"] == PRS.COMPLETED
        assert res.json()["origin"] == c.RequestOrigin.REQUEST
        inv = res.json()["invoices"][0]
        assert inv["approved_amount"] == "1000.00" and inv["variance_percent"] == "5.00"
        assert inv["variance_status"] == c.VarianceStatus.WITHIN_TOLERANCE
        r.refresh_from_db()
        assert r.status == RS.COMPLETED
        actions = list(r.approval_actions.values_list("action", flat=True))
        assert actions[-3:] == ["INVOICE_RECORDED", "MARK_PURCHASED", "COMPLETE"]
        post_sp(world.office, body, expect=400)
        assert client_for(world.operator).get(f"{API}/records/{res.json()['id']}/").status_code == 200
        assert client_for(world.operator2).get(f"{API}/records/{res.json()['id']}/").status_code == 404

    def test_unapproved_or_large_request_refused(self, world):
        draft = new_request(world.operator, equipment=world.equipment)
        post_sp(world.office, {"purchase_request_id": draft.pk, "invoice": invoice_block()}, expect=400)
        big = self._approved_small(world, "5000.00")
        post_sp(world.office, {"purchase_request_id": big.pk, "invoice": invoice_block("5000.00")}, expect=400)

    def test_variance_office_review(self, world, office2):
        r = self._approved_small(world)
        res = post_sp(world.office, {"purchase_request_id": r.pk, "invoice": invoice_block("1200.00")}, files=[pdf_upload()])
        body = res.json()
        inv = body["invoices"][0]
        assert inv["variance_status"] == c.VarianceStatus.OFFICE_REVIEW and inv["variance_percent"] == "20.00"
        assert body["blockers"] == ["variance_open"]
        url = f"{API}/invoices/{inv['id']}/variance-review/"
        assert client_for(world.office).post(url, {"note": "ok"}, format="json").status_code == 403
        assert client_for(office2).post(url, {}, format="json").status_code == 400
        assert client_for(office2).post(url, {"note": "Price rise accepted"}, format="json").status_code == 200
        assert ProcurementRecord.objects.get(pk=body["id"]).status == PRS.COMPLETED
        r.refresh_from_db()
        assert r.status == RS.COMPLETED

    def test_variance_reapproval_needs_hod(self, world, office2):
        config_service.update_config(world.admin, world.dept, {"variance_action": "REAPPROVAL"})
        r = self._approved_small(world)
        body = post_sp(world.office, {"purchase_request_id": r.pk, "invoice": invoice_block("1200.00")}, files=[pdf_upload()]).json()
        inv = body["invoices"][0]
        assert inv["variance_status"] == c.VarianceStatus.REAPPROVAL_REQUIRED
        assert r.approval_actions.filter(action="REAPPROVAL_REQUIRED").exists()
        url = f"{API}/invoices/{inv['id']}/variance-review/"
        assert client_for(office2).post(url, {"note": "x"}, format="json").status_code == 403
        assert client_for(world.hod).post(url, {"note": "Re-approved"}, format="json").status_code == 200

    def test_variance_flag_only_does_not_block(self, world):
        config_service.update_config(world.admin, world.dept, {"variance_action": "FLAG_ONLY"})
        r = self._approved_small(world)
        body = post_sp(world.office, {"purchase_request_id": r.pk, "invoice": invoice_block("1200.00")}, files=[pdf_upload()]).json()
        assert body["invoices"][0]["variance_status"] == c.VarianceStatus.FLAGGED
        assert body["status"] == PRS.COMPLETED


class TestPayments:
    def test_payment_rules(self, world):
        body = post_sp(world.office, direct_body(world, "1500.00"), files=[pdf_upload()]).json()
        url = f"{API}/invoices/{body['invoices'][0]['id']}/payments/"
        today = timezone.localdate().isoformat()
        cl = client_for(world.office)
        assert cl.post(url, {"amount": "1500.01", "payment_date": today, "payment_reference": "UTR1"}, format="json").status_code == 400
        assert cl.post(url, {"amount": "500.00", "payment_date": today, "payment_reference": "UTR1"}, format="json").json()["payment_status"] == "PARTIALLY_PAID"
        res = cl.post(url, {"amount": "1000.00", "payment_date": today, "payment_reference": "UTR2"}, format="json")
        assert res.json()["payment_status"] == "PAID" and res.json()["paid_amount"] == "1500.00"
        rec = ProcurementRecord.objects.get(pk=body["id"])
        assert rec.payment_status == "PAID" and rec.paid_amount == Decimal("1500.00")
        assert client_for(world.stores).post(url, {"amount": "1", "payment_date": today, "payment_reference": "x"}, format="json").status_code == 403

    def test_cannot_pay_with_open_variance(self, world):
        cfg = config_of(world.dept)
        assert cfg.variance_action == c.VarianceAction.OFFICE_REVIEW
        r = new_request(world.operator, equipment=world.equipment, lines=[line("1000.00")], submit=True)
        act(world.oic, r, "approve")
        act(world.stores, r, "approve")
        body = post_sp(world.office, {"purchase_request_id": r.pk, "invoice": invoice_block("1300.00")}).json()
        url = f"{API}/invoices/{body['invoices'][0]['id']}/payments/"
        res = client_for(world.office).post(
            url, {"amount": "100.00", "payment_date": timezone.localdate().isoformat(), "payment_reference": "x"}, format="json"
        )
        assert res.json()["code"] == "variance_open"

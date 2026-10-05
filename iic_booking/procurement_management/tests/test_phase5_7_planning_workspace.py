import uuid
from decimal import Decimal

import pytest
from django.utils import timezone

from iic_booking.procurement_management import config_service
from iic_booking.procurement_management import constants as c
from iic_booking.procurement_management.models import PlanRequirement, ProcurementRecord, RequirementChangeLog, Vendor
from iic_booking.users.models.user_type import UserType

from .conftest import API, act, client_for, line, make_user, new_request, pdf_upload

pytestmark = pytest.mark.django_db
RQ = c.RequirementStatus
PRS = c.ProcurementRecordStatus
RS = c.RequestStatus
FY = "2027-28"
TODAY = lambda: timezone.localdate().isoformat()  # noqa: E731


def raise_req(user, equipment=None, expect=201, **extra):
    body = {
        "funding_type": extra.pop("funding_type", "PLAN"),
        "financial_year": FY,
        "description": extra.pop("description", "Cryostat"),
        "justification": "Needed",
        "quantity": extra.pop("quantity", "2"),
        "uom": "Nos",
        "estimated_unit_cost": extra.pop("estimated_unit_cost", "50000.00"),
        **extra,
    }
    if equipment is not None:
        body["equipment_id"] = equipment.pk
    res = client_for(user).post(f"{API}/requirements/", body, format="json")
    assert res.status_code == expect, res.json()
    return res.json()


def req_action(user, rid, action, expect=200, **body):
    res = client_for(user).post(f"{API}/requirements/{rid}/{action}/", body, format="json")
    assert res.status_code == expect, res.json()
    return res.json()


def submitted(world, user=None, equipment=None, **extra):
    body = raise_req(user or world.operator, equipment or world.equipment, **extra)
    return req_action(user or world.operator, body["id"], "submit")


@pytest.fixture
def office2(world):
    user = make_user(user_type=UserType.FINANCE, name="Office Two")
    config_service.assign_role(world.admin, world.dept, user, c.ModuleRole.OFFICE)
    return user


class TestRequirements:
    def test_raise_submit_freezes_original_values(self, world):
        body = raise_req(world.operator, world.equipment)
        assert body["number"].startswith(f"PLAN/{FY}/") and body["status"] == RQ.DRAFT
        assert body["estimated_total"] == "100000.00"
        out = req_action(world.operator, body["id"], "submit")
        assert out["status"] == RQ.SUBMITTED
        assert out["original_values"]["quantity"] == "2.000"
        np_body = raise_req(world.operator, world.equipment, funding_type="NON_PLAN")
        assert np_body["number"].startswith(f"NP/{FY}/")

    def test_visibility(self, world):
        rid = raise_req(world.operator, world.equipment)["id"]
        assert client_for(world.operator2).get(f"{API}/requirements/{rid}/").status_code == 404
        assert client_for(world.office).get(f"{API}/requirements/{rid}/").status_code == 200
        assert client_for(world.oic).get(f"{API}/requirements/{rid}/").status_code == 200

    def test_lab_staff_only_for_own_equipment(self, world):
        raise_req(world.operator, world.equipment2, expect=403)
        raise_req(world.stores, world.equipment, expect=403)

    def test_plan_window_closed(self, world):
        config_service.update_config(world.admin, world.dept, {"plan_submission_open": False})
        assert raise_req(world.operator, world.equipment, expect=400)["code"] == "plan_closed"
        raise_req(world.operator, world.equipment, funding_type="NON_PLAN")

    def test_office_edit_needs_reason_and_logs_fields(self, world):
        r = submitted(world)
        req_action(world.office, r["id"], "office-edit", expect=400, changes={"quantity": "3"})
        req_action(world.oic, r["id"], "office-edit", expect=403, changes={"quantity": "3"}, reason="x")
        out = req_action(world.office, r["id"], "office-edit", changes={"quantity": "3"}, reason="Third user group")
        assert out["status"] == RQ.UNDER_CONSOLIDATION and out["estimated_total"] == "150000.00"
        assert out["original_values"]["quantity"] == "2.000"
        logs = {(x["field"], x["old_value"], x["new_value"]) for x in out["changes"]}
        assert ("quantity", "2.000", "3.000") in logs and ("estimated_total", "100000.00", "150000.00") in logs
        assert all(x["reason"] == "Third user group" for x in out["changes"])

    def test_office_add_remove_restore(self, world):
        added = raise_req(world.office, None, department_id=world.dept.pk, reason="Common facility need")
        assert added["added_by_office"] and added["status"] == RQ.SUBMITTED
        assert RequirementChangeLog.objects.filter(requirement_id=added["id"], change_type="ADD").exists()
        assert req_action(world.office, added["id"], "remove", reason="Duplicate")["status"] == RQ.REMOVED
        assert req_action(world.office, added["id"], "restore", reason="Needed after all")["status"] == RQ.UNDER_CONSOLIDATION

    def test_merge_and_split(self, world):
        a = submitted(world, quantity="2")
        b = submitted(world, quantity="3")
        out = req_action(world.office, a["id"], "merge", source_ids=[b["id"]], reason="Same item")
        assert out["quantity"] == "5.000"
        assert PlanRequirement.objects.get(pk=b["id"]).status == RQ.MERGED
        assert PlanRequirement.objects.get(pk=b["id"]).merged_into_id == a["id"]
        req_action(world.office, a["id"], "split", parts=[{"quantity": "5"}], reason="x", expect=400)
        req_action(world.office, a["id"], "split", parts=[{"quantity": "2", "description": "Cryostat (lab B)"}], reason="Two labs")
        a_db = PlanRequirement.objects.get(pk=a["id"])
        child = PlanRequirement.objects.get(split_from=a_db)
        assert a_db.quantity == Decimal("3.000") and child.quantity == Decimal("2.000")
        assert child.change_logs.filter(change_type="SPLIT").exists()

    def test_merge_refuses_other_bucket(self, world):
        a = submitted(world)
        b = submitted(world, funding_type="NON_PLAN")
        req_action(world.office, a["id"], "merge", source_ids=[b["id"]], reason="x", expect=400)


class TestProposals:
    def _proposal(self, world, *reqs):
        res = client_for(world.office).post(
            f"{API}/proposals/",
            {"department_id": world.dept.pk, "funding_type": "PLAN", "financial_year": FY, "title": "Plan 2027-28",
             "requirement_ids": [r["id"] for r in reqs]},
            format="json",
        )
        assert res.status_code == 201, res.json()
        return res.json()

    def test_full_cycle_in_app(self, world):
        a, b = submitted(world), submitted(world, estimated_unit_cost="1000.00")
        p = self._proposal(world, a, b)
        assert p["status"] == c.ProposalStatus.CONSOLIDATED and p["total_amount"] == "102000.00"
        cl = client_for(world.office)
        assert cl.post(f"{API}/proposals/{p['id']}/send/", {}, format="json").json()["status"] == "SENT_FOR_APPROVAL"
        req_action(world.office, a["id"], "office-edit", changes={"quantity": "1"}, reason="x", expect=400)
        hod = client_for(world.hod)
        assert hod.post(f"{API}/proposals/{p['id']}/decide/", {"decision": "REJECT"}, format="json").status_code == 400
        res = hod.post(f"{API}/proposals/{p['id']}/decide/", {"decision": "APPROVE", "amounts": {str(a["id"]): "90000.00"}}, format="json")
        assert res.status_code == 200, res.json()
        assert res.json()["status"] == "APPROVED" and res.json()["approved_amount"] == "92000.00"
        assert PlanRequirement.objects.get(pk=a["id"]).status == RQ.APPROVED
        pdf = cl.get(f"{API}/proposals/{p['id']}/pdf/")
        assert pdf.status_code == 200 and pdf["Content-Type"] == "application/pdf" and pdf.content.startswith(b"%PDF")

    def test_only_hod_or_offline_office(self, world, office2):
        p = self._proposal(world, submitted(world))
        client_for(world.office).post(f"{API}/proposals/{p['id']}/send/", {}, format="json")
        assert client_for(world.oic).post(f"{API}/proposals/{p['id']}/decide/", {"decision": "APPROVE"}, format="json").status_code == 404
        assert client_for(world.stores).post(f"{API}/proposals/{p['id']}/decide/", {"decision": "APPROVE"}, format="json").status_code == 403
        body = {"decision": "APPROVE", "approver_name": "Prof. X", "approver_designation": "HOD", "approval_date": TODAY(), "file": pdf_upload()}
        assert client_for(world.office).post(f"{API}/proposals/{p['id']}/decide/", body, format="multipart").status_code == 403
        body["file"] = pdf_upload()
        res = client_for(office2).post(f"{API}/proposals/{p['id']}/decide/", body, format="multipart")
        assert res.status_code == 200, res.json()
        assert res.json()["history"][-1]["is_offline"] is True

    def test_bucket_enforced(self, world):
        np_req = submitted(world, funding_type="NON_PLAN")
        res = client_for(world.office).post(
            f"{API}/proposals/",
            {"department_id": world.dept.pk, "funding_type": "PLAN", "financial_year": FY, "title": "x", "requirement_ids": [np_req["id"]]},
            format="json",
        )
        assert res.status_code == 400


def _vendor(world, name):
    return Vendor.objects.create(department=world.dept, code=f"V-{uuid.uuid4().hex[:6]}", name=name)


def _approved_request(world, price="30000.00"):
    r = new_request(world.operator, equipment=world.equipment, lines=[line(price)], submit=True)
    for actor in (world.oic, world.stores, world.hod):
        if r.status in c.PENDING_STATUSES:
            act(actor, r, "approve")
    assert r.status == RS.APPROVED
    return r


class TestWorkspace:
    def step(self, user, rec_id, step, expect=200, **body):
        res = client_for(user).post(f"{API}/records/{rec_id}/steps/{step}/", body, format="json")
        assert res.status_code == expect, res.json()
        return res.json()

    def test_end_to_end(self, world):
        r = _approved_request(world)
        assert client_for(world.stores).post(f"{API}/requests/{r.pk}/start-procurement/", {}, format="json").status_code == 403
        res = client_for(world.office).post(f"{API}/requests/{r.pk}/start-procurement/", {}, format="json")
        assert res.status_code == 201, res.json()
        rec = res.json()
        assert rec["number"].startswith("PROC/") and "COMPARATIVE" in rec["required_steps"]
        r.refresh_from_db()
        assert r.status == RS.IN_PROCUREMENT
        rid = rec["id"]
        o = world.office
        self.step(o, rid, "purchase_order", expect=400, po_number="PO1", po_date=TODAY(), po_amount="1")
        self.step(o, rid, "indent", indent_number="IND/1", indent_date=TODAY())
        self.step(o, rid, "specification", specification="As per request")
        self.step(o, rid, "rfq", rfq_reference="RFQ/1", rfq_date=TODAY())
        self.step(o, rid, "quotations", expect=400)
        v1, v2 = _vendor(world, "Alpha"), _vendor(world, "Beta")
        cl = client_for(o)
        cl.post(f"{API}/records/{rid}/quotations/", {"vendor_id": v1.pk, "amount": "28000.00", "gst_amount": "0"}, format="json")
        cl.post(f"{API}/records/{rid}/quotations/", {"vendor_id": v2.pk, "amount": "27000.00", "gst_amount": "0"}, format="json")
        comp = cl.get(f"{API}/records/{rid}/quotations/").json()["results"]
        assert comp[0]["vendor"] == "Beta" and comp[0]["is_lowest_compliant"]
        xlsx = cl.get(f"{API}/records/{rid}/quotations/?export=xlsx")
        assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK"
        self.step(o, rid, "quotations")
        self.step(o, rid, "comparative")
        alpha_q = next(x for x in comp if x["vendor"] == "Alpha")["quotation_id"]
        self.step(o, rid, "vendor_selection", expect=400, quotation_id=alpha_q)
        self.step(o, rid, "vendor_selection", quotation_id=comp[0]["quotation_id"])
        self.step(o, rid, "purchase_order", expect=400, po_number="PO/1", po_date=TODAY(), po_amount="33000.01")
        assert self.step(o, rid, "purchase_order", po_number="PO/1", po_date=TODAY(), po_amount="27000.00")["status"] == PRS.PO_ISSUED
        self.step(o, rid, "delivery", delivery_date=TODAY(), delivery_challan_number="DC-9")
        r.refresh_from_db()
        assert r.status == RS.AWAITING_INVOICE
        self.step(o, rid, "inspection", expect=400, inspection_date=TODAY(), inspection_result="REJECTED")
        self.step(o, rid, "inspection", inspection_date=TODAY(), inspection_result="ACCEPTED")
        bill = {
            "vendor_id": v2.pk, "invoice_number": "BETA/77", "invoice_date": TODAY(),
            "lines": [{"description": "Consumables", "quantity": "1", "unit_price": "27000.00", "gst_rate": "0"}],
        }
        import json

        res = cl.post(f"{API}/records/{rid}/invoices/", {"payload": json.dumps(bill), "files": [pdf_upload()]}, format="multipart")
        assert res.status_code == 201, res.json()
        assert res.json()["status"] == PRS.COMPLETED, res.json()["blockers"]
        r.refresh_from_db()
        assert r.status == RS.COMPLETED

    def test_from_requirements_and_cancel(self, world):
        a = submitted(world, estimated_unit_cost="1000.00")
        p = client_for(world.office).post(
            f"{API}/proposals/",
            {"department_id": world.dept.pk, "funding_type": "PLAN", "financial_year": FY, "title": "P", "requirement_ids": [a["id"]]},
            format="json",
        ).json()
        client_for(world.office).post(f"{API}/proposals/{p['id']}/send/", {}, format="json")
        client_for(world.hod).post(f"{API}/proposals/{p['id']}/decide/", {"decision": "APPROVE"}, format="json")
        res = client_for(world.office).post(f"{API}/records/from-requirements/", {"requirement_ids": [a["id"]], "title": "Plan buy"}, format="json")
        assert res.status_code == 201, res.json()
        rec = res.json()
        assert rec["origin"] == c.RequestOrigin.PLAN_REQUIREMENT and rec["approved_amount"] == "2000.00"
        assert rec["proposal_id"] == p["id"] and rec["financial_year"] == FY
        assert PlanRequirement.objects.get(pk=a["id"]).status == RQ.PROCUREMENT_IN_PROGRESS
        client_for(world.office).post(f"{API}/records/from-requirements/", {"requirement_ids": [a["id"]], "title": "again"}, format="json")
        assert ProcurementRecord.objects.filter(proposal_id=p["id"]).count() == 1
        assert client_for(world.office).post(f"{API}/records/{rec['id']}/cancel/", {}, format="json").status_code == 400
        assert client_for(world.office).post(f"{API}/records/{rec['id']}/cancel/", {"reason": "Re-tender"}, format="json").json()["status"] == PRS.CANCELLED
        assert PlanRequirement.objects.get(pk=a["id"]).status == RQ.APPROVED

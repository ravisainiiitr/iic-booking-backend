import csv
import io
import json
import uuid
from decimal import Decimal

import pytest
from django.utils import timezone

from iic_booking.procurement_management import constants as c
from iic_booking.procurement_management.fy import fy_label
from iic_booking.procurement_management.models import ProcurementRecord

from .conftest import API, act, category, client_for, line, new_request, pdf_upload

pytestmark = pytest.mark.django_db
FY = fy_label()
TODAY = lambda: timezone.localdate().isoformat()  # noqa: E731


def allocate(user, world, expect=201, **extra):
    body = {"department_id": world.dept.pk, "financial_year": FY, "funding_type": "OTHER", "amount": "100000.00", "reference": "DEPT/BUD/1", **extra}
    res = client_for(user).post(f"{API}/budgets/", body, format="json")
    assert res.status_code == expect, res.json()
    return res.json()


def summary(user, world, expect=200, **params):
    res = client_for(user).get(f"{API}/budgets/summary/", {"department_id": world.dept.pk, "financial_year": FY, **params})
    assert res.status_code == expect, res.json()
    return res.json()


def other_row(data):
    return next(r for r in data["rows"] if r["funding_type"] == "OTHER")


def small_purchase(world, price="1500.00", title="Glassware"):
    body = {
        "department_id": world.dept.pk, "category_id": category(world.dept, "CONSUMABLE").pk, "title": title,
        "invoice": {"vendor_name": "Local Store", "invoice_number": f"B-{uuid.uuid4().hex[:6]}", "invoice_date": TODAY(),
                    "lines": [{"description": "Beakers", "quantity": "1", "unit_price": price, "gst_rate": "0"}]},
    }
    res = client_for(world.office).post(f"{API}/small-purchases/", {"payload": json.dumps(body), "files": [pdf_upload()]}, format="multipart")
    assert res.status_code == 201, res.json()
    return res.json()


class TestBudget:
    def test_allocation_rules(self, world):
        row = allocate(world.office, world)
        assert row["amount"] == "100000.00"
        assert allocate(world.office, world, expect=400, financial_year="2026-28")["code"] == "invalid_fy"
        allocate(world.operator, world, expect=403)
        allocate(world.hod, world, expect=403)
        cl = client_for(world.office)
        assert cl.patch(f"{API}/budgets/{row['id']}/", {"amount": "120000"}, format="json").json()["code"] == "reason_required"
        assert cl.patch(f"{API}/budgets/{row['id']}/", {"amount": "120000", "reason": "Revised"}, format="json").json()["amount"] == "120000.00"
        assert other_row(summary(world.hod, world))["budget"] == "120000.00"
        assert cl.post(f"{API}/budgets/{row['id']}/archive/", {"reason": "Wrong head"}, format="json").status_code == 200
        assert other_row(summary(world.hod, world))["budget"] == "0.00"
        summary(world.operator, world, expect=403)
        assert client_for(world.operator).get(f"{API}/budgets/").json()["count"] == 0

    def test_budget_vs_actual(self, world):
        allocate(world.office, world)
        r = new_request(world.operator, equipment=world.equipment, lines=[line("30000.00")], submit=True)
        for u in (world.oic, world.stores, world.hod):
            act(u, r, "approve")
        assert r.financial_year == FY and r.funding_type == "OTHER"
        sp = small_purchase(world)
        rec = client_for(world.office).post(f"{API}/requests/{r.pk}/start-procurement/", {}, format="json").json()
        ProcurementRecord.objects.filter(pk=rec["id"]).update(po_amount=Decimal("27000.00"))
        row = other_row(summary(world.office, world))
        assert row["approved"] == "30000.00"
        assert row["committed"] == "27000.00"
        assert row["purchased"] == "1500.00"
        assert row["balance"] == "71500.00" and row["utilisation_percent"] == "28.50"
        inv_id = sp["invoices"][0]["id"]
        res = client_for(world.office).post(f"{API}/invoices/{inv_id}/payments/", {"amount": "1000", "payment_date": TODAY(), "payment_reference": "UTR1"}, format="json")
        assert res.status_code == 200, res.json()
        data = summary(world.office, world)
        assert other_row(data)["paid"] == "1000.00" and data["total"]["budget"] == "100000.00"


class TestDashboard:
    def test_role_aware(self, world):
        allocate(world.office, world)
        new_request(world.operator, equipment=world.equipment, submit=True)
        op = client_for(world.operator).get(f"{API}/dashboard/").json()
        assert op["my_requests"] == {"PENDING_OIC": 1} and "requests_by_status" not in op and "budget" not in op
        assert client_for(world.oic).get(f"{API}/dashboard/").json()["pending_approvals"] == 1
        office = client_for(world.office).get(f"{API}/dashboard/").json()
        assert office["requests_by_status"] == {"PENDING_OIC": 1} and office["budget"]["total"]["budget"] == "100000.00"
        assert "budget" in client_for(world.hod).get(f"{API}/dashboard/").json()
        assert client_for(world.outsider).get(f"{API}/dashboard/").status_code == 403


class TestReports:
    def test_every_report_renders(self, world):
        small_purchase(world)
        cl = client_for(world.auditor)
        names = cl.get(f"{API}/reports/").json()["reports"]
        assert {"requests", "purchases", "invoices", "vendor-spend", "stock", "assets", "amc", "budget", "audit"} <= set(names)
        for name in names:
            res = cl.get(f"{API}/reports/{name}/", {"department_id": world.dept.pk})
            assert res.status_code == 200, (name, res.json())
            body = res.json()
            assert body["headers"] and all(len(r) == len(body["headers"]) for r in body["rows"])
        assert cl.get(f"{API}/reports/invoices/").json()["count"] == 1
        assert cl.get(f"{API}/reports/vendor-spend/").json()["rows"][0][0] == "Local Store"

    def test_formats_and_guards(self, world):
        new_request(world.operator, equipment=world.equipment, title="=HYPERLINK(\"http://x\")")
        cl = client_for(world.hod)
        res = cl.get(f"{API}/reports/requests/", {"export": "csv"})
        assert res.status_code == 200 and res["Content-Type"].startswith("text/csv")
        rows = list(csv.reader(io.StringIO(res.content.decode("utf-8-sig"))))
        assert any(cell.startswith("'=HYPERLINK") for row in rows for cell in row)
        assert cl.get(f"{API}/reports/purchases/", {"export": "xlsx"}).content[:2] == b"PK"
        assert cl.get(f"{API}/reports/budget/", {"export": "pdf"}).content.startswith(b"%PDF")
        assert cl.get(f"{API}/reports/nope/").status_code == 404
        assert cl.get(f"{API}/reports/requests/", {"financial_year": "26-27"}).status_code == 400
        assert cl.get(f"{API}/reports/requests/", {"date_from": "2026-05-01", "date_to": "2026-04-01"}).status_code == 400
        assert client_for(world.operator).get(f"{API}/reports/requests/").status_code == 403
        assert client_for(world.oic).get(f"{API}/reports/requests/").status_code == 403
        assert client_for(world.auditor).get(f"{API}/reports/requests/", {"department_id": world.other_dept.pk}).status_code == 404


class TestTrail:
    def test_request_trail(self, world):
        r = new_request(world.operator, equipment=world.equipment, lines=[line("30000.00")], submit=True)
        for u in (world.oic, world.stores, world.hod):
            act(u, r, "approve")
        rec = client_for(world.office).post(f"{API}/requests/{r.pk}/start-procurement/", {}, format="json").json()
        res = client_for(world.auditor).get(f"{API}/trail/request/{r.pk}/")
        assert res.status_code == 200, res.json()
        body = res.json()
        actions = [a["action"] for a in body["audit"]]
        assert "request.created" in actions and "request.approve" in actions and "procurement.started" in actions
        assert body["related"]["records"][0]["id"] == rec["id"]
        assert [h["action"] for h in body["object"]["history"]][:2] == ["SUBMIT", "APPROVE"]
        assert client_for(world.auditor).get(f"{API}/trail/record/{rec['id']}/").json()["related"]["purchase_request"]["id"] == r.pk
        assert client_for(world.operator).get(f"{API}/trail/request/{r.pk}/").status_code == 404
        assert client_for(world.auditor).get(f"{API}/trail/unknown/{r.pk}/").status_code == 404

    def test_asset_and_invoice_trail(self, world):
        sp = small_purchase(world)
        inv = client_for(world.auditor).get(f"{API}/trail/invoice/{sp['invoices'][0]['id']}/").json()
        assert inv["object"]["invoice_number"] == sp["invoices"][0]["invoice_number"]
        assert [a["action"] for a in inv["audit"]] == ["invoice.recorded"]
        assert inv["related"]["record"]["id"] == sp["id"]

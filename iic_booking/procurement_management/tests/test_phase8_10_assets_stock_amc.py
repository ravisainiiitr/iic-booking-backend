import json
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.utils import timezone

from iic_booking.procurement_management import amc as amc_service
from iic_booking.procurement_management import config_service
from iic_booking.procurement_management import constants as c
from iic_booking.procurement_management.models import (
    AMCServiceRecord,
    Asset,
    AssetStatusHistory,
    Item,
    ProcurementAuditLog,
    StockBalance,
    StockTransaction,
)

from .conftest import API, act, category, client_for, line, new_request, pdf_upload

pytestmark = pytest.mark.django_db
A = c.AssetStatus
TODAY = timezone.localdate


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def fake(recipients, **kwargs):
        calls.append({"to": {u.pk for u in recipients}, **kwargs})

    monkeypatch.setattr("iic_booking.communication.in_app.notify_in_app", fake)
    return calls


def make_item(world, cat="CONSUMABLE", name="Ethanol 500 ml"):
    return Item.objects.create(
        department=world.dept, code=f"ITM-{uuid.uuid4().hex[:6]}", name=name, category=category(world.dept, cat), uom="Btl"
    )


def stock_post(user, world, item, tx_type, qty, expect=201, **extra):
    body = {"department_id": world.dept.pk, "item_id": item.pk, "tx_type": tx_type, "quantity": qty, **extra}
    res = client_for(user).post(f"{API}/stock/transactions/", body, format="json")
    assert res.status_code == expect, res.json()
    return res.json()


def balance(world, item):
    row = StockBalance.objects.filter(department=world.dept, item=item, laboratory__isnull=True).first()
    return row.quantity if row else Decimal("0")


class TestStock:
    def test_opening_adjust_and_guards(self, world):
        item = make_item(world)
        out = stock_post(world.stores, world, item, "OPENING", "10")
        assert out["number"].startswith("STK/") and out["balance_after"] == "10.000"
        assert stock_post(world.stores, world, item, "OPENING", "5", expect=400)["code"] == "opening_exists"
        assert stock_post(world.stores, world, item, "ADJUSTMENT_OUT", "2", expect=400)["code"] == "required"
        res = stock_post(world.stores, world, item, "ADJUSTMENT_OUT", "11", expect=400, remarks="Breakage")
        assert res["code"] == "insufficient_stock" and res["available"] == "10.000"
        stock_post(world.stores, world, item, "ADJUSTMENT_OUT", "3", remarks="Breakage")
        assert balance(world, item) == Decimal("7.000")
        assert sum(t.signed_quantity for t in StockTransaction.objects.filter(item=item)) == Decimal("7.000")
        assert ProcurementAuditLog.objects.filter(action="stock.adjustment_out").exists()

    def test_permissions_and_non_stock_items(self, world):
        item = make_item(world)
        stock_post(world.operator, world, item, "OPENING", "1", expect=403)
        stock_post(world.hod, world, item, "OPENING", "1", expect=403)
        tool = make_item(world, cat="NON_CONSUMABLE", name="Spanner")
        assert stock_post(world.stores, world, tool, "OPENING", "1", expect=400)["code"] == "not_stock_item"
        other = Item.objects.create(
            department=world.other_dept, code=f"ITM-{uuid.uuid4().hex[:6]}", name="X", category=category(world.other_dept, "CONSUMABLE")
        )
        stock_post(world.stores, world, other, "OPENING", "1", expect=404)

    def test_visibility(self, world):
        item = make_item(world)
        stock_post(world.stores, world, item, "OPENING", "4")
        assert client_for(world.operator).get(f"{API}/stock/balances/").json()["count"] == 1
        assert client_for(world.operator).get(f"{API}/stock/transactions/").json()["count"] == 0
        assert client_for(world.auditor).get(f"{API}/stock/transactions/").json()["count"] == 1
        assert client_for(world.other_operator).get(f"{API}/stock/balances/").json()["count"] == 0

    def test_levels_and_low_filter(self, world):
        item = make_item(world)
        stock_post(world.stores, world, item, "OPENING", "4")
        res = client_for(world.stores).post(f"{API}/stock/levels/", {"department_id": world.dept.pk, "item_id": item.pk, "min_level": "5", "reorder_level": "6"}, format="json")
        assert res.status_code == 200 and res.json()["below_min"] and res.json()["reorder_due"]
        low = client_for(world.stores).get(f"{API}/stock/balances/?low=1").json()
        assert [r["item"]["id"] for r in low["results"]] == [item.pk]

    def test_issue_against_request_uses_ledger(self, world):
        item = make_item(world)
        r = new_request(world.operator, equipment=world.equipment, lines=[line("100.00", qty="3", item_id=item.pk)], submit=True)
        act(world.oic, r, "approve")
        act(world.stores, r, "stores-review", decision="AVAILABLE")
        res = act(world.stores, r, "issue", expect=400)
        assert res.json()["code"] == "insufficient_stock"
        assert r.lines.get().issued_quantity == 0
        stock_post(world.stores, world, item, "OPENING", "5")
        act(world.stores, r, "issue")
        assert r.status == c.RequestStatus.ISSUED and balance(world, item) == Decimal("2.000")
        tx = StockTransaction.objects.get(item=item, tx_type="ISSUE")
        assert tx.purchase_request_id == r.pk and tx.issued_to_id == world.operator.pk

    def test_bill_receipt_adds_stock(self, world):
        item = make_item(world)
        body = {
            "department_id": world.dept.pk, "category_id": category(world.dept, "CONSUMABLE").pk, "title": "Ethanol",
            "invoice": {"vendor_name": "Chem Mart", "invoice_number": "CM-1", "invoice_date": TODAY().isoformat(),
                        "lines": [{"item_id": item.pk, "quantity": "6", "unit_price": "250.00", "gst_rate": "0"}]},
        }
        res = client_for(world.office).post(f"{API}/small-purchases/", {"payload": json.dumps(body), "files": [pdf_upload()]}, format="multipart")
        assert res.status_code == 201, res.json()
        assert balance(world, item) == Decimal("6.000")
        tx = StockTransaction.objects.get(item=item)
        assert tx.tx_type == "RECEIPT" and tx.unit_cost == Decimal("250.00") and tx.procurement_record_id == res.json()["id"]


def register(user, expect=201, **body):
    res = client_for(user).post(f"{API}/assets/", body, format="json")
    assert res.status_code == expect, res.json()
    return res.json()


def asset_body(world, **extra):
    return {"equipment_id": world.equipment.pk, "category_id": category(world.dept, "MINOR_ASSET").pk,
            "description": "UPS 2 kVA", "cost": "18000.00", "purchase_date": TODAY().isoformat(), **extra}


class TestAssets:
    def test_register_batch_and_guards(self, world):
        out = register(world.stores, **asset_body(world, count=2, serial_numbers=["S1", "S2"]))["results"]
        assert len(out) == 2 and all(a["number"].startswith("AST/") for a in out)
        assert out[0]["status"] == A.IN_STORE
        assert AssetStatusHistory.objects.filter(asset_id=out[0]["id"]).count() == 1
        assert register(world.stores, expect=400, **asset_body(world, serial_number="s1"))["code"] == "duplicate_serial"
        assert register(world.stores, expect=400, **asset_body(world, count=2, serial_numbers=["A"]))["code"] == "serial_count"
        cons = asset_body(world, category_id=category(world.dept, "CONSUMABLE").pk)
        assert register(world.stores, expect=400, **cons)["code"] == "not_asset_category"
        register(world.operator, expect=403, **asset_body(world))
        register(world.stores, expect=400, **asset_body(world, purchase_date=(TODAY() + timedelta(days=1)).isoformat()))

    def test_visibility(self, world):
        aid = register(world.stores, **asset_body(world))["results"][0]["id"]
        assert client_for(world.operator).get(f"{API}/assets/{aid}/").status_code == 200
        assert client_for(world.operator2).get(f"{API}/assets/{aid}/").status_code == 404
        assert client_for(world.other_oic).get(f"{API}/assets/{aid}/").status_code == 404
        assert client_for(world.operator2).get(f"{API}/assets/").json()["count"] == 0
        x = client_for(world.stores).get(f"{API}/assets/?export=xlsx")
        assert x.status_code == 200 and x.content[:2] == b"PK"

    def test_register_from_record_completes_it(self, world):
        body = {
            "department_id": world.dept.pk, "category_id": category(world.dept, "MINOR_ASSET").pk, "title": "Balance",
            "invoice": {"vendor_name": "Lab Mart", "invoice_number": "LM-9", "invoice_date": TODAY().isoformat(),
                        "lines": [{"description": "Balance", "quantity": "1", "unit_price": "1800.00", "gst_rate": "0"}]},
        }
        rec = client_for(world.office).post(f"{API}/small-purchases/", {"payload": json.dumps(body), "files": [pdf_upload()]}, format="multipart").json()
        assert rec["blockers"] == ["asset_entry_missing"]
        out = register(world.stores, procurement_record_id=rec["id"], serial_number="BAL-1")["results"][0]
        assert out["cost"] == "1800.00" and out["procurement_record"]["id"] == rec["id"] and out["description"] == "Balance"
        detail = client_for(world.office).get(f"{API}/records/{rec['id']}/").json()
        assert detail["status"] == c.ProcurementRecordStatus.COMPLETED and detail["assets"][0]["id"] == out["id"]

    def test_status_rules(self, world):
        aid = register(world.stores, **asset_body(world))["results"][0]["id"]
        url = f"{API}/assets/{aid}/status/"
        st = client_for(world.stores)
        assert st.post(url, {"status": "IN_USE"}, format="json").json()["code"] == "reason_required"
        assert st.post(url, {"status": A.TEMPORARILY_TRANSFERRED, "reason": "x"}, format="json").json()["code"] == "use_transfer"
        assert st.post(url, {"status": "DISPOSED", "reason": "x"}, format="json").json()["code"] == "invalid_transition"
        oic = client_for(world.oic)
        assert oic.post(url, {"status": "UNDER_REPAIR", "reason": "Fan noise"}, format="json").status_code == 200
        assert oic.post(url, {"status": "CONDEMNED", "reason": "x"}, format="json").status_code == 403
        assert client_for(world.oic2).post(url, {"status": "IN_USE", "reason": "x"}, format="json").status_code == 404
        assert st.post(url, {"status": "CONDEMNED", "reason": "Beyond repair"}, format="json").status_code == 200
        assert st.post(url, {"status": "DISPOSED", "reason": "Auctioned"}, format="json").status_code == 200
        assert st.patch(f"{API}/assets/{aid}/", {"location": "Store"}, format="json").json()["code"] == "asset_final"
        hist = client_for(world.stores).get(f"{API}/assets/{aid}/").json()["status_history"]
        assert [h["to_status"] for h in hist] == [A.IN_STORE, A.UNDER_REPAIR, A.CONDEMNED, A.DISPOSED]

    def test_cost_change_needs_reason(self, world):
        aid = register(world.stores, **asset_body(world))["results"][0]["id"]
        st = client_for(world.stores)
        assert st.patch(f"{API}/assets/{aid}/", {"cost": "17000.00"}, format="json").status_code == 400
        res = st.patch(f"{API}/assets/{aid}/", {"cost": "17000.00", "reason": "Credit note"}, format="json")
        assert res.status_code == 200 and res.json()["cost"] == "17000.00"
        log = ProcurementAuditLog.objects.filter(action="asset.updated", object_id=str(aid)).get()
        assert log.old_value == {"cost": "18000.00"} and log.reason == "Credit note"

    def test_temporary_transfer_cycle(self, world, sent):
        aid = register(world.stores, **asset_body(world, location="Room 101"))["results"][0]["id"]
        oic = client_for(world.oic)
        url = f"{API}/assets/{aid}/transfers/"
        assert oic.post(url, {"transfer_type": "TEMPORARY", "to_equipment_id": world.equipment2.pk, "reason": "Loan"}, format="json").status_code == 400
        assert oic.post(url, {"transfer_type": "TEMPORARY", "reason": "Loan"}, format="json").json()["code"] == "destination_required"
        res = oic.post(url, {"transfer_type": "TEMPORARY", "to_equipment_id": world.equipment2.pk, "to_location": "Room 202",
                             "reason": "Loan for XRD", "expected_return_date": (TODAY() + timedelta(days=7)).isoformat()}, format="json")
        assert res.status_code == 201, res.json()
        t = res.json()
        assert t["number"].startswith("TRF/") and sent
        assert oic.post(url, {"transfer_type": "PERMANENT", "to_location": "X", "reason": "y"}, format="json").json()["code"] == "transfer_open"
        assert oic.post(f"{API}/transfers/{t['id']}/decide/", {"decision": "APPROVE"}, format="json").status_code == 403
        st = client_for(world.stores)
        assert st.post(f"{API}/transfers/{t['id']}/complete/", {}, format="json").json()["code"] == "invalid_status"
        assert st.post(f"{API}/transfers/{t['id']}/decide/", {"decision": "APPROVE"}, format="json").json()["status"] == "APPROVED"
        assert st.post(f"{API}/transfers/{t['id']}/complete/", {}, format="json").json()["status"] == "COMPLETED"
        a = Asset.objects.get(pk=aid)
        assert a.equipment_id == world.equipment2.pk and a.location == "Room 202" and a.status == A.TEMPORARILY_TRANSFERRED
        assert st.post(f"{API}/assets/{aid}/status/", {"status": "IN_USE", "reason": "x"}, format="json").json()["code"] == "transfer_open"
        assert st.post(f"{API}/transfers/{t['id']}/return/", {"note": "Back"}, format="json").json()["status"] == "RETURNED"
        a.refresh_from_db()
        assert a.equipment_id == world.equipment.pk and a.location == "Room 101" and a.status == A.IN_STORE

    def test_reject_and_cancel(self, world):
        aid = register(world.stores, **asset_body(world))["results"][0]["id"]
        office = client_for(world.office)
        t = office.post(f"{API}/assets/{aid}/transfers/", {"transfer_type": "PERMANENT", "to_location": "Annex", "reason": "Space"}, format="json").json()
        assert office.post(f"{API}/transfers/{t['id']}/decide/", {"decision": "APPROVE"}, format="json").status_code == 403
        st = client_for(world.stores)
        assert st.post(f"{API}/transfers/{t['id']}/decide/", {"decision": "REJECT"}, format="json").status_code == 400
        assert st.post(f"{API}/transfers/{t['id']}/decide/", {"decision": "REJECT", "note": "No space"}, format="json").json()["status"] == "REJECTED"
        t2 = office.post(f"{API}/assets/{aid}/transfers/", {"transfer_type": "PERMANENT", "to_location": "Annex", "reason": "Space"}, format="json").json()
        assert office.post(f"{API}/transfers/{t2['id']}/cancel/", {"reason": "Changed plan"}, format="json").json()["status"] == "CANCELLED"


def amc_body(world, days=30, **extra):
    return {"equipment_id": world.equipment.pk, "contract_type": "AMC", "start_date": (TODAY() - timedelta(days=335)).isoformat(),
            "end_date": (TODAY() + timedelta(days=days)).isoformat(), "contract_value": "50000.00", "gst_amount": "9000.00", **extra}


def amc_post(user, body, files=None, expect=201):
    cl = client_for(user)
    if files is not None:
        res = cl.post(f"{API}/amc/", {"payload": json.dumps(body), "files": files}, format="multipart")
    else:
        res = cl.post(f"{API}/amc/", body, format="json")
    assert res.status_code == expect, res.json()
    return res.json()


class TestAMC:
    def test_create_with_contract_and_visibility(self, world):
        out = amc_post(world.office, amc_body(world), files=[pdf_upload("contract.pdf")])
        assert out["number"].startswith("AMC/") and out["total_value"] == "59000.00" and out["status"] == "ACTIVE"
        assert out["days_left"] == 30 and out["documents"][0]["doc_type"] == "AMC_CONTRACT"
        assert client_for(world.operator).get(f"{API}/amc/{out['id']}/").status_code == 200
        assert client_for(world.operator2).get(f"{API}/amc/{out['id']}/").status_code == 404
        assert client_for(world.office).get(f"{API}/amc/?expiring_within=45").json()["count"] == 1
        assert client_for(world.office).get(f"{API}/amc/?expiring_within=10").json()["count"] == 0

    def test_guards(self, world):
        amc_post(world.stores, amc_body(world), expect=403)
        amc_post(world.oic, amc_body(world), expect=403)
        bad = amc_body(world, start_date=TODAY().isoformat(), end_date=(TODAY() - timedelta(days=1)).isoformat())
        assert amc_post(world.office, bad, expect=400)["code"] == "invalid_dates"
        amc_post(world.office, amc_body(world, equipment_id=world.other_equipment.pk), expect=403)
        config_service.update_config(world.admin, world.dept, {"amc_enabled": False})
        assert amc_post(world.office, amc_body(world), expect=400)["code"] == "feature_disabled"

    def test_reminders_once_and_expiry(self, world, sent):
        due = amc_post(world.office, amc_body(world, days=30))
        amc_post(world.office, amc_body(world, days=200))
        lapsed = AMCServiceRecord.objects.create(
            number="AMC/TEST/1", department=world.dept, equipment=world.equipment2, start_date=TODAY() - timedelta(days=400),
            end_date=TODAY() - timedelta(days=1), contract_value=Decimal("1.00"), total_value=Decimal("1.00"), created_by=world.office,
        )
        assert amc_service.send_reminders() == {"expired": 1, "reminded": 1}
        reminder = [x for x in sent if x["event"] == "amc_expiring"]
        assert len(reminder) == 1 and world.oic.pk in reminder[0]["to"] and world.office.pk in reminder[0]["to"]
        assert amc_service.send_reminders() == {"expired": 0, "reminded": 0}
        lapsed.refresh_from_db()
        assert lapsed.status == "EXPIRED"
        assert AMCServiceRecord.objects.get(pk=due["id"]).reminder_sent_at is not None
        call_command("procurement_amc_reminders", "--today", TODAY().isoformat())

    def test_renew_and_cancel(self, world):
        old = amc_post(world.office, amc_body(world))
        cl = client_for(world.office)
        bad = {"start_date": (TODAY() - timedelta(days=400)).isoformat(), "end_date": (TODAY() + timedelta(days=400)).isoformat(), "contract_value": "52000"}
        assert cl.post(f"{API}/amc/{old['id']}/renew/", bad, format="json").status_code == 400
        good = {"start_date": (TODAY() + timedelta(days=31)).isoformat(), "end_date": (TODAY() + timedelta(days=395)).isoformat(), "contract_value": "52000"}
        res = cl.post(f"{API}/amc/{old['id']}/renew/", good, format="json")
        assert res.status_code == 201, res.json()
        assert res.json()["renewed_from_id"] == old["id"]
        assert AMCServiceRecord.objects.get(pk=old["id"]).status == "RENEWED"
        assert cl.post(f"{API}/amc/{old['id']}/renew/", good, format="json").json()["code"] == "invalid_status"
        new_id = res.json()["id"]
        assert cl.post(f"{API}/amc/{new_id}/cancel/", {}, format="json").json()["code"] == "reason_required"
        assert cl.post(f"{API}/amc/{new_id}/cancel/", {"reason": "Vendor closed"}, format="json").json()["status"] == "CANCELLED"

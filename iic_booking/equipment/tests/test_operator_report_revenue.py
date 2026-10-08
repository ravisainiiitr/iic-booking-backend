"""Lab Operators see bookings, hours and utilization on Reports & Statistics, but no money (page or exports)."""

import csv
import io
from datetime import timedelta
from types import SimpleNamespace

import pytest
from openpyxl import load_workbook

from iic_booking.equipment.models import BookingStatus
from iic_booking.equipment.models import EquipmentManager
from iic_booking.equipment.models import EquipmentOperator
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

MONEY_WORDS = ("revenue", "charged", "spent", "amount", "cost", "₹", "financial")


@pytest.fixture
def world(egs_factory):
    f = egs_factory
    eq = f.equipment(name="Gamma SEM")
    operator = UserFactory(user_type=UserType.OPERATOR, admin_approved=True, department=f.department)
    oic = UserFactory(user_type=UserType.MANAGER, admin_approved=True, department=f.department)
    EquipmentOperator.objects.create(equipment=eq, operator=operator)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    start = f.future(days=3)
    done = f.booking(f.student(), eq, start, total_charge="250.00")
    done.status = BookingStatus.COMPLETED
    done.save(update_fields=["status"])
    f.booking(f.student(), eq, start + timedelta(hours=2), total_charge="150.00")
    period = {
        "date_from": (start.date() - timedelta(days=1)).isoformat(),
        "date_to": (start.date() + timedelta(days=1)).isoformat(),
        "equipment_id": eq.pk,
    }
    return SimpleNamespace(f=f, eq=eq, operator=operator, oic=oic, period=period)


def _export(client, report, export_format="csv", **params):
    return client.get(f"/api/exports/{report}/", {"export_format": export_format, **params})


def _workbook_text(content) -> str:
    wb = load_workbook(io.BytesIO(content))
    cells = [wb.sheetnames]
    for ws in wb.worksheets:
        cells += [c for row in ws.iter_rows(values_only=True) for c in row if c is not None]
    return " ".join(str(c) for c in cells).lower()


def _pdf_text(content) -> str:
    from pypdf import PdfReader

    return " ".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(content)).pages).lower()


def _assert_no_money(text: str):
    found = [w for w in MONEY_WORDS if w in text]
    assert not found, found


@pytest.mark.django_db
def test_booking_stats_omit_money_for_operator_only(world):
    op = world.f.client_for(world.operator).get("/api/bookings/stats/").data
    assert op["revenue_visible"] is False
    assert op["total_bookings"] == 2 and op["total_hours"] == 2.0
    for key in ("total_spent", "average_cost", "refunded_amount"):
        assert key not in op
    oic = world.f.client_for(world.oic).get("/api/bookings/stats/").data
    assert oic["revenue_visible"] is True
    assert oic["total_spent"] == 400.0 and oic["average_cost"] == 200.0 and "refunded_amount" in oic


@pytest.mark.django_db
def test_equipment_report_omits_revenue_for_operator(world):
    op = world.f.client_for(world.operator).get("/api/admin/equipment-reports/", world.period)
    assert op.status_code == 200, op.content[:300]
    assert op.data["revenue_visible"] is False
    assert "financial" not in op.data
    assert not [k for k in op.data["summary"] if k.startswith("revenue")]
    assert op.data["summary"]["total_equipment"] == 1
    assert op.data["equipment"][0]["completed_in_period"] == 1
    oic = world.f.client_for(world.oic).get("/api/admin/equipment-reports/", world.period).data
    assert oic["revenue_visible"] is True
    assert oic["summary"]["revenue_total"] == 250.0
    assert oic["financial"]["revenue_by_equipment"][0]["total"] == 250.0


@pytest.mark.django_db
@pytest.mark.parametrize("report", ["reports-statistics", "equipment-performance", "booking-statistics",
                                    "report-bookings"])
def test_operator_exports_have_no_money(world, report):
    client = world.f.client_for(world.operator)
    xlsx = _export(client, report, export_format="xlsx", **world.period)
    assert xlsx.status_code == 200, xlsx.content[:300]
    _assert_no_money(_workbook_text(xlsx.content))
    csv_res = _export(client, report, **world.period)
    assert csv_res.status_code == 200
    _assert_no_money(csv_res.content.decode("utf-8-sig").lower())
    pdf = _export(client, report, export_format="pdf", **world.period)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    _assert_no_money(_pdf_text(pdf.content))


@pytest.mark.django_db
def test_operator_report_bookings_keep_hours(world):
    res = _export(world.f.client_for(world.operator), "report-bookings")
    rows = list(csv.reader(io.StringIO(res.content.decode("utf-8-sig"))))
    assert "Hours" in rows[0] and "Amount (₹)" not in rows[0]
    assert len(rows) - 1 == 2


@pytest.mark.django_db
def test_operator_cannot_pick_a_revenue_table(world):
    res = _export(world.f.client_for(world.operator), "equipment-performance", table="revenue_department",
                  **world.period)
    assert res.status_code == 400


@pytest.mark.django_db
def test_oic_exports_keep_money(world):
    client = world.f.client_for(world.oic)
    text = _workbook_text(_export(client, "reports-statistics", export_format="xlsx", **world.period).content)
    assert "revenue by user type" in text and "total charged" in text and "refunded amount" in text
    rows = list(csv.reader(io.StringIO(_export(client, "report-bookings").content.decode("utf-8-sig"))))
    assert "Amount (₹)" in rows[0]
    pdf = _pdf_text(_export(client, "equipment-performance", export_format="pdf", **world.period).content)
    assert "revenue" in pdf


@pytest.mark.django_db
def test_legacy_report_downloads_and_monthly_email_drop_revenue_for_operator(world):
    op = world.f.client_for(world.operator)
    xlsx = op.get("/api/admin/equipment-reports/download-excel/", world.period)
    assert xlsx.status_code == 200
    _assert_no_money(_workbook_text(xlsx.content))
    pdf = op.get("/api/admin/equipment-reports/download-pdf/", world.period)
    assert pdf.status_code == 200
    _assert_no_money(_pdf_text(pdf.content))
    oic_pdf = world.f.client_for(world.oic).get("/api/admin/equipment-reports/download-pdf/", world.period)
    assert "revenue" in _pdf_text(oic_pdf.content)


@pytest.mark.django_db
def test_monthly_report_email_attachment_per_recipient(world, mailoutbox, monkeypatch):
    from iic_booking.equipment import report_exports
    from iic_booking.equipment.tasks import send_oic_monthly_reports

    calls = []
    real = report_exports.build_report_pdf

    def spy(**kwargs):
        calls.append(kwargs["include_revenue"])
        return real(**kwargs)

    monkeypatch.setattr(report_exports, "build_report_pdf", spy)
    month = world.f.future(days=3).strftime("%Y-%m")
    sent = send_oic_monthly_reports(target_month=month)
    assert sent >= 2
    assert sorted(calls) == [False, True]
    by_recipient = {m.to[0]: m.attachments[0][1] for m in mailoutbox if m.attachments}
    assert "revenue" not in _pdf_text(by_recipient[world.operator.email])
    assert "revenue" in _pdf_text(by_recipient[world.oic.email])

"""Proforma Invoice PDF: the legal fields, the GST split by place of supply, the watermark and page numbers."""

import io
import os
from datetime import datetime
from decimal import Decimal

import pytest
from django.utils import timezone
from pypdf import PdfReader

from iic_booking.equipment.document_exports import build_proforma_invoice_multi_pdf
from iic_booking.equipment.document_exports import build_proforma_invoice_pdf
from iic_booking.equipment.proforma_pdf import inr
from iic_booking.equipment.proforma_pdf import pan_from_gstin
from iic_booking.equipment.proforma_pdf import state_code_for
from iic_booking.users.models import ExternalBillingProfile
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db
WHEN = timezone.make_aware(datetime(2026, 11, 11, 14, 30))


def _line(name="FE-SEM", code="IIC-SEM-01", base="1000.00", gst="180.00"):
    total = Decimal(base) + Decimal(gst)
    return {
        "equipment_name": name, "equipment_code": code,
        "input_labels_and_values": {"Number of samples": 4, "Coating required?": "No", "Sample type": "Powder"},
        "charge_breakdown": [{"description": "Analysis charge (4 x 250)", "amount": base}],
        "base_charge": base, "gst_amount": gst, "total_charge": str(total),
    }


def _pdf(user, lines):
    sub = sum(Decimal(li["base_charge"]) for li in lines)
    gst = sum(Decimal(li["gst_amount"]) for li in lines)
    pdf = build_proforma_invoice_multi_pdf(user=user, request_date_time=WHEN, line_items=lines,
                                           subtotal=str(sub), total_gst=str(gst), total_amount=str(sub + gst))
    sample_dir = os.environ.get("PI_SAMPLE_DIR")
    if sample_dir:
        name = os.environ.get("PYTEST_CURRENT_TEST", "pi").split("::")[-1].split(" ")[0]
        with open(os.path.join(sample_dir, f"{name}.pdf"), "wb") as fh:
            fh.write(pdf)
    reader = PdfReader(io.BytesIO(pdf))
    return reader, "\n".join(page.extract_text() for page in reader.pages)


def _external(state="", gstin="", **kw):
    user = UserFactory(user_type=UserType.INSTITUTE, name="Anita Sharma", **kw)
    ExternalBillingProfile.objects.update_or_create(user=user, defaults=dict(
        billing_name="Shivalik Materials Pvt Ltd", gstin=gstin, billing_address_line1="Plot 12, Industrial Area",
        billing_city="Haridwar" if not state or state == "Uttarakhand" else "New Delhi", billing_pincode="249403",
        billing_state=state))
    return user


def test_intra_state_has_legal_fields_and_cgst_sgst():
    reader, text = _pdf(_external(state="Uttarakhand"), [_line()])
    for needle in ("PROFORMA INVOICE", "NOT A TAX INVOICE", "PROFORMA NO.", "IIC-PI-20261111-", "DATE OF ISSUE",
                   "VALID UNTIL", "11 Dec 2026", "PLACE OF SUPPLY", "Uttarakhand (05)", "Shivalik Materials Pvt Ltd",
                   "Attn: Anita Sharma", "Unregistered", "998346", "CGST @ 9%", "SGST @ 9%", "1,180.00",
                   "Rupees One Thousand One Hundred Eighty Only", "Authorised Signatory", "Number of samples: 4",
                   "Page 1 of 1"):
        assert needle in text, needle
    assert "IGST" not in text.replace("IGST applies", "")
    assert "Coating required" not in text  # a "No" answer to an optional extra is left out


def test_inter_state_buyer_gstin_gives_igst():
    _, text = _pdf(_external(gstin="07AAACS1234F1Z5"), [_line()])
    assert "Delhi (07)" in text
    assert "07AAACS1234F1Z5" in text
    assert "IGST @ 18%" in text
    assert "CGST @" not in text


def test_guest_and_no_gst():
    _, text = _pdf(None, [_line(base="500.00", gst="0.00")])
    assert "Guest (not signed in)" in text
    assert "GST is not charged" in text
    assert "500.00" in text


def test_many_lines_run_over_pages_with_numbers():
    reader, text = _pdf(_external(state="Uttarakhand"), [_line(name=f"Instrument {i}", code=f"EQ-{i}") for i in range(25)])
    assert len(reader.pages) >= 2
    assert f"Page 2 of {len(reader.pages)}" in text
    assert "29,500.00" in text  # 25 x 1,180


def test_single_equipment_estimate():
    user = _external(state="Uttar Pradesh")
    pdf = build_proforma_invoice_pdf(data={"equipment_name": "XRD", "equipment_code": "IIC-XRD", "base_charge": "2000",
                                           "gst_amount": "360", "total_charge": "2360"},
                                     billing_profile=ExternalBillingProfile.objects.get(user=user))
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
    assert "XRD (IIC-XRD)" in text
    assert "Uttar Pradesh (09)" in text
    assert "IGST @ 18%" in text
    assert "2,360.00" in text


def test_helpers():
    assert inr("1234567.5") == "12,34,567.50"
    assert inr("999") == "999.00"
    assert state_code_for(state="uttaranchal") == "05"
    assert state_code_for(gstin="27AAACS1234F1Z5", state="Delhi") == "27"  # the GSTIN wins
    assert pan_from_gstin("05AAALI0033R1Z5") == "AAALI0033R"
    assert pan_from_gstin("bad") == ""

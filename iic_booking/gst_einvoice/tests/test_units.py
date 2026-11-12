"""GSTIN check digit, GST split and rounding, amount in words, numbering, the e-invoice JSON checks, and the sandbox
guard (no production host can be reached)."""

from datetime import date
from decimal import Decimal

import pytest

from iic_booking.gst_einvoice import conf, numbering, tax
from iic_booking.gst_einvoice.gstin import check_character, validate


def test_gstin_check_digit():
    assert validate("05AAALI0033R1Z5") == (True, "")  # SRIC, IIT Roorkee
    assert validate("07AAACX0000X1Z2")[0]
    assert validate("05aaali0033r1z5 ")[0]  # trimmed, upper-cased
    ok, why = validate("05AAALI0033R1Z6")
    assert not ok and "check character" in why
    assert not validate("99AAALI0033R1Z" + check_character("99AAALI0033R1Z"))[0]  # no state 99
    assert not validate("05AAALI0033R1Y5")[0]
    assert not validate("")[0]


def test_intra_state_splits_cgst_and_sgst():
    r = tax.calculate([tax.LineInput("FE-SEM", "998397", Decimal("18"), Decimal("9000"))], supplier_state="05",
                      place_of_supply="05")
    assert not r.inter_state
    assert (r.cgst, r.sgst, r.igst, r.total) == (Decimal("810.00"), Decimal("810.00"), Decimal("0.00"), Decimal("10620.00"))


def test_inter_state_is_igst_and_rounds_to_the_rupee():
    r = tax.calculate([tax.LineInput("XRD", "998397", Decimal("18"), Decimal("1234.55"))], supplier_state="05",
                      place_of_supply="07")
    assert r.inter_state and r.cgst == r.sgst == Decimal("0.00")
    assert r.igst == Decimal("222.22")
    assert r.total == Decimal("1457.00") and r.round_off == Decimal("0.23")


def test_rate_and_quantity_limits():
    with pytest.raises(ValueError):
        tax.calculate([tax.LineInput("x", "998397", Decimal("40"), Decimal("1"))], supplier_state="05", place_of_supply="05")
    with pytest.raises(ValueError):
        tax.calculate([], supplier_state="05", place_of_supply="05")


def test_amount_in_words():
    assert tax.in_words(Decimal("10620")) == "INR Ten Thousand Six Hundred Twenty Only"
    assert tax.in_words(Decimal("1234567.50")) == "INR Twelve Lakh Thirty Four Thousand Five Hundred Sixty Seven and Fifty Paise Only"


def test_financial_year():
    assert numbering.financial_year(date(2026, 11, 11)) == "26-27"
    assert numbering.financial_year(date(2027, 3, 31)) == "26-27"
    assert numbering.financial_year(date(2027, 4, 1)) == "27-28"


@pytest.mark.django_db
def test_numbers_are_continuous_and_restart_each_april():
    from django.db import transaction

    got = []
    for _ in range(3):
        with transaction.atomic():
            got.append(numbering.allocate(date(2026, 11, 11))[1])
    assert got == ["EBP/26-27/0001", "EBP/26-27/0002", "EBP/26-27/0003"]
    with transaction.atomic():
        assert numbering.allocate(date(2027, 4, 2))[1] == "EBP/27-28/0001"  # restarts each April


@pytest.mark.parametrize("url", [
    "https://einvoice1.gst.gov.in/eicore/v1.03/Invoice", "https://api.einvoice6.gst.gov.in/x",
    "https://einv-api.nic.in/x", "http://einv-apisandbox.nic.in/x", "https://example.org/x",
])
def test_only_the_nic_sandbox_can_be_called(url):
    with pytest.raises(conf.ProductionEndpointRefused):
        conf.assert_sandbox_url(url)
    assert conf.assert_sandbox_url("https://einv-apisandbox.nic.in/eicore/v1.03/Invoice")


def test_no_production_mode(settings):
    settings.GST_EINVOICE_MODE = "production"
    assert conf.mode() == "off" and not conf.enabled()
    from iic_booking.gst_einvoice.providers import get_provider

    with pytest.raises(conf.ProductionEndpointRefused):
        get_provider()
    assert conf.status()["production_available"] is False


def test_production_base_url_refuses_before_any_request(settings):
    settings.GST_EINVOICE_PROVIDER = "nic_sandbox"
    settings.GST_EINVOICE_SANDBOX_BASE_URL = "https://einvoice1.gst.gov.in"
    from iic_booking.gst_einvoice.providers.nic_sandbox import NicSandboxProvider

    with pytest.raises(conf.ProductionEndpointRefused):
        NicSandboxProvider()
    assert "production" in conf.status()["problem"]


def test_nic_open_points_are_configurable(settings):
    assert conf.sandbox_options() == {"appkey_bytes": 32, "client_id_header": "client_id",
                                      "vital_prefix": "/eivital/v1.04", "core_prefix": "/eicore/v1.03"}
    settings.GST_EINVOICE_SANDBOX_APPKEY_BYTES = "16"
    settings.GST_EINVOICE_SANDBOX_CLIENT_ID_HEADER = "client-id"
    settings.GST_EINVOICE_SANDBOX_CORE_PREFIX = "/eicore/v1.04/"
    settings.GST_EINVOICE_SANDBOX_VITAL_PREFIX = "/x?y=1"  # not a path: ignored
    assert conf.sandbox_options() == {"appkey_bytes": 16, "client_id_header": "client-id",
                                      "vital_prefix": "/eivital/v1.04", "core_prefix": "/eicore/v1.04"}
    settings.GST_EINVOICE_SANDBOX_APPKEY_BYTES = "24"
    assert conf.sandbox_options()["appkey_bytes"] == 32


def test_redaction_never_keeps_secrets():
    from iic_booking.gst_einvoice.payload import redact

    out = redact({"Password": "p", "AppKey": "k", "Sek": "s", "AuthToken": "t", "BuyerDtls": {"Em": "a@b.c", "Ph": ""},
                  "SignedQRCode": "x" * 50})
    assert out == {"Password": "***", "AppKey": "***", "Sek": "***", "AuthToken": "***",
                   "BuyerDtls": {"Em": "***", "Ph": ""}, "SignedQRCode": "<signed JWT, 50 characters>"}

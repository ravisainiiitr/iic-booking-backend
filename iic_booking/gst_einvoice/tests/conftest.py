"""Fixtures for portal GST invoice tests. Sandbox only: the mock provider by default, and a fake NIC sandbox (no
network) for the NIC adapter. Nothing leaves the test process."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from iic_booking.external_services.tests.conftest import Factory, client_for, main_admin, make_user  # noqa: F401
from iic_booking.users.models.user_type import UserType

BUYER_GSTIN_DELHI = "07AAACX0000X1Z2"   # dummy, valid check digit (specimen)
BUYER_GSTIN_UK = "05AABCU9603R1ZT"      # dummy Uttarakhand GSTIN with a valid check digit


@pytest.fixture
def factory(db):
    return Factory()


@pytest.fixture(autouse=True)
def sandbox(settings, tmp_path):
    settings.GST_EINVOICE_ENABLED = True
    settings.GST_EINVOICE_MODE = "sandbox"
    settings.GST_EINVOICE_PROVIDER = "mock"
    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.DEFAULT_FILE_STORAGE = "django.core.files.storage.FileSystemStorage"
    settings.STORAGES = {**getattr(settings, "STORAGES", {}),
                         "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"}}
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    from django.core.cache import cache

    cache.clear()


@pytest.fixture
def ext_on(db):
    from iic_booking.external_services.models import ExternalServicesSettings

    s = ExternalServicesSettings.load()
    s.invoice_requests_enabled = True
    s.save()
    return s


@pytest.fixture
def accounts(db):
    return make_user(UserType.FINANCE)


def billing(user, **extra):
    from iic_booking.users.models import ExternalBillingProfile

    values = {"billing_name": "Asha Labs Pvt Ltd", "gstin": BUYER_GSTIN_DELHI, "billing_address_line1": "12 Ring Road",
              "billing_city": "New Delhi", "billing_state": "Delhi", "billing_pincode": "110001"}
    values.update(extra)
    return ExternalBillingProfile.objects.update_or_create(user=user, defaults=values)[0]


def _paid_with_receipt(booking, amount):
    """A test-mode gateway payment for the booking with its printed receipt number (no gateway call)."""
    import uuid

    from iic_booking.payments.models import Payment, PaymentOrder, PaymentPurpose

    tag = uuid.uuid4().hex[:10]
    order = PaymentOrder.objects.create(
        razorpay_order_id=f"order_T{tag}", receipt=f"iic_{tag}", purpose=PaymentPurpose.BOOKING_SHORTFALL,
        user=booking.user, booking=booking, base_amount=amount, convenience_fee=Decimal("0"), fee_gst=Decimal("0"),
        fee_gst_percent_snapshot=Decimal("18"), total_amount=amount, idempotency_key=f"k-{tag}", mode="test",
        status="PAID")
    return Payment.objects.create(payment_order=order, razorpay_payment_id=f"pay_T{tag}", amount=amount,
                                  status="CAPTURED", receipt_number=f"IITR/IIC/2026/TEST-{tag[:5].upper()}")


def completed_request(factory, *, test_account=True, base=9000, gst=1620, with_receipt=True, **billing_extra):
    """A completed, paid external booking of a (test) account and its SRIC invoice request (existing flow)."""
    from iic_booking.external_services import invoices
    from iic_booking.external_services.models import InvoiceTrigger, SricInvoiceRequest

    user = make_user(is_test_account=test_account)
    billing(user, **billing_extra)
    eq = factory.equipment(name="FE-SEM (Field Emission SEM)")
    booking = factory.booking(user, eq, factory.at(timezone.localdate() - timedelta(days=2)), status="COMPLETED",
                              total=str(base + gst), completed_at=timezone.now() - timedelta(hours=3),
                              payment_settled_at=timezone.now(),
                              charge_breakdown=[{"description": "Analysis (6 samples)", "amount": float(base)},
                                                {"description": "GST (18%)", "amount": float(gst)}])
    if with_receipt:
        _paid_with_receipt(booking, Decimal(base + gst))
    snap = invoices.build_snapshot(booking, trigger=InvoiceTrigger.COMPLETION, preference=None)
    req = SricInvoiceRequest.objects.create(booking=booking, trigger=InvoiceTrigger.COMPLETION, snapshot=snap)
    return req

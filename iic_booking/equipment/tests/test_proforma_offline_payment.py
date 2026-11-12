"""Paying a saved proforma offline: the receipt goes through the usual offline recharge (approval, wallet credit,
reconciliation) and the proforma follows it to "Paid and reconciled"."""

from datetime import timedelta
from decimal import Decimal

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone

from iic_booking.equipment.proforma_models import ProformaPayment, SavedProformaInvoice
from iic_booking.users.tests.test_external_offline_recharge import PDF, BASE, OfflineRechargeTestBase


@override_settings(EXTERNAL_OFFLINE_RECHARGE_ALL_EXTERNAL_USERS=True)
class ProformaOfflinePaymentTests(OfflineRechargeTestBase):
    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.pi = SavedProformaInvoice(
            number="IIC-PI-TEST-0001", user=self.external, department=self.dept, issued_at=now,
            valid_until=(now + timedelta(days=30)).date(), buyer={"name": "Acme Labs"}, lines=[],
            taxable_total=Decimal("5000.00"), gst_total=Decimal("900.00"), total=Decimal("5900.00"),
        )
        self.pi.pdf.save("IIC-PI-TEST-0001.pdf", ContentFile(b"%PDF-1.4 pi"), save=False)
        self.pi.save()
        self.url = f"/api/proforma-invoices/{self.pi.pk}/"

    def pay(self, **over):
        data = {
            "payment_date": (timezone.localdate() - timedelta(days=1)).isoformat(), "payment_mode": "NEFT",
            "transaction_reference": "SBIN 9999 0001", "payer_bank": "State Bank of India",
            "payer_account_name": "Acme Labs Pvt Ltd",
            "receipt_file": SimpleUploadedFile("receipt.pdf", PDF, content_type="application/pdf"),
        }
        data.update(over)
        return self.client_for(self.external).post(f"{self.url}offline-payment/", data, format="multipart")

    def state(self):
        return self.client_for(self.external).get(self.url).data["state"]

    def test_offline_payment_runs_to_reconciled(self):
        res = self.pay()
        self.assertEqual(res.status_code, 201, res.data)
        link = ProformaPayment.objects.get(proforma=self.pi)
        req = link.recharge_request
        self.assertEqual(req.department, self.dept)
        self.assertEqual(req.amount, Decimal("5900.00"))  # the amount still to pay, when none is given
        self.assertIn("IIC-PI-TEST-0001", req.offline_payment.remarks)
        self.assertEqual(self.state()["key"], "payment_submitted")

        self.assertEqual(self.act(self.accounts, req, "approve", note="Matches").status_code, 200)
        self.assertEqual(self.balance(), Decimal("5900.00"))
        self.assertEqual(self.state()["key"], "paid")

        res = self.act(self.accounts, req, "reconcile", outcome="reconciled", reference="GRANT-77", credit_date="2026-11-01")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self.state()["key"], "reconciled")
        csv = self.client_for(self.accounts).get("/api/admin/proforma-invoices/", {"export": "csv"}).content.decode()
        self.assertIn("GRANT-77", csv)
        self.assertIn("SBIN99990001", csv)

    def test_part_payment_and_no_more_than_is_left(self):
        self.assertEqual(self.pay(amount="6000").status_code, 400)
        self.assertEqual(self.pay(amount="2000").status_code, 201)
        state = self.state()
        self.assertEqual(state["pending"], "2000.00")
        self.assertEqual(state["outstanding"], "3900.00")
        self.assertFalse(state["can_cancel"])

    def test_department_cannot_be_changed_from_the_form(self):
        res = self.pay(department_id=str(self.other.pk))
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(ProformaPayment.objects.get(proforma=self.pi).recharge_request.department, self.dept)

    def test_cancelled_proforma_takes_no_payment(self):
        self.assertEqual(self.client_for(self.external).post(f"{self.url}cancel/").status_code, 200)
        self.assertEqual(self.pay().status_code, 409)
        self.assertFalse(ProformaPayment.objects.exists())
        self.assertEqual(BASE, "/api/wallet/offline-recharge/")

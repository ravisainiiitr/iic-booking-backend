"""Seed the sandbox demonstration for Dean SRIC and SRIC Accounts (proposal section 10, step 1).

    python manage.py gst_einvoice_demo --signatory-email sric.signer@iic-booking.test \
        --certificate test-dsc/test-dsc.cer --token-out signer-token.txt

* Refuses unless GST_EINVOICE_ENABLED is on in sandbox mode (there is no other mode).
* Creates (or reuses) the signatory as a TEST account, registers the given TEST DSC certificate for them, and links a
  signing utility whose token is written to --token-out (never printed).
* Prepares four demonstration invoices for demo buyers, which go down their paths at once:
  B2B Delhi (IRN, IGST), B2B Uttarakhand (IRN, CGST + SGST), B2C Uttarakhand and B2C Maharashtra (waiting for the
  DSC); with --show-timeout a fifth B2B one whose first call goes unanswered and is then looked up, not renumbered.
Then run the signer utility (tools/sric_signer) with the test DSC to sign the B2C invoices.
"""

from __future__ import annotations

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from iic_booking.gst_einvoice import conf, service
from iic_booking.gst_einvoice.models import SignerCertificate

DEMO = [
    {"legal_name": "Demo Analytics Pvt Ltd", "gstin": "07AAACX0000X1Z2", "address1": "12 Ring Road", "location": "New Delhi",
     "pincode": "110001", "amount": "9000", "description": "FE-SEM analysis (6 samples)", "equipment": "FE-SEM",
     "receipt_number": "IITR/IIC/2026/TEST-00003", "po_number": "PO/DA/2026/118"},
    {"legal_name": "Demo Materials LLP", "gstin": "05AABCU9603R1ZT", "address1": "Industrial Area, SIDCUL", "location": "Haridwar",
     "pincode": "249403", "amount": "4500", "description": "XRD analysis (3 samples)", "equipment": "XRD",
     "receipt_number": "IITR/IIC/2026/TEST-00004"},
    {"legal_name": "Dr Demo Researcher", "gstin": "", "address1": "Civil Lines", "location": "Roorkee", "pincode": "247667",
     "state_code": "05", "amount": "2400", "description": "Raman spectroscopy (2 samples)", "equipment": "Raman",
     "receipt_number": "IITR/IIC/2026/TEST-00005"},
    {"legal_name": "Demo Institute of Science", "gstin": "", "address1": "College Road", "location": "Pune",
     "pincode": "411001", "state_code": "27", "amount": "12000", "description": "TEM imaging (4 hours)", "equipment": "HR-TEM",
     "receipt_number": "IITR/IIC/2026/TEST-00006"},
]


class Command(BaseCommand):
    help = "Seed the GST invoice sandbox demonstration (demo buyers, test DSC signatory, signing utility token)."

    def add_arguments(self, parser):
        parser.add_argument("--signatory-email", required=True, help="a TEST account email (created if missing)")
        parser.add_argument("--certificate", required=True, help="the TEST DSC's public certificate (.cer)")
        parser.add_argument("--token-out", required=True, help="file to write the signing utility token to")
        parser.add_argument("--reviewer-email", default="", help="account that prepares the invoices (default: a Main Administrator)")
        parser.add_argument("--show-timeout", action="store_true", help="also show the unanswered-call lookup")

    def handle(self, *args, **opts):
        if not conf.enabled() or conf.mode() != conf.MODE_SANDBOX:
            raise CommandError("GST_EINVOICE_ENABLED must be on (sandbox). Nothing was created.")
        User = get_user_model()
        from iic_booking.users.models.user_type import UserType

        reviewer = (User.objects.filter(email__iexact=opts["reviewer_email"]).first() if opts["reviewer_email"]
                    else User.objects.filter(user_type=UserType.ADMIN, is_active=True).order_by("pk").first())
        if reviewer is None:
            reviewer = User.objects.create(email="gst.demo.admin@iic-booking.test", name="GST Demo Administrator",
                                           user_type=UserType.ADMIN, is_staff=True, is_test_account=True,
                                           admin_approved=True, email_verified=True)
        signatory = User.objects.filter(email__iexact=opts["signatory_email"]).first()
        if signatory is None:
            signatory = User.objects.create(email=opts["signatory_email"], name="SRIC Demo Signatory",
                                            user_type=UserType.FINANCE, is_test_account=True, admin_approved=True,
                                            email_verified=True)
        elif not signatory.is_test_account:
            raise CommandError("The signatory must be a test account for the sandbox demonstration.")
        with open(opts["certificate"], "rb") as fh:
            raw = fh.read()
        from iic_booking.gst_einvoice.dsc import certificate_details

        fingerprint = certificate_details(raw)["fingerprint"]
        if not SignerCertificate.objects.filter(sha256_fingerprint=fingerprint).exists():
            admin = reviewer if service.can_administer(reviewer) else User.objects.filter(
                user_type=UserType.ADMIN, is_active=True).first()
            service.register_certificate(admin, signatory, raw, label="Test DSC (sandbox demonstration)", is_test=True)
        device, token = service.new_device(signatory, "Demo signing computer")
        os.makedirs(os.path.dirname(os.path.abspath(opts["token_out"])), exist_ok=True)
        with open(opts["token_out"], "w", encoding="utf-8") as fh:
            fh.write(token)
        self.stdout.write(f"Signing utility linked ({device.name}); token written to {opts['token_out']}.")
        rows = list(DEMO)
        if opts["show_timeout"]:
            rows.append({**DEMO[0], "legal_name": "Demo Timeout Industries", "notes": "#simulate-timeout",
                         "receipt_number": "IITR/IIC/2026/TEST-00007"})
        for data in rows:
            inv = service.create_demo(data, reviewer)
            if inv.status == "submission_uncertain":
                self.stdout.write(f"  {inv.invoice_number}: {inv.get_status_display()} -> looking it up first")
                inv = service.submit_irn(inv.pk, reviewer)
            self.stdout.write(f"  {inv.invoice_number}  {inv.supply_type}  {inv.customer.legal_name:<28} "
                              f"total {inv.total:>10}  {inv.get_status_display()}"
                              + (f"  IRN {inv.irn[:28]}..." if inv.irn else ""))
        self.stdout.write("Now sign the B2C invoices with tools/sric_signer (test DSC).")

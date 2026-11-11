from django.urls import path

from . import views

app_name = "gst_einvoice"

urlpatterns = [
    path("status/", views.status_view, name="status"),
    path("settings/", views.settings_view, name="settings"),
    path("tax-rules/<int:rule_id>/", views.tax_rule_view, name="tax-rule"),
    # SRIC reviewers (Main Administrator, Accounts In Charge, named SRIC accounts)
    path("invoices/", views.invoices_view, name="invoices"),
    path("invoices/demo/", views.demo_invoice_view, name="invoice-demo"),
    path("invoices/from-request/<int:request_id>/", views.from_request_view, name="invoice-from-request"),
    path("invoices/<int:invoice_id>/", views.invoice_detail_view, name="invoice"),
    path("invoices/<int:invoice_id>/approve/", views.approve_view, name="invoice-approve"),
    path("invoices/<int:invoice_id>/reject/", views.reject_view, name="invoice-reject"),
    path("invoices/<int:invoice_id>/submit/", views.submit_view, name="invoice-submit"),
    path("invoices/<int:invoice_id>/buyer/", views.buyer_view, name="invoice-buyer"),
    path("invoices/<int:invoice_id>/check-gstin/", views.check_gstin_view, name="invoice-check-gstin"),
    path("invoices/<int:invoice_id>/pdf/", views.invoice_pdf_view, name="invoice-pdf"),
    path("register/", views.register_view, name="register"),
    path("gstr1/", views.gstr1_view, name="gstr1"),
    # Signatories' DSC certificates (Main Administrator) and signing utilities (the signatory)
    path("certificates/", views.certificates_view, name="certificates"),
    path("certificates/<int:cert_id>/deactivate/", views.certificate_deactivate_view, name="certificate-deactivate"),
    path("signer/devices/", views.devices_view, name="devices"),
    path("signer/devices/<int:device_id>/revoke/", views.device_revoke_view, name="device-revoke"),
    # The signing utility on the signatory's computer (device token, no session)
    path("signer/api/whoami/", views.signer_whoami, name="signer-whoami"),
    path("signer/api/queue/", views.signer_queue, name="signer-queue"),
    path("signer/api/invoices/<int:invoice_id>/pdf/", views.signer_pdf, name="signer-pdf"),
    path("signer/api/invoices/<int:invoice_id>/signed/", views.signer_upload, name="signer-upload"),
    path("signer/api/gstr1/", views.signer_gstr1, name="signer-gstr1"),
    # Buyers
    path("my/bookings/<int:booking_id>/", views.buyer_booking_view, name="buyer-booking"),
]

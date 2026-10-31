from django.urls import path

from . import views

app_name = "external_services"

urlpatterns = [
    path("window/", views.external_window_view, name="window"),
    # Booking checkout: payment choice before the booking exists (slots held only while an online payment is open)
    path("checkouts/", views.checkout_create, name="checkout-create"),
    path("checkouts/<uuid:public_id>/", views.checkout_detail, name="checkout"),
    path("checkouts/<uuid:public_id>/pay/", views.checkout_pay, name="checkout-pay"),
    path("checkouts/<uuid:public_id>/cancel/", views.checkout_cancel, name="checkout-cancel"),
    # Special booking requests (external users)
    path("special-requests/policy/", views.special_policy_view, name="special-policy"),
    path("special-requests/preview/", views.special_request_preview, name="special-preview"),
    path("special-requests/", views.special_requests_view, name="special-requests"),
    path("special-requests/<int:request_id>/", views.special_request_detail, name="special-request"),
    path("special-requests/<int:request_id>/respond/", views.special_request_respond, name="special-respond"),
    path("special-requests/<int:request_id>/withdraw/", views.special_request_withdraw, name="special-withdraw"),
    path(
        "special-requests/<int:request_id>/acknowledge-short-notice/",
        views.special_request_acknowledge,
        name="special-acknowledge",
    ),
    # Officer in Charge / Main Administrator review (scoped to equipment in their charge)
    path("staff/special-requests/", views.staff_special_requests, name="staff-special-requests"),
    path("staff/special-requests/<int:request_id>/", views.staff_special_request_detail, name="staff-special-request"),
    path(
        "staff/special-requests/<int:request_id>/clarify/",
        views.staff_special_request_clarify,
        name="staff-special-clarify",
    ),
    path(
        "staff/special-requests/<int:request_id>/reject/",
        views.staff_special_request_reject,
        name="staff-special-reject",
    ),
    path(
        "staff/special-requests/<int:request_id>/slots/",
        views.staff_special_request_slots,
        name="staff-special-slots",
    ),
    path(
        "staff/special-requests/<int:request_id>/allocate/",
        views.staff_special_request_allocate,
        name="staff-special-allocate",
    ),
    path(
        "special-requests/<int:request_id>/provisional-invoice/",
        views.special_request_provisional_invoice,
        name="special-provisional-invoice",
    ),
    # Cancellation and refunds (31 Oct 2026 policy): quote, cancel, bank accounts, the user's refunds, staff tracker
    path("bookings/<int:booking_id>/cancellation-quote/", views.booking_cancellation_quote, name="cancellation-quote"),
    path("bookings/<int:booking_id>/cancel/", views.booking_cancel_with_refund, name="booking-cancel"),
    path("refund-accounts/", views.refund_bank_accounts, name="refund-accounts"),
    path("refunds/", views.my_refunds_view, name="my-refunds"),
    path("staff/refunds/", views.staff_refunds_view, name="staff-refunds"),
    path("staff/refunds/<int:refund_id>/close/", views.staff_refund_close, name="staff-refund-close"),
    # Invoices (the GST invoice is issued by SRIC; the portal requests, tracks and delivers it)
    path("bookings/<int:booking_id>/invoice/", views.booking_invoice_view, name="booking-invoice"),
    path(
        "bookings/<int:booking_id>/invoice/request-early/",
        views.booking_invoice_request_early,
        name="booking-invoice-early",
    ),
    path(
        "bookings/<int:booking_id>/provisional-invoice/",
        views.booking_provisional_invoice,
        name="booking-provisional-invoice",
    ),
    path(
        "invoices/documents/<int:document_id>/download/",
        views.invoice_document_download,
        name="invoice-document-download",
    ),
    # Saved addresses and printed invoice copies
    path("addresses/", views.addresses_view, name="addresses"),
    path("addresses/<int:address_id>/", views.address_detail_view, name="address"),
    path("bookings/<int:booking_id>/invoice/copy/", views.booking_copy_request, name="booking-invoice-copy"),
    path("invoice-copies/", views.my_copies_view, name="my-invoice-copies"),
    path("invoice-copies/<int:copy_id>/cancel/", views.copy_cancel_view, name="invoice-copy-cancel"),
    # Sample shipment label (no QR code since 31 Oct 2026: staff record receipt against the booking ID)
    path("bookings/<int:booking_id>/sample-label/", views.booking_sample_label, name="booking-sample-label"),
    # Accounts In Charge
    path("staff/invoice-requests/", views.staff_invoice_requests, name="staff-invoice-requests"),
    path("staff/invoice-requests/<int:request_id>/upload/", views.staff_invoice_upload, name="staff-invoice-upload"),
    path("staff/invoice-requests/<int:request_id>/resolve/", views.staff_invoice_resolve, name="staff-invoice-resolve"),
    path("staff/invoice-copies/", views.staff_copies, name="staff-invoice-copies"),
    path("staff/invoice-copies/<int:copy_id>/prepared/", views.staff_copy_prepared, name="staff-copy-prepared"),
    path("staff/invoice-copies/<int:copy_id>/dispatched/", views.staff_copy_dispatched, name="staff-copy-dispatched"),
    path("staff/invoice-copies/<int:copy_id>/envelope/", views.staff_copy_envelope, name="staff-copy-envelope"),
    # Invoice mode (per booking / consolidated) and credit for government organisations
    path("invoice-mode/", views.invoice_mode_view, name="invoice-mode"),
    path("invoice-mode/consolidated/release/", views.consolidated_invoice_release, name="consolidated-release"),
    path("govt-credit/proforma/", views.consolidated_proforma_create, name="govt-credit-proforma"),
    path("govt-credit/", views.govt_credit_requests, name="govt-credit"),
    path("govt-credit/<uuid:public_id>/settle/", views.govt_credit_settle, name="govt-credit-settle"),
    path("govt-credit/decide/<str:token>/", views.govt_credit_decide, name="govt-credit-decide"),
]

admin_urlpatterns = [
    path("window/", views.admin_window_view, name="admin-window"),
    path("window/preview/", views.admin_window_preview, name="admin-window-preview"),
    path("window/pause/", views.admin_window_pause, name="admin-window-pause"),
    path("window/resume/", views.admin_window_resume, name="admin-window-resume"),
    path("window/advance/", views.admin_window_advance, name="admin-window-advance"),
    path("window/history/", views.admin_window_history, name="admin-window-history"),
    path("settings/", views.admin_settings_view, name="admin-settings"),
    path("policies/", views.admin_policy_add, name="admin-policy-add"),
]

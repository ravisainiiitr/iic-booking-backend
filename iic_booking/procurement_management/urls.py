from django.urls import path, re_path

from . import views_assets, views_config, views_masters, views_planning, views_procurement, views_reports, views_requests

app_name = "procurement_management"

urlpatterns = [
    path("bootstrap/", views_config.bootstrap, name="bootstrap"),
    path("config/", views_config.config_list, name="config-list"),
    path("config/users/", views_config.user_search, name="config-user-search"),
    path("config/<int:department_id>/", views_config.config_detail, name="config-detail"),
    path("config/<int:department_id>/roles/", views_config.role_assignments, name="config-roles"),
    path(
        "config/<int:department_id>/roles/<int:assignment_id>/",
        views_config.role_assignment_detail,
        name="config-role-detail",
    ),
    path("audit/", views_config.audit_logs, name="audit-logs"),
    # Masters
    path("categories/", views_masters.categories, name="categories"),
    path("categories/<int:pk>/", views_masters.category_detail, name="category-detail"),
    path("request-types/", views_masters.request_types, name="request-types"),
    path("request-types/<int:pk>/", views_masters.request_type_detail, name="request-type-detail"),
    path("gst-rates/", views_masters.gst_rates, name="gst-rates"),
    path("gst-rates/<int:pk>/", views_masters.gst_rate_detail, name="gst-rate-detail"),
    path("vendors/", views_masters.vendors, name="vendors"),
    path("vendors/<int:pk>/", views_masters.vendor_detail, name="vendor-detail"),
    path("vendors/<int:pk>/archive/", views_masters.vendor_archive, name="vendor-archive"),
    path("items/", views_masters.items, name="items"),
    path("items/<int:pk>/", views_masters.item_detail, name="item-detail"),
    path("items/<int:pk>/archive/", views_masters.item_archive, name="item-archive"),
    # Requests and approvals
    path("requests/", views_requests.requests_list, name="requests"),
    path("requests/<int:pk>/", views_requests.request_detail, name="request-detail"),
    path("requests/<int:pk>/documents/", views_requests.request_documents, name="request-documents"),
    path("requests/<int:pk>/offline-hod-decision/", views_requests.request_offline_hod, name="request-offline-hod"),
    re_path(
        r"^requests/(?P<pk>\d+)/(?P<action>submit|resubmit|approve|reject|hold|resume|cancel|stores-review|issue)/$",
        views_requests.request_action,
        name="request-action",
    ),
    path("approvals/", views_requests.approvals_inbox, name="approvals"),
    # Plan / non-plan requirements and proposals
    path("requirements/", views_planning.requirements, name="requirements"),
    path("requirements/<int:pk>/", views_planning.requirement_detail, name="requirement-detail"),
    re_path(
        r"^requirements/(?P<pk>\d+)/(?P<action>submit|office-edit|remove|restore|merge|split)/$",
        views_planning.requirement_action,
        name="requirement-action",
    ),
    path("proposals/", views_planning.proposals, name="proposals"),
    path("proposals/<int:pk>/", views_planning.proposal_detail, name="proposal-detail"),
    path("proposals/<int:pk>/pdf/", views_planning.proposal_pdf, name="proposal-pdf"),
    re_path(
        r"^proposals/(?P<pk>\d+)/(?P<action>requirements|send|decide)/$", views_planning.proposal_action, name="proposal-action"
    ),
    path("small-purchase/check/", views_requests.small_purchase_check, name="small-purchase-check"),
    # Purchases, records, invoices
    path("small-purchases/", views_procurement.small_purchases, name="small-purchases"),
    path("requests/<int:pk>/start-procurement/", views_procurement.start_from_request, name="request-start-procurement"),
    path("records/from-requirements/", views_procurement.start_from_requirements, name="records-from-requirements"),
    path("records/<int:pk>/steps/<str:step>/", views_procurement.record_step, name="record-step"),
    path("records/<int:pk>/quotations/", views_procurement.record_quotations, name="record-quotations"),
    path("records/<int:pk>/cancel/", views_procurement.record_cancel, name="record-cancel"),
    path("records/", views_procurement.records, name="records"),
    path("records/<int:pk>/", views_procurement.record_detail, name="record-detail"),
    path("records/<int:pk>/complete/", views_procurement.record_complete, name="record-complete"),
    path("records/<int:pk>/documents/", views_procurement.record_documents, name="record-documents"),
    path("records/<int:pk>/invoices/", views_procurement.record_invoices, name="record-invoices"),
    path("invoices/<int:pk>/", views_procurement.invoice_detail, name="invoice-detail"),
    path("invoices/<int:pk>/variance-review/", views_procurement.invoice_variance_review, name="invoice-variance-review"),
    path("invoices/<int:pk>/payments/", views_procurement.invoice_payment, name="invoice-payment"),
    path("documents/<int:pk>/download/", views_requests.document_download, name="document-download"),
    path("documents/<int:pk>/archive/", views_requests.document_archive, name="document-archive"),
    # Asset register and transfers
    path("assets/", views_assets.assets_list, name="assets"),
    path("assets/<int:pk>/", views_assets.asset_detail, name="asset-detail"),
    path("assets/<int:pk>/status/", views_assets.asset_status, name="asset-status"),
    path("assets/<int:pk>/documents/", views_assets.asset_documents, name="asset-documents"),
    path("assets/<int:pk>/transfers/", views_assets.asset_transfers, name="asset-transfers"),
    path("transfers/", views_assets.transfers_list, name="transfers"),
    re_path(
        r"^transfers/(?P<pk>\d+)/(?P<action>decide|complete|return|cancel)/$", views_assets.transfer_action, name="transfer-action"
    ),
    # Consumable stock
    path("stock/balances/", views_assets.stock_balances, name="stock-balances"),
    path("stock/transactions/", views_assets.stock_transactions, name="stock-transactions"),
    path("stock/levels/", views_assets.stock_levels, name="stock-levels"),
    # AMC / service
    path("amc/", views_assets.amc_list, name="amc"),
    path("amc/<int:pk>/", views_assets.amc_detail, name="amc-detail"),
    path("amc/<int:pk>/renew/", views_assets.amc_renew, name="amc-renew"),
    path("amc/<int:pk>/cancel/", views_assets.amc_cancel, name="amc-cancel"),
    # Dashboard, budget, reports, auditor drill-down
    path("dashboard/", views_reports.dashboard, name="dashboard"),
    path("budgets/", views_reports.budgets, name="budgets"),
    path("budgets/summary/", views_reports.budget_summary, name="budget-summary"),
    path("budgets/<int:pk>/", views_reports.budget_detail, name="budget-detail"),
    path("budgets/<int:pk>/archive/", views_reports.budget_archive, name="budget-archive"),
    path("reports/", views_reports.report_catalog, name="report-catalog"),
    path("reports/<str:name>/", views_reports.report, name="report"),
    path("trail/<str:kind>/<int:pk>/", views_reports.trail, name="trail"),
]

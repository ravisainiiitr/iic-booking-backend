"""URL routes for Laboratory Infrastructure."""

from django.urls import path

from iic_booking.lab_infrastructure import views

app_name = "lab_infrastructure"

urlpatterns = [
    path("infrastructure/", views.infrastructure, name="infrastructure"),
    path("infrastructure/nodes/<path:node_id>/", views.node_detail, name="node-detail"),
    path("infrastructure/nodes/<path:node_id>/repair/", views.repair_action, name="node-repair"),
    path("infrastructure/nodes/<path:node_id>/diagnostics/", views.run_diagnostics, name="node-diagnostics"),
    path("alerts/", views.alerts_list, name="alerts"),
    path("alerts/<uuid:alert_id>/ack/", views.alert_ack, name="alert-ack"),
    path("audit/", views.audit_list, name="audit"),
    path("configuration/profiles/<uuid:profile_id>/", views.configuration_history, name="config-history"),
    path(
        "configuration/profiles/<uuid:profile_id>/rollback/",
        views.configuration_rollback,
        name="config-rollback",
    ),
    path("configuration/ack/", views.configuration_ack, name="config-ack"),
    path("software/compliance/", views.software_compliance, name="software-compliance"),
    path("reports/utilization/", views.utilization_report, name="utilization"),
    path("infrastructure/nodes/<path:node_id>/maintenance/", views.maintenance_action, name="node-maintenance"),
    path(
        "infrastructure/nodes/<path:node_id>/rotate-credentials/",
        views.rotate_agent_secret_hint,
        name="node-rotate-credentials",
    ),
    # Phase 2.5 — Automated Test Dashboard (Main Admin)
    path("testing/", views.testing_dashboard, name="testing-dashboard"),
    path("testing/runs/", views.testing_runs, name="testing-runs"),
    path("testing/runs/<uuid:run_id>/", views.testing_run_detail, name="testing-run-detail"),
    path("testing/runs/<uuid:run_id>/report/", views.testing_report, name="testing-report"),
    path("testing/results/", views.testing_results, name="testing-results"),
    path("testing/results/<uuid:result_id>/", views.testing_result_update, name="testing-result-update"),
    path("testing/seed/", views.testing_seed_catalog, name="testing-seed"),
    path("testing/wizard/", views.testing_wizard_current, name="testing-wizard"),
    path("testing/evidence/", views.testing_evidence_upload, name="testing-evidence"),
    path("testing/defects/", views.testing_defects, name="testing-defects"),
    path("testing/health/", views.testing_health_panel, name="testing-health"),
    path("testing/readiness/", views.testing_readiness, name="testing-readiness"),
]

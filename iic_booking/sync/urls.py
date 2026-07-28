"""
URL configuration for Department Sync Agent APIs.

Mounted at /api/v1/sync/
"""

from django.urls import path

from iic_booking.sync import views

app_name = "sync"

urlpatterns = [
    # Control plane (Milestone 4)
    path("enroll/", views.enroll, name="enroll"),
    path("heartbeat/", views.heartbeat, name="heartbeat"),
    path("bootstrap/", views.bootstrap, name="bootstrap"),
    # Operational data plane (Milestone 5)
    path("equipment/", views.equipment_list, name="equipment-list"),
    path("bookings/", views.bookings_list, name="bookings-list"),
    path("workspaces/", views.workspaces_create, name="workspaces-create"),
    path("commands/", views.commands_list, name="commands-list"),
    path("commands/<uuid:command_id>/acknowledge/", views.command_acknowledge, name="command-acknowledge"),
    path("commands/<uuid:command_id>/complete/", views.command_complete, name="command-complete"),
    path("commands/<uuid:command_id>/fail/", views.command_fail, name="command-fail"),
    # Upload transport (Milestone 9)
    path("uploads/start/", views.upload_start, name="upload-start"),
    path("uploads/chunk/", views.upload_chunk, name="upload-chunk"),
    path("uploads/complete/", views.upload_complete, name="upload-complete"),
    # Result processing (Milestone 10)
    path("results/import/", views.results_import, name="results-import"),
    path("results/finalize/", views.results_finalize, name="results-finalize"),
    # Agent management dashboard (Milestone 11)
    path("admin/agents/", views.admin_agents_list, name="admin-agents-list"),
    path("admin/agents/<uuid:agent_id>/", views.admin_agent_detail, name="admin-agent-detail"),
    path(
        "admin/agents/<uuid:agent_id>/commands/",
        views.admin_agent_create_command,
        name="admin-agent-create-command",
    ),
    # Security (Milestone 12)
    path("security/device/register/", views.security_device_register, name="security-device-register"),
    path("security/device/", views.security_device_identity, name="security-device-identity"),
    path("security/certificates/issue/", views.security_certificate_issue, name="security-certificate-issue"),
    path("security/certificates/renew/", views.security_certificate_renew, name="security-certificate-renew"),
    path("security/certificates/status/", views.security_certificate_status, name="security-certificate-status"),
    path("security/api-keys/rotate/", views.security_api_key_rotate, name="security-api-key-rotate"),
    # Offline sync / disaster recovery (Milestone 13)
    path("recovery/reconcile/", views.recovery_reconcile, name="recovery-reconcile"),
    path("recovery/status/", views.recovery_status, name="recovery-status"),
    path("recovery/events/", views.recovery_event, name="recovery-event"),
    path("recovery/integrity/", views.recovery_integrity_report, name="recovery-integrity"),
    path("recovery/conflicts/", views.recovery_conflict, name="recovery-conflict"),
    # Enterprise multi-agent / multi-department (Milestone 14)
    path("enterprise/departments/", views.enterprise_departments, name="enterprise-departments"),
    path("enterprise/buildings/", views.enterprise_buildings, name="enterprise-buildings"),
    path("enterprise/agents/", views.enterprise_agents, name="enterprise-agents"),
    path("enterprise/topology/", views.enterprise_topology, name="enterprise-topology"),
    path("enterprise/dashboard/", views.enterprise_dashboard, name="enterprise-dashboard"),
    path("enterprise/assign/", views.enterprise_assign, name="enterprise-assign"),
    path("enterprise/maintenance/", views.enterprise_maintenance, name="enterprise-maintenance"),
    path("enterprise/drain/", views.enterprise_drain, name="enterprise-drain"),
    path("enterprise/retire/", views.enterprise_retire, name="enterprise-retire"),
    path("enterprise/capabilities/", views.enterprise_capabilities_report, name="enterprise-capabilities"),
    # Enterprise monitoring / health dashboard (Milestone 15)
    path("monitoring/overview/", views.monitoring_overview, name="monitoring-overview"),
    path("monitoring/agents/", views.monitoring_agents, name="monitoring-agents"),
    path("monitoring/history/", views.monitoring_history, name="monitoring-history"),
    path("monitoring/alerts/", views.monitoring_alerts, name="monitoring-alerts"),
    path("monitoring/capacity/", views.monitoring_capacity, name="monitoring-capacity"),
    path(
        "monitoring/alerts/<uuid:alert_id>/acknowledge/",
        views.monitoring_alert_acknowledge,
        name="monitoring-alert-acknowledge",
    ),
    path(
        "monitoring/alerts/<uuid:alert_id>/resolve/",
        views.monitoring_alert_resolve,
        name="monitoring-alert-resolve",
    ),
    path("monitoring/telemetry/", views.monitoring_telemetry, name="monitoring-telemetry"),
    # Automatic updates / release orchestration (Milestone 16)
    path("releases/", views.releases_list, name="releases-list"),
    path("releases/<uuid:release_id>/", views.releases_detail, name="releases-detail"),
    path("releases/publish/", views.releases_publish, name="releases-publish"),
    path("releases/deploy/", views.releases_deploy, name="releases-deploy"),
    path("releases/rollback/", views.releases_rollback, name="releases-rollback"),
    path("updates/history/", views.updates_history, name="updates-history"),
    path("updates/status/", views.updates_status, name="updates-status"),
    path("updates/discover/", views.updates_discover, name="updates-discover"),
    path("updates/report/", views.updates_status_report, name="updates-report"),
    # Instrument integration / experiments (Milestone 18)
    path("experiments/", views.experiments_list, name="experiments-list"),
    path("experiments/<uuid:experiment_id>/", views.experiments_detail, name="experiments-detail"),
    path("experiments/report/", views.experiments_report, name="experiments-report"),
    path("experiments/telemetry/", views.experiments_telemetry, name="experiments-telemetry"),
    path("instruments/", views.instruments_list, name="instruments-list"),
    path("plugins/", views.plugins_list, name="plugins-list"),
    # Production operations / diagnostics (Release Candidate)
    path("operations/diagnostics/", views.operations_diagnostics, name="operations-diagnostics"),
    path("operations/table-sizes/", views.operations_table_sizes, name="operations-table-sizes"),
    path("operations/top-events/", views.operations_top_events, name="operations-top-events"),
    path("operations/maintenance/", views.operations_maintenance, name="operations-maintenance"),
]

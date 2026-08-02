"""
URL configuration for Remote Analysis APIs.

Mounted at /api/v1/analysis/
"""

from django.urls import path

from iic_booking.remote_analysis import reservation_views, views, workflow_views
from iic_booking.remote_analysis.guacamole import views as session_views
from iic_booking.remote_analysis.workspace import views as workspace_views
from iic_booking.remote_analysis.operations import views as ops_views
from iic_booking.remote_analysis.collaboration import views as collab_views
from iic_booking.remote_analysis import health as health_views
from iic_booking.remote_analysis.installer import views as installer_views

app_name = "remote_analysis"

urlpatterns = [
    # Milestone 8 — production probes (no auth; load balancer friendly)
    path("health/", health_views.health, name="health"),
    path("health/live/", health_views.liveness, name="health-live"),
    path("health/ready/", health_views.readiness, name="health-ready"),
    # Agent control plane (matches Remote Analysis Agent contracts)
    path("register/", views.register, name="register"),
    path("heartbeat/", views.heartbeat, name="heartbeat"),
    path("inventory/", views.inventory, name="inventory"),
    path("commands/", views.commands_poll, name="commands-poll"),
    path("commands/<uuid:command_id>/complete/", views.command_complete, name="command-complete"),
    # Portal management
    path("workstations/", views.workstations_list, name="workstations-list"),
    path("workstations/<uuid:workstation_id>/", views.workstation_detail, name="workstation-detail"),
    path(
        "workstations/<uuid:workstation_id>/maintenance/",
        views.workstation_maintenance,
        name="workstation-maintenance",
    ),
    path(
        "workstations/<uuid:workstation_id>/enable/",
        views.workstation_enable,
        name="workstation-enable",
    ),
    path(
        "workstations/<uuid:workstation_id>/disable/",
        views.workstation_disable,
        name="workstation-disable",
    ),
    path(
        "workstations/<uuid:workstation_id>/commands/",
        views.workstation_create_command,
        name="workstation-create-command",
    ),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("software/", views.software_list, name="software-list"),
    path("commands/history/", views.commands_list, name="commands-history"),
    path("events/", views.events_list, name="events-list"),
    path("heartbeats/", views.heartbeats_list, name="heartbeats-list"),
    # Milestone 3 — Scheduler / reservations
    path("reservations/", reservation_views.reservations_collection, name="reservations"),
    path("reservations/<uuid:reservation_id>/", reservation_views.reservation_detail, name="reservation-detail"),
    path(
        "reservations/<uuid:reservation_id>/cancel/",
        reservation_views.reservation_cancel,
        name="reservation-cancel",
    ),
    path(
        "reservations/<uuid:reservation_id>/extend/",
        reservation_views.reservation_extend,
        name="reservation-extend",
    ),
    path("availability/", reservation_views.availability, name="availability"),
    path("candidates/", reservation_views.candidates, name="candidates"),
    path("scheduler/status/", reservation_views.scheduler_status, name="scheduler-status"),
    path("scheduler/dashboard/", reservation_views.scheduler_dashboard, name="scheduler-dashboard"),
    path("queue/", reservation_views.reservation_queue, name="reservation-queue"),
    # Milestone 4 — Browser remote desktop / Guacamole
    path("session/create/", session_views.session_create, name="session-create"),
    path("session/dashboard/", session_views.session_dashboard, name="session-dashboard"),
    path("session/history/", session_views.session_history, name="session-history"),
    path("sessions/", session_views.sessions_list, name="sessions-list"),
    path("session/<uuid:session_id>/launch/", session_views.session_launch, name="session-launch"),
    path("session/<uuid:session_id>/connect/", session_views.session_connect, name="session-connect"),
    path("session/<uuid:session_id>/terminate/", session_views.session_terminate, name="session-terminate"),
    path("session/<uuid:session_id>/status/", session_views.session_status, name="session-status"),
    path("session/<uuid:session_id>/activity/", session_views.session_activity, name="session-activity"),
    path("session/<uuid:session_id>/audits/", session_views.session_audits, name="session-audits"),
    # Milestone 5 — Analysis workspace / secure file exchange
    path("workspaces/", workspace_views.workspaces_collection, name="workspaces"),
    path("workspaces/dashboard/", workspace_views.workspace_dashboard, name="workspaces-dashboard"),
    path("workspaces/<uuid:workspace_id>/", workspace_views.workspace_detail, name="workspace-detail"),
    path("workspaces/<uuid:workspace_id>/upload/", workspace_views.workspace_upload, name="workspace-upload"),
    path("workspaces/<uuid:workspace_id>/download/", workspace_views.workspace_download, name="workspace-download"),
    path("workspaces/<uuid:workspace_id>/archive/", workspace_views.workspace_archive, name="workspace-archive"),
    path("workspaces/<uuid:workspace_id>/restore/", workspace_views.workspace_restore, name="workspace-restore"),
    path("workspaces/<uuid:workspace_id>/files/", workspace_views.workspace_files, name="workspace-files"),
    path("workspaces/<uuid:workspace_id>/sync/", workspace_views.workspace_sync, name="workspace-sync"),
    path(
        "workspaces/<uuid:workspace_id>/retry-transfer/",
        workspace_views.workspace_retry_transfer,
        name="workspace-retry-transfer",
    ),
    path(
        "workspaces/<uuid:workspace_id>/cancel-transfer/",
        workspace_views.workspace_cancel_transfer,
        name="workspace-cancel-transfer",
    ),
    path(
        "workspaces/<uuid:workspace_id>/files/<uuid:file_id>/versions/",
        workspace_views.workspace_file_versions,
        name="workspace-file-versions",
    ),
    path(
        "workspaces/<uuid:workspace_id>/manifest/",
        workspace_views.agent_workspace_manifest,
        name="workspace-agent-manifest",
    ),
    path(
        "workspaces/<uuid:workspace_id>/files/<uuid:file_id>/content/",
        workspace_views.agent_file_content,
        name="workspace-agent-file-content",
    ),
    path(
        "workspaces/<uuid:workspace_id>/agent-upload/",
        workspace_views.agent_workspace_upload,
        name="workspace-agent-upload",
    ),
    # Milestone 6 — Operations Center
    path("operations/dashboard/", ops_views.operations_dashboard, name="operations-dashboard"),
    path("operations/diagnostics/", ops_views.deployment_diagnostics, name="operations-diagnostics"),
    path("operations/commissioning/", ops_views.commissioning_console, name="operations-commissioning"),
    path(
        "operations/commissioning/action/",
        ops_views.commissioning_action,
        name="operations-commissioning-action",
    ),
    # Commissioning & Diagnostics Toolkit (admin-only, optional — does not alter production workflows)
    path("operations/toolkit/", ops_views.toolkit_console, name="operations-toolkit"),
    path("operations/toolkit/dashboard/", ops_views.toolkit_dashboard, name="operations-toolkit-dashboard"),
    path("operations/toolkit/agent/", ops_views.toolkit_agent, name="operations-toolkit-agent"),
    path("operations/toolkit/connectivity/", ops_views.toolkit_connectivity, name="operations-toolkit-connectivity"),
    path("operations/toolkit/logs/", ops_views.toolkit_logs, name="operations-toolkit-logs"),
    path("operations/toolkit/health-report/", ops_views.toolkit_health_report, name="operations-toolkit-health"),
    path("operations/toolkit/self-test/", ops_views.toolkit_self_test, name="operations-toolkit-self-test"),
    path("operations/toolkit/report/", ops_views.toolkit_commissioning_report, name="operations-toolkit-report"),
    path(
        "operations/toolkit/monitoring/",
        ops_views.toolkit_monitoring_recommendations,
        name="operations-toolkit-monitoring",
    ),
    path("operations/toolkit/runs/", ops_views.toolkit_runs, name="operations-toolkit-runs"),
    path(
        "operations/toolkit/runs/<uuid:run_id>/",
        ops_views.toolkit_run_detail,
        name="operations-toolkit-run-detail",
    ),
    path(
        "operations/toolkit/runs/<uuid:run_id>/timeline/",
        ops_views.toolkit_run_timeline,
        name="operations-toolkit-run-timeline",
    ),
    path(
        "operations/toolkit/runs/<uuid:run_id>/evidence/",
        ops_views.toolkit_run_evidence,
        name="operations-toolkit-run-evidence",
    ),
    path(
        "operations/toolkit/runs/<uuid:run_id>/failures/",
        ops_views.toolkit_run_failure_snapshots,
        name="operations-toolkit-run-failures",
    ),
    path("analytics/", ops_views.analytics_view, name="analytics"),
    path("utilization/", ops_views.utilization_view, name="utilization"),
    path("performance/", ops_views.performance_view, name="performance"),
    path("capacity/", ops_views.capacity_view, name="capacity"),
    path("alerts/", ops_views.alerts_list, name="alerts"),
    path("alerts/<uuid:alert_id>/acknowledge/", ops_views.alert_acknowledge, name="alert-acknowledge"),
    path("reports/", ops_views.reports_list, name="reports"),
    path("reports/generate/", ops_views.reports_generate, name="reports-generate"),
    path("reports/<uuid:report_id>/download/", ops_views.report_download, name="report-download"),
    # Milestone 7 — Collaboration Center
    path("collaboration/dashboard/", collab_views.collaboration_dashboard, name="collaboration-dashboard"),
    path("activity/", collab_views.activity_feed, name="activity-feed"),
    path("notifications/", collab_views.notifications_list, name="notifications"),
    path("notifications/read/", collab_views.notifications_read, name="notifications-read"),
    path("comments/", collab_views.comments_collection, name="comments"),
    path("notes/", collab_views.notes_collection, name="notes"),
    path("share/", collab_views.share_collection, name="share"),
    path("invite/", collab_views.invite_collection, name="invite"),
    path("assistance/", collab_views.assistance_collection, name="assistance"),
    path("timeline/", collab_views.timeline_view, name="timeline"),
    path("announcements/", collab_views.announcements_collection, name="announcements"),
    path("bookmarks/", collab_views.bookmarks_collection, name="bookmarks"),
    path("favorites/", collab_views.favorites_collection, name="favorites"),
    path("recent-workspaces/", collab_views.recent_workspace_touch, name="recent-workspaces"),
    # Analysis Workflow Designer + ops
    path("workflows/", workflow_views.workflow_collection, name="workflows"),
    path("workflows/ops/", workflow_views.workflow_ops_dashboard, name="workflows-ops"),
    path("workflows/capabilities/", workflow_views.capability_list, name="workflow-capabilities"),
    path("workflows/<uuid:workflow_id>/", workflow_views.workflow_detail, name="workflow-detail"),
    path("workflows/<uuid:workflow_id>/clone/", workflow_views.workflow_clone, name="workflow-clone"),
    path("workflows/<uuid:workflow_id>/publish/", workflow_views.workflow_publish, name="workflow-publish"),
    path("workflows/<uuid:workflow_id>/steps/", workflow_views.workflow_steps, name="workflow-steps"),
    path("workflows/<uuid:workflow_id>/map-equipment/", workflow_views.workflow_map_equipment, name="workflow-map-equipment"),
    # Agent Installer distribution + enrollment-keyed bootstrap
    path("installer/releases/", installer_views.releases_collection, name="installer-releases"),
    path("installer/releases/latest/", installer_views.release_latest, name="installer-release-latest"),
    path(
        "installer/releases/latest/download/",
        installer_views.release_latest_download,
        name="installer-release-latest-download",
    ),
    path(
        "installer/releases/latest/download-ticket/",
        installer_views.release_download_ticket,
        name="installer-release-latest-download-ticket",
    ),
    path(
        "installer/releases/download/ticket/<path:token>/",
        installer_views.release_download_by_ticket,
        name="installer-release-download-by-ticket",
    ),
    path(
        "installer/releases/<uuid:release_id>/download-ticket/",
        installer_views.release_download_ticket,
        name="installer-release-download-ticket",
    ),
    path(
        "installer/releases/<uuid:release_id>/download/",
        installer_views.release_download,
        name="installer-release-download",
    ),
    path("installer/catalog/software/", installer_views.catalog_software, name="installer-catalog-software"),
    path("installer/equipment-tree/", installer_views.equipment_tree, name="installer-equipment-tree"),
    path("installer/link/", installer_views.link_equipment, name="installer-link"),
]

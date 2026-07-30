"""
URL configuration for Remote Analysis APIs.

Mounted at /api/v1/analysis/
"""

from django.urls import path

from iic_booking.remote_analysis import reservation_views, views
from iic_booking.remote_analysis.guacamole import views as session_views
from iic_booking.remote_analysis.workspace import views as workspace_views
from iic_booking.remote_analysis.operations import views as ops_views
from iic_booking.remote_analysis.collaboration import views as collab_views
from iic_booking.remote_analysis import health as health_views

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
]

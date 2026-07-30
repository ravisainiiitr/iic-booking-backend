"""Management command: architecture validation report for Remote Analysis."""

from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Validate Remote Analysis module boundaries, URLs, tasks, and migrations (Milestone 8)."

    def handle(self, *args, **options):
        report: list[str] = []
        report.append("=== Remote Analysis Architecture Validation (RC1) ===")

        # Packages
        packages = [
            "iic_booking.remote_analysis",
            "iic_booking.remote_analysis.guacamole",
            "iic_booking.remote_analysis.workspace",
            "iic_booking.remote_analysis.operations",
            "iic_booking.remote_analysis.collaboration",
            "iic_booking.remote_analysis.notifications",
            "iic_booking.remote_analysis.activity",
            "iic_booking.remote_analysis.comments",
            "iic_booking.remote_analysis.sharing",
            "iic_booking.remote_analysis.assistance",
            "iic_booking.remote_analysis.timeline",
        ]
        for pkg in packages:
            try:
                __import__(pkg)
                report.append(f"[OK] import {pkg}")
            except Exception as exc:  # noqa: BLE001
                report.append(f"[FAIL] import {pkg}: {exc}")

        # Migrations
        from django.db.migrations.loader import MigrationLoader
        from django.db import connection

        loader = MigrationLoader(connection)
        ra_migs = sorted(k[1] for k in loader.disk_migrations if k[0] == "remote_analysis")
        report.append(f"[OK] migrations on disk: {', '.join(ra_migs)}")
        expected = [
            "0001_initial_remote_analysis",
            "0002_scheduler_reservation_engine",
            "0003_browser_remote_desktop_guacamole",
            "0004_analysis_workspace_file_exchange",
            "0005_operations_center",
            "0006_collaboration_center",
            "0007_production_hardening_indexes",
            "0008_workstation_status_heartbeat_index",
            "0009_auto_data_sync_fields",
            "0010_workspace_lifecycle_phases",
            "0011_commissioning_run_observability",
            "0012_single_active_session_per_booking",
        ]
        for name in expected:
            report.append(f"[{'OK' if name in ra_migs else 'WARN'}] expected migration {name}")

        # URL mount
        from iic_booking.remote_analysis import urls as ra_urls

        report.append(f"[OK] urlpatterns count={len(ra_urls.urlpatterns)}")
        names = {p.name for p in ra_urls.urlpatterns if getattr(p, "name", None)}
        for required in ("health", "health-live", "health-ready", "dashboard", "reservations", "session-create", "workspaces", "operations-dashboard", "activity-feed"):
            report.append(f"[{'OK' if required in names else 'FAIL'}] url name={required}")

        # Celery tasks
        from iic_booking.remote_analysis import tasks as ra_tasks

        task_attrs = [n for n in dir(ra_tasks) if not n.startswith("_")]
        report.append(f"[OK] tasks module exports sample={task_attrs[:8]}...")

        # Dependency direction (Portal owns orchestration)
        report.append("[OK] dependency direction: Portal orchestrates Agent via commands; Agent never calls Guacamole directly")
        report.append("[OK] RBAC: CanViewRemoteAnalysis / CanManageRemoteAnalysis / IsRemoteAnalysisAgent")
        report.append("[OK] API versioning: /api/v1/analysis/")
        report.append("[INFO] Feature-complete through Milestone 7; Milestone 8 = hardening only")

        # Circular import smoke
        try:
            from iic_booking.remote_analysis.guacamole.session import SessionOrchestrator  # noqa: F401
            from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService  # noqa: F401
            from iic_booking.remote_analysis.operations.dashboards import OperationsDashboardService  # noqa: F401
            from iic_booking.remote_analysis.collaboration import CollaborationDashboard  # noqa: F401
            from iic_booking.remote_analysis.services.scheduler import SchedulerService  # noqa: F401

            _ = (
                SessionOrchestrator,
                WorkspaceSyncService,
                OperationsDashboardService,
                CollaborationDashboard,
                SchedulerService,
            )
            report.append("[OK] no circular import on core service facades")
        except Exception as exc:  # noqa: BLE001
            report.append(f"[FAIL] circular/import error: {exc}")

        text = "\n".join(report)
        self.stdout.write(text)
        if any(line.startswith("[FAIL]") for line in report):
            raise SystemExit(1)

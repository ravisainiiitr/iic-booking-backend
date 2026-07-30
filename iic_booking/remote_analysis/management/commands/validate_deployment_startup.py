"""Fail-fast deployment startup validator (ops only — no business logic changes)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from django.conf import settings as django_settings
from django.core.management.base import BaseCommand
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


REQUIRED_ENV = [
    "DJANGO_SECRET_KEY",
    "DJANGO_ALLOWED_HOSTS",
]

# Accept either naming convention used in this repo / sample env
SECRET_ALIASES = ("DJANGO_SECRET_KEY", "SECRET_KEY")
HOSTS_ALIASES = ("DJANGO_ALLOWED_HOSTS", "ALLOWED_HOSTS")


class Command(BaseCommand):
    help = (
        "Validate production deployment prerequisites before/after start: "
        "env, database, redis, storage, migrations, Guacamole (when enabled)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Treat warnings as failures (recommended in CI / verify-production).",
        )
        parser.add_argument(
            "--skip-guacamole",
            action="store_true",
            help="Skip Guacamole reachability (sync-only deploy).",
        )

    def handle(self, *args, **options):
        strict = bool(options["strict"])
        skip_guac = bool(options["skip_guacamole"])
        rows: list[tuple[str, str, str]] = []  # status, name, detail
        fail = 0
        warn = 0

        def ok(name: str, detail: str = "") -> None:
            rows.append(("PASS", name, detail))

        def bad(name: str, detail: str = "") -> None:
            nonlocal fail
            rows.append(("FAIL", name, detail))
            fail += 1

        def soft(name: str, detail: str = "") -> None:
            nonlocal warn, fail
            if strict:
                rows.append(("FAIL", name, detail))
                fail += 1
            else:
                rows.append(("WARN", name, detail))
                warn += 1

        # --- Environment ---
        secret = next((os.environ.get(k) for k in SECRET_ALIASES if os.environ.get(k)), None)
        if secret and secret not in {"", "change-me", "change-me-use-long-random"}:
            ok("env.SECRET_KEY", "present")
        else:
            bad("env.SECRET_KEY", f"set one of {SECRET_ALIASES}")

        hosts = next((os.environ.get(k) for k in HOSTS_ALIASES if os.environ.get(k)), None)
        if hosts:
            ok("env.ALLOWED_HOSTS", hosts[:80])
        else:
            bad("env.ALLOWED_HOSTS", f"set one of {HOSTS_ALIASES}")

        if django_settings.DEBUG:
            bad("env.DEBUG", "DEBUG must be False in production")
        else:
            ok("env.DEBUG", "False")

        enrollment = (os.environ.get("RA_AGENT_ENROLLMENT_KEY") or "").strip()
        if enrollment:
            ok("env.RA_AGENT_ENROLLMENT_KEY", "configured")
        else:
            soft("env.RA_AGENT_ENROLLMENT_KEY", "missing — readiness will fail when DEBUG=False")

        # --- Database ---
        try:
            connection.ensure_connection()
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            ok("database.reachable", connection.settings_dict.get("NAME") or "default")
        except Exception as exc:  # noqa: BLE001
            bad("database.reachable", f"{type(exc).__name__}: {exc}")

        # --- Migrations current ---
        try:
            executor = MigrationExecutor(connection)
            plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
            if plan:
                pending = [f"{m.app_label}.{m.name}" for m, _ in plan]
                bad("migrations.current", "pending: " + ", ".join(pending[:20]))
            else:
                ok("migrations.current", "no pending migrations")
            ra_applied = [
                k[1]
                for k, v in executor.loader.applied_migrations.items()
                if k[0] == "remote_analysis"
            ]
            if "0012_single_active_session_per_booking" in ra_applied or any(
                n.startswith("0012_") for n in ra_applied
            ):
                ok("migrations.remote_analysis_0012", "applied")
            else:
                soft("migrations.remote_analysis_0012", f"applied={sorted(ra_applied)[-3:]}")
        except Exception as exc:  # noqa: BLE001
            bad("migrations.current", f"{type(exc).__name__}: {exc}")

        # --- Redis ---
        try:
            from django.core.cache import cache

            cache.set("ra_deploy_validate", "1", timeout=5)
            if cache.get("ra_deploy_validate") == "1":
                ok("redis_or_cache.reachable", "cache get/set OK")
            else:
                soft("redis_or_cache.reachable", "cache set/get mismatch")
        except Exception as exc:  # noqa: BLE001
            soft("redis_or_cache.reachable", f"{type(exc).__name__}: {exc}")

        broker = (
            getattr(django_settings, "CELERY_BROKER_URL", None)
            or os.environ.get("CELERY_BROKER_URL")
            or os.environ.get("REDIS_URL")
            or ""
        )
        if str(broker).startswith(("redis://", "rediss://")):
            try:
                import redis

                client = redis.from_url(str(broker), socket_connect_timeout=2, socket_timeout=2)
                if client.ping():
                    ok("redis.broker", "PING OK")
                else:
                    bad("redis.broker", "PING failed")
            except Exception as exc:  # noqa: BLE001
                bad("redis.broker", f"{type(exc).__name__}: {exc}")
        else:
            soft("redis.broker", "CELERY_BROKER_URL/REDIS_URL not redis://")

        # --- Storage writable ---
        try:
            from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings
            from iic_booking.remote_analysis.workspace.storage import StorageManager

            settings_obj = RemoteAnalysisSettings.get_solo()
            root = StorageManager(settings_obj).workspace_root()
            root.mkdir(parents=True, exist_ok=True)
            probe = root / ".ra_deploy_write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            ok("storage.writable", str(root))
        except Exception as exc:  # noqa: BLE001
            # Fallback MEDIA_ROOT
            try:
                media = Path(django_settings.MEDIA_ROOT)
                media.mkdir(parents=True, exist_ok=True)
                probe = media / ".ra_deploy_write_probe"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink(missing_ok=True)
                ok("storage.writable", f"MEDIA_ROOT={media}")
            except Exception as exc2:  # noqa: BLE001
                bad("storage.writable", f"{type(exc).__name__}/{type(exc2).__name__}: {exc2}")

        # --- Guacamole ---
        if skip_guac:
            ok("guacamole", "skipped (--skip-guacamole)")
        else:
            try:
                from iic_booking.remote_analysis.guacamole.client import GuacamoleClient
                from iic_booking.remote_analysis.guacamole.settings_env import production_guacamole_configured
                from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings

                settings_obj = RemoteAnalysisSettings.get_solo()
                if settings_obj.mock_guacamole:
                    soft("guacamole", "mock_guacamole=True — set RA_MOCK_GUACAMOLE=false for live desktop")
                else:
                    configured, problems = production_guacamole_configured(settings_obj)
                    if not configured:
                        bad("guacamole.configured", ",".join(problems or ["incomplete"]))
                    else:
                        probe = GuacamoleClient(settings_obj).health_probe()
                        if probe.get("ok"):
                            ok("guacamole.reachable", f"latency_ms={probe.get('latency_ms')}")
                        else:
                            bad("guacamole.reachable", probe.get("error") or probe.get("status") or "unreachable")
            except Exception as exc:  # noqa: BLE001
                bad("guacamole", f"{type(exc).__name__}: {exc}")

        # --- Report ---
        self.stdout.write("=== Deployment Startup Validation ===")
        for status, name, detail in rows:
            line = f"{status:4}  {name}"
            if detail:
                line += f" — {detail}"
            self.stdout.write(line)
        self.stdout.write("")
        self.stdout.write(f"Summary: PASS={sum(1 for s,_,_ in rows if s=='PASS')} "
                          f"WARN={warn} FAIL={fail}")
        if fail:
            self.stderr.write(self.style.ERROR("RESULT: FAIL"))
            sys.exit(1)
        self.stdout.write(self.style.SUCCESS("RESULT: PASS"))

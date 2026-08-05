"""Read-only Phase 4 reverse-tunnel production verification (no mutations)."""

from __future__ import annotations

import sys

from django.core.management.base import BaseCommand
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder


class Command(BaseCommand):
    help = (
        "Read-only Phase 4 cutover verification: migration 0015, Gateway health/metrics, "
        "and RA_TUNNEL_* presence. Does not print secret values and does not mutate state."
    )

    def handle(self, *args, **options):
        rows: list[tuple[str, str, str]] = []
        fail = 0

        def ok(name: str, detail: str = "") -> None:
            rows.append(("PASS", name, detail))

        def bad(name: str, detail: str = "") -> None:
            nonlocal fail
            rows.append(("FAIL", name, detail))
            fail += 1

        # --- Migration 0015 ---
        try:
            recorder = MigrationRecorder(connection)
            applied = recorder.applied_migrations()
            key = ("remote_analysis", "0015_reverse_tunnel_transport")
            if key in applied:
                ok("migration.0015_reverse_tunnel_transport", "applied")
            else:
                # Tolerate alternate naming if graph renamed; check prefix.
                alt = any(
                    app == "remote_analysis" and name.startswith("0015_")
                    for app, name in applied
                )
                if alt:
                    ok("migration.0015_reverse_tunnel_transport", "applied (0015_*)")
                else:
                    bad("migration.0015_reverse_tunnel_transport", "not applied")
        except Exception as exc:  # noqa: BLE001
            bad("migration.0015_reverse_tunnel_transport", f"{type(exc).__name__}: {exc}")

        # --- Settings / env presence (no secret values) ---
        try:
            from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings
            from iic_booking.remote_analysis.tunnel import (
                TunnelGatewayClient,
                reverse_tunnel_config_status,
            )

            settings_obj = RemoteAnalysisSettings.get_solo()
            ok("transport_mode", str(settings_obj.transport_mode))

            cfg = reverse_tunnel_config_status(settings_obj)
            for name, status in cfg.items():
                if status == "configured":
                    ok(f"config.{name}", "configured")
                else:
                    bad(f"config.{name}", "missing")

            client = TunnelGatewayClient(settings_obj)
            health = client.health()
            if health.get("ok"):
                ok(
                    "gateway.health",
                    "ok "
                    f"connected_agents={health.get('connected_agents', 'n/a')} "
                    f"active_tunnels={health.get('active_tunnels', 'n/a')}",
                )
            else:
                detail = health.get("detail") or health.get("status") or "unreachable"
                bad("gateway.health", str(detail))

            metrics = client.metrics()
            if metrics.get("ok"):
                ok(
                    "gateway.metrics",
                    "ok "
                    f"connected_agents={metrics.get('connected_agents', 'n/a')} "
                    f"active_tunnels={metrics.get('active_tunnels', 'n/a')}",
                )
            else:
                detail = metrics.get("detail") or "unavailable"
                bad("gateway.metrics", str(detail))
        except Exception as exc:  # noqa: BLE001
            bad("reverse_tunnel.verify", f"{type(exc).__name__}: {exc}")

        self.stdout.write("=== Reverse Tunnel Production Verification (read-only) ===")
        for status, name, detail in rows:
            line = f"{status:4}  {name}"
            if detail:
                line += f" — {detail}"
            self.stdout.write(line)
        self.stdout.write("")
        warn = sum(1 for s, _, _ in rows if s == "WARN")
        self.stdout.write(
            f"Summary: PASS={sum(1 for s, _, _ in rows if s == 'PASS')} WARN={warn} FAIL={fail}"
        )
        if fail:
            self.stderr.write(self.style.ERROR("RESULT: FAIL"))
            sys.exit(1)
        self.stdout.write(self.style.SUCCESS("RESULT: PASS"))

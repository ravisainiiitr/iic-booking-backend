"""Phase 3 micro-benchmark for Remote Analysis hot paths (local DB).

Usage (from repo root with venv):
  .\\venv\\Scripts\\python.exe scripts/ra_phase3_benchmark.py

Writes timings to stdout as markdown-friendly rows.
Not a substitute for production load testing.
"""
from __future__ import annotations

import os
import statistics
import sys
import time
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

import django

django.setup()

from django.utils import timezone

from iic_booking.remote_analysis.constants import WorkstationStatus
from iic_booking.remote_analysis.models import AnalysisWorkstation
from iic_booking.remote_analysis.services.heartbeat import HeartbeatService
from iic_booking.remote_analysis.services.reservation import ReservationService
from iic_booking.remote_analysis.services.scheduler import SchedulerService
from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings
from iic_booking.users.tests.factories import UserFactory


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((p / 100.0) * (len(ordered) - 1)))))
    return ordered[idx]


def _time_calls(label: str, fn, n: int = 30) -> dict:
    samples: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return {
        "label": label,
        "n": n,
        "avg_ms": statistics.fmean(samples),
        "p95_ms": _pct(samples, 95),
        "max_ms": max(samples),
    }


def main() -> None:
    settings_obj = RemoteAnalysisSettings.get_solo()
    settings_obj.mock_guacamole = True
    settings_obj.save(update_fields=["mock_guacamole"])

    user = UserFactory(user_type="faculty")
    ws, _ = AnalysisWorkstation.objects.get_or_create(
        agent_id="bench-agent-phase3",
        defaults={
            "display_name": "Bench WS",
            "hostname": "bench-pc",
            "status": WorkstationStatus.AVAILABLE,
            "enabled": True,
            "last_heartbeat": timezone.now(),
            "health_score": 100,
        },
    )
    ws.status = WorkstationStatus.AVAILABLE
    ws.enabled = True
    ws.last_heartbeat = timezone.now()
    ws.health_score = 100
    ws.save()

    hb = HeartbeatService()
    sched = SchedulerService()
    res_svc = ReservationService()
    start = timezone.now() + timedelta(minutes=5)
    end = start + timedelta(hours=1)

    results = []
    results.append(
        _time_calls(
            "heartbeat_process",
            lambda: hb.process(
                ws,
                {"CPU": 12, "Memory": 40, "Disk": 55, "Online": True, "CurrentStatus": "AVAILABLE"},
            ),
            n=40,
        )
    )
    results.append(_time_calls("scheduler_refresh_health", lambda: sched.refresh_health(), n=20))
    results.append(
        _time_calls(
            "reservation_create_cancel",
            lambda: res_svc.cancel(
                res_svc.create_reservation(
                    user=user, requested_start=start, requested_end=end, created_by=user
                ),
                actor=user,
            ),
            n=15,
        )
    )
    results.append(_time_calls("scheduler_process_queue", lambda: sched.process_queue(limit=10), n=15))
    results.append(_time_calls("scheduler_expire_stale", lambda: sched.expire_stale(), n=15))

    print("| Operation | N | Avg (ms) | P95 (ms) | Max (ms) |")
    print("|-----------|---|----------|----------|----------|")
    for r in results:
        print(
            f"| {r['label']} | {r['n']} | {r['avg_ms']:.2f} | {r['p95_ms']:.2f} | {r['max_ms']:.2f} |"
        )


if __name__ == "__main__":
    main()

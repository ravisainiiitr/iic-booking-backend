"""Standalone Mock Agent loop for scripted / CI harness runs.

Usage (Django shell context via manage.py):
  python manage.py shell -c "from tests.analysis_platform.mock_agent.runner import main; main()"
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time

logger = logging.getLogger(__name__)


def main() -> int:
    """Run mock agent until SIGINT / ANALYSIS_MOCK_AGENT_SECONDS elapses."""
    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")
    django.setup()

    from rest_framework.test import APIClient

    from tests.analysis_platform.mock_agent import MockAnalysisAgent
    from tests.analysis_platform.seeder import AnalysisPlatformSeeder

    seconds = float(os.environ.get("ANALYSIS_MOCK_AGENT_SECONDS", "30"))
    seed = AnalysisPlatformSeeder().run()
    agent = MockAnalysisAgent.from_seed(seed, api=APIClient())
    agent.bootstrap()
    agent.start_background(interval_seconds=1.0)
    logger.info(
        "Mock agent online agent_id=%s booking=%s for %.0fs",
        agent.state.agent_id,
        seed.booking.booking_id,
        seconds,
    )

    stop = False

    def _stop(*_args):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)

    deadline = time.time() + seconds
    while not stop and time.time() < deadline:
        time.sleep(0.5)

    agent.stop()
    print(  # noqa: T201
        f"MOCK_AGENT_DONE heartbeats={agent.state.heartbeats} "
        f"commands={len(agent.state.commands_completed)} "
        f"booking_id={seed.booking.booking_id} "
        f"researcher={seed.researcher.email}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

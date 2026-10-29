"""Shared start-up for the local acceptance scripts: acceptance settings only, then django.setup()."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def setup():
    if os.environ.get("DJANGO_SETTINGS_MODULE") != "config.settings.acceptance_local":
        sys.exit("Run through scripts/extbook_local/extbook-local.ps1 (acceptance settings only).")
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import django

    django.setup()
    from django.conf import settings

    if not getattr(settings, "ACCEPTANCE_LOCAL", False):
        sys.exit("Not the local acceptance settings; refusing.")
    return settings

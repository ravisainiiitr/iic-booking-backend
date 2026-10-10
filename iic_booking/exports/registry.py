"""Report keys for ``GET /api/exports/<key>/``; each builder returns a ``Document`` for the request."""

from __future__ import annotations

import importlib
from typing import Callable

_BUILDERS: dict[str, Callable] = {}
_LOADED = False

REPORT_MODULES = (
    "iic_booking.exports.reports.statistics",
    "iic_booking.exports.reports.booking_activity",
    "iic_booking.exports.reports.people",
    "iic_booking.exports.reports.finance",
    "iic_booking.exports.reports.disruptions",
    "iic_booking.exports.reports.admin_insights",
)


def register(key: str):
    def decorator(builder: Callable):
        if key in _BUILDERS and _BUILDERS[key] is not builder:
            raise ValueError(f"Export report {key!r} registered twice")
        _BUILDERS[key] = builder
        return builder

    return decorator


def get_builder(key: str) -> Callable | None:
    global _LOADED
    if not _LOADED:
        for module in REPORT_MODULES:
            importlib.import_module(module)
        _LOADED = True
    return _BUILDERS.get(key)


def report_keys() -> list[str]:
    get_builder("")
    return sorted(_BUILDERS)

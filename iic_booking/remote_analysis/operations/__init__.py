"""Operations Center — analytics, utilization, alerts, reporting, capacity."""

from __future__ import annotations

__all__ = ["OperationsDashboardService"]


def __getattr__(name: str):
    if name == "OperationsDashboardService":
        from iic_booking.remote_analysis.operations.dashboards import OperationsDashboardService

        return OperationsDashboardService
    raise AttributeError(name)

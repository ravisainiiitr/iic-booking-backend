"""Apache Guacamole integration for browser-based Remote Analysis sessions."""

from __future__ import annotations

__all__ = ["GuacamoleIntegrationService"]


def __getattr__(name: str):
    if name == "GuacamoleIntegrationService":
        from iic_booking.remote_analysis.guacamole.services import GuacamoleIntegrationService

        return GuacamoleIntegrationService
    raise AttributeError(name)

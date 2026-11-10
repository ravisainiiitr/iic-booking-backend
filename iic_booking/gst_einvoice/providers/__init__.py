"""IRP adapters. ``get_provider()`` returns the configured sandbox adapter; there is no production adapter."""

from __future__ import annotations

from .. import conf
from .base import IrpProvider, ProviderResult  # noqa: F401


def get_provider() -> IrpProvider:
    if conf.mode() != conf.MODE_SANDBOX:
        raise conf.ProductionEndpointRefused("The GST e-invoice module only runs in sandbox mode.")
    if conf.provider() == conf.PROVIDER_NIC_SANDBOX:
        from .nic_sandbox import NicSandboxProvider

        return NicSandboxProvider()
    from .mock import MockSandboxProvider

    return MockSandboxProvider()

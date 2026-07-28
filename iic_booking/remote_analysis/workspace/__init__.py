"""Analysis Workspace package — Portal-managed secure file exchange."""

from __future__ import annotations

__all__ = ["StorageManager", "TransferManager"]


def __getattr__(name: str):
    if name == "StorageManager":
        from iic_booking.remote_analysis.workspace.storage import StorageManager

        return StorageManager
    if name == "TransferManager":
        from iic_booking.remote_analysis.workspace.transfer import TransferManager

        return TransferManager
    raise AttributeError(name)

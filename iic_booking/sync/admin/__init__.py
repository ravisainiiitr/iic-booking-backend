"""
Department Sync Operations Console — Django Admin package.

RBAC hooks live in scoping.py (not enforced yet). Import order registers all
ModelAdmins for the sync app.
"""

from .agents import DepartmentSyncAgentAdmin
from .assignments import AgentAssignmentAdmin
from .commands import AgentCommandAdmin, BookingWorkspaceAdmin
from .console import SyncOperationsConsoleAdmin
from .heartbeats import AgentHeartbeatAdmin
from .logs import SyncLogAdmin
from .profiles import EquipmentSyncProfileAdmin

__all__ = [
    "SyncOperationsConsoleAdmin",
    "DepartmentSyncAgentAdmin",
    "EquipmentSyncProfileAdmin",
    "AgentAssignmentAdmin",
    "AgentHeartbeatAdmin",
    "SyncLogAdmin",
    "AgentCommandAdmin",
    "BookingWorkspaceAdmin",
]

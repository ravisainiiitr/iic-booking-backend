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
from .installer import DsaInstallerReleaseAdmin
from .ip_reservations import EquipmentPcIpReservationAdmin
from .logs import SyncLogAdmin
from .profiles import EquipmentSyncProfileAdmin
from .templates_admin import EquipmentSyncTemplateAdmin

__all__ = [
    "SyncOperationsConsoleAdmin",
    "DepartmentSyncAgentAdmin",
    "EquipmentSyncProfileAdmin",
    "EquipmentSyncTemplateAdmin",
    "EquipmentPcIpReservationAdmin",
    "AgentAssignmentAdmin",
    "AgentHeartbeatAdmin",
    "SyncLogAdmin",
    "AgentCommandAdmin",
    "BookingWorkspaceAdmin",
    "DsaInstallerReleaseAdmin",
]

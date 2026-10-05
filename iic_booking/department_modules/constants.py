from django.db import models


class ModuleKey(models.TextChoices):
    DSA = "dsa", "Department Sync Agent"
    REMOTE_ANALYSIS = "remote_analysis", "Remote Analysis"
    TRAINING = "training", "Training & Certification"
    PROCUREMENT = "procurement", "Procurement & Assets"


# Switches stored in DepartmentModuleSetting. Procurement keeps its own per-department configuration
# (ProcurementManagementConfiguration) as the single source of truth; the central service reads and writes it.
LOCAL_MODULES = (ModuleKey.DSA, ModuleKey.REMOTE_ANALYSIS, ModuleKey.TRAINING)
ALL_MODULES = LOCAL_MODULES + (ModuleKey.PROCUREMENT,)
LOCAL_MODULE_CHOICES = [(k.value, k.label) for k in LOCAL_MODULES]

TEST_USERS_ONLY_HELP = {
    ModuleKey.DSA: "Only bookings made by test accounts are sent to the department's sync agents as new work.",
    ModuleKey.REMOTE_ANALYSIS: "Only test accounts can start new remote analysis on this department's equipment.",
    ModuleKey.TRAINING: (
        "Only test-account faculty and students of this department see Training, and only test accounts can be "
        "nominated or request demonstrations on its equipment."
    ),
    ModuleKey.PROCUREMENT: "Pilot mode: only the pilot users chosen in Procurement & Assets settings can use it.",
}

OFF_HELP = {
    ModuleKey.DSA: "New bookings on this department's equipment get no agent workspaces; existing ones keep syncing.",
    ModuleKey.REMOTE_ANALYSIS: "No new remote analysis on this department's equipment; bookings made earlier can finish.",
    ModuleKey.TRAINING: "The department's equipment leaves Training and its faculty and students no longer see it.",
    ModuleKey.PROCUREMENT: "Procurement & Assets is hidden for this department.",
}

REASON_MIN_LENGTH = 5

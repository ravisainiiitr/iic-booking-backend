"""
Canonical hierarchical registry of Admin Settings / Admin Panel modules.

New modules should be registered here so they appear automatically in the
Main Admin "Admin Panel Access" permission tree and can be enforced by key.
"""

from __future__ import annotations

from typing import Any


# Stable module keys (dotted). Paths are frontend routes used for guards.
ADMIN_SETTINGS_MODULE_TREE: list[dict[str, Any]] = [
    {
        "key": "user_management",
        "label": "User Management",
        "description": "Users, departments, projects, wallets, documents, and groups",
        "path": "/user-management",
        "children": [
            {
                "key": "user_management.users",
                "label": "Users",
                "description": "List, edit, and map users (Department Admin: map only)",
                "path": "/admin/section/users",
            },
            {
                "key": "user_management.departments",
                "label": "Departments",
                "path": "/admin/section/departments",
            },
            {
                "key": "user_management.projects",
                "label": "Projects",
                "path": "/admin/section/projects",
            },
            {
                "key": "user_management.wallets",
                "label": "Wallets",
                "path": "/admin/section/wallets",
            },
            {
                "key": "user_management.sub_wallets",
                "label": "Sub-Wallets",
                "path": "/admin/section/subWallets",
            },
            {
                "key": "user_management.sub_wallet_transactions",
                "label": "Sub-Wallet Transactions",
                "path": "/admin/section/subWalletTransactions",
            },
            {
                "key": "user_management.wallet_razorpay_orders",
                "label": "Wallet Razorpay Orders",
                "path": "/admin/section/walletRazorpayOrders",
            },
            {
                "key": "user_management.wallet_recharge_requests",
                "label": "Wallet Recharge Requests",
                "path": "/admin/section/walletRechargeRequests",
            },
            {
                "key": "user_management.user_documents",
                "label": "User Documents",
                "path": "/admin/section/userDocuments",
            },
            {
                "key": "user_management.user_groups",
                "label": "User Groups",
                "path": "/admin/section/userGroups",
            },
            {
                "key": "user_management.user_group_members",
                "label": "User Group Members",
                "path": "/admin/section/userGroupMembers",
            },
            {
                "key": "user_management.wallet_sric_settings",
                "label": "Wallet SRIC Office Notification Settings",
                "path": "/admin-settings/wallet-sric-settings",
                "main_admin_only": True,
            },
            {
                "key": "user_management.wallet_withdrawal_requests",
                "label": "Wallet Withdrawal Requests",
                "path": "/admin-settings/wallet-withdrawal-requests",
                "main_admin_only": True,
            },
            {
                "key": "user_management.wallet_credit_facility_settings",
                "label": "Wallet Credit Facility Settings",
                "path": "/admin-settings/wallet-credit-facility-settings",
                "main_admin_only": True,
            },
            {
                "key": "user_management.wallet_student_recharge_settings",
                "label": "Wallet Student Recharge Settings",
                "path": "/admin-settings/wallet-student-recharge-settings",
                "main_admin_only": True,
            },
        ],
    },
    {
        "key": "admin_settings.auth",
        "label": "Session / Auto-logout",
        "description": "Inactivity timeout for authenticated sessions",
        "path": "/admin-settings/auth",
        "main_admin_only": True,
    },
    {
        "key": "admin_settings.communication",
        "label": "Communication",
        "description": "Email templates and communication logs",
        "path": "/admin-settings/communication",
    },
    {
        "key": "admin_settings.inbox_email",
        "label": "Inbox Email",
        "description": "Fetch and view the configured IMAP mailbox",
        "path": "/admin-settings/inbox-email",
        "main_admin_only": True,
    },
    {
        "key": "admin_settings.wallet",
        "label": "Wallet Management",
        "description": "Parse and manage wallet recharge emails",
        "path": "/admin-settings/wallet-recharge-parse",
    },
    {
        "key": "admin_settings.legacy_wallet",
        "label": "Legacy Wallet Lookup",
        "path": "/admin-settings/legacy-wallet-import",
        "main_admin_only": True,
    },
    {
        "key": "admin_settings.equipment",
        "label": "Equipment",
        "description": "Equipment modules, slots, nominations, charges, buffers",
        "path": "/admin-settings/equipment",
        "children": [
            {
                "key": "admin_settings.equipment.addition_requests",
                "label": "Equipment addition requests",
                "path": "/admin/equipment-addition-requests",
            },
            {
                "key": "admin_settings.equipment.booking_attempt_logs",
                "label": "Booking requests log",
                "path": "/booking-attempt-logs",
            },
            {
                "key": "admin_settings.equipment.bookings",
                "label": "Bookings",
                "path": "/admin/section/bookings",
            },
            {
                "key": "admin_settings.equipment.repeat_sample_requests",
                "label": "Repeat Sample Requests",
                "path": "/admin/section/repeatSampleRequests",
            },
            {
                "key": "admin_settings.equipment.daily_slots",
                "label": "Daily Slots",
                "path": "/admin/section/dailySlots",
            },
            {
                "key": "admin_settings.equipment.equipment",
                "label": "Equipment",
                "path": "/admin/section/equipment",
            },
            {
                "key": "admin_settings.equipment.categories",
                "label": "Equipment Categories",
                "path": "/admin/section/equipmentCategories",
            },
            {
                "key": "admin_settings.equipment.groups",
                "label": "Equipment Groups",
                "path": "/admin/section/equipmentGroups",
            },
            {
                "key": "admin_settings.equipment.holidays",
                "label": "Holidays",
                "path": "/admin/section/holidays",
            },
            {
                "key": "admin_settings.equipment.semesters",
                "label": "Semesters",
                "path": "/admin-settings/equipment/semesters",
                "main_admin_only": True,
            },
            {
                "key": "admin_settings.equipment.student_nominations",
                "label": "Student Equipment Operating Nominations",
                "path": "/admin-settings/equipment/student-nominations",
            },
            {
                "key": "admin_settings.equipment.icpms_standards",
                "label": "ICPMS Standard Sample Database",
                "path": "/admin-settings/equipment/icpms-standards",
                "main_admin_only": True,
            },
            {
                "key": "admin_settings.equipment.mode_schedules",
                "label": "Equipment Mode Schedule",
                "path": "/admin-settings/equipment/mode-schedules",
            },
            {
                "key": "admin_settings.equipment.booking_charge_settings",
                "label": "Booking Charge Settings",
                "path": "/admin-settings/equipment/booking-charge-settings",
                "main_admin_only": True,
            },
            {
                "key": "admin_settings.equipment.booking_buffer_config",
                "label": "Booking Buffer Configuration",
                "path": "/admin-settings/equipment/booking-buffer-config",
                "main_admin_only": True,
            },
        ],
    },
    {
        "key": "admin_settings.department_rbac",
        "label": "Department Administration",
        "description": "Manage OIC / Lab / Accounts staff and permission caps for departments",
        "path": "/admin/department-administration",
        "main_admin_only": True,
    },
    {
        "key": "admin_settings.admin_panel_access",
        "label": "Admin Panel Access by User Type",
        "description": "Configure Admin Panel visibility per user type and department",
        "path": "/admin-settings/admin-panel-access",
        "main_admin_only": True,
    },
    {
        "key": "admin_settings.support",
        "label": "Support Tickets",
        "path": "/admin-settings/support",
        "main_admin_only": True,
    },
    {
        "key": "admin_settings.feedback",
        "label": "Portal Feedback",
        "path": "/admin-settings/feedback",
        "main_admin_only": True,
    },
    {
        "key": "admin_settings.quality_improvement",
        "label": "Quality Improvement",
        "path": "/admin-settings/quality-improvement",
        "main_admin_only": True,
    },
    {
        "key": "admin_settings.rewards",
        "label": "Reward Config (Per Equipment)",
        "path": "/admin-settings/rewards",
    },
]


def _walk(nodes: list[dict[str, Any]], parent_key: str | None = None):
    for node in nodes:
        yield node, parent_key
        children = node.get("children") or []
        yield from _walk(children, node["key"])


def flatten_admin_settings_modules() -> list[dict[str, Any]]:
    """Flat list with parent_key for each module."""
    out: list[dict[str, Any]] = []
    for node, parent_key in _walk(ADMIN_SETTINGS_MODULE_TREE):
        out.append(
            {
                "key": node["key"],
                "label": node["label"],
                "description": node.get("description") or "",
                "path": node.get("path") or "",
                "parent_key": parent_key,
                "main_admin_only": bool(node.get("main_admin_only")),
                "has_children": bool(node.get("children")),
            }
        )
    return out


def get_admin_settings_module_tree(*, include_main_admin_only: bool = True) -> list[dict[str, Any]]:
    """Return tree suitable for JSON (optionally stripping main-admin-only nodes)."""

    def clone(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = []
        for n in nodes:
            if not include_main_admin_only and n.get("main_admin_only"):
                continue
            item = {
                "key": n["key"],
                "label": n["label"],
                "description": n.get("description") or "",
                "path": n.get("path") or "",
                "main_admin_only": bool(n.get("main_admin_only")),
            }
            kids = clone(n.get("children") or [])
            if kids:
                item["children"] = kids
            elif n.get("children") and include_main_admin_only:
                item["children"] = []
            result.append(item)
        return result

    return clone(ADMIN_SETTINGS_MODULE_TREE)


def all_module_keys() -> set[str]:
    return {row["key"] for row in flatten_admin_settings_modules()}


def normalize_module_keys(selected: list[str] | set[str]) -> set[str]:
    """
    Drop parent keys that do not cover their full descendant set.

    Selecting a parent then unchecking some children can leave the parent key in
    the stored list; expanding that parent would re-grant the unchecked siblings.
    """
    selected_set = {k for k in selected if k}
    flat = flatten_admin_settings_modules()
    children_map: dict[str, list[str]] = {}
    for row in flat:
        pk = row["parent_key"]
        if pk:
            children_map.setdefault(pk, []).append(row["key"])

    def all_descendants(key: str) -> set[str]:
        out: set[str] = set()
        stack = list(children_map.get(key, []))
        while stack:
            child = stack.pop()
            if child in out:
                continue
            out.add(child)
            stack.extend(children_map.get(child, []))
        return out

    cleaned = set(selected_set)
    for key in list(cleaned):
        desc = all_descendants(key)
        if desc and not desc.issubset(cleaned):
            cleaned.discard(key)
    return cleaned


def expand_module_keys(selected: list[str] | set[str]) -> set[str]:
    """
    Expand selection so that selecting a parent grants all descendants.

    Do NOT add ancestors here. Putting a parent key into the effective set just
    because a leaf was selected would make hub-matching treat every sibling under
    that parent as granted. Hub pages stay reachable via
    ``user_can_access_admin_module`` / ``hasAdminModule`` when any descendant is granted.
    """
    selected_set = normalize_module_keys(selected)
    flat = flatten_admin_settings_modules()
    children_map: dict[str, list[str]] = {}
    for row in flat:
        pk = row["parent_key"]
        if pk:
            children_map.setdefault(pk, []).append(row["key"])

    expanded = set(selected_set)

    # Parent → all descendants
    changed = True
    while changed:
        changed = False
        for key in list(expanded):
            for child in children_map.get(key, []):
                if child not in expanded:
                    expanded.add(child)
                    changed = True

    return expanded


def module_key_for_path(path: str) -> str | None:
    """Best-effort match of a frontend path to a registry module key."""
    if not path:
        return None
    normalized = path.rstrip("/") or "/"
    best = None
    best_len = -1
    for row in flatten_admin_settings_modules():
        p = (row.get("path") or "").rstrip("/")
        if not p:
            continue
        if normalized == p or normalized.startswith(p + "/"):
            if len(p) > best_len:
                best = row["key"]
                best_len = len(p)
    return best


# Map legacy admin section API keys → module keys for backend enforcement.
ADMIN_SECTION_MODULE_KEYS: dict[str, str] = {
    "users": "user_management.users",
    "departments": "user_management.departments",
    "projects": "user_management.projects",
    "wallets": "user_management.wallets",
    "subWallets": "user_management.sub_wallets",
    "subWalletTransactions": "user_management.sub_wallet_transactions",
    "walletRazorpayOrders": "user_management.wallet_razorpay_orders",
    "walletRechargeRequests": "user_management.wallet_recharge_requests",
    "userDocuments": "user_management.user_documents",
    "userGroups": "user_management.user_groups",
    "userGroupMembers": "user_management.user_group_members",
    "bookings": "admin_settings.equipment.bookings",
    "repeatSampleRequests": "admin_settings.equipment.repeat_sample_requests",
    "dailySlots": "admin_settings.equipment.daily_slots",
    "equipment": "admin_settings.equipment.equipment",
    "equipmentCategories": "admin_settings.equipment.categories",
    "equipmentGroups": "admin_settings.equipment.groups",
    "holidays": "admin_settings.equipment.holidays",
}


# Legacy RBAC permission codes granted when the user has matching Admin Panel modules.
# Selecting a parent module (e.g. admin_settings.equipment) also grants these via expand/match.
PERMISSION_CODE_MODULE_KEYS: dict[str, tuple[str, ...]] = {
    "bookings.manage": (
        "admin_settings.equipment",
        "admin_settings.equipment.bookings",
        "admin_settings.equipment.repeat_sample_requests",
        "admin_settings.equipment.daily_slots",
        "admin_settings.equipment.booking_attempt_logs",
    ),
    "equipment.manage": (
        "admin_settings.equipment",
        "admin_settings.equipment.equipment",
        "admin_settings.equipment.categories",
        "admin_settings.equipment.groups",
        "admin_settings.equipment.holidays",
        "admin_settings.equipment.mode_schedules",
        "admin_settings.equipment.student_nominations",
        "admin_settings.equipment.addition_requests",
        "admin_settings.equipment.icpms_standards",
        "admin_settings.equipment.semesters",
        "admin_settings.equipment.booking_charge_settings",
        "admin_settings.equipment.booking_buffer_config",
    ),
    "equipment.request_add": (
        "admin_settings.equipment",
        "admin_settings.equipment.addition_requests",
    ),
    "users.manage": (
        "user_management",
        "user_management.users",
        "user_management.departments",
        "user_management.projects",
        "user_management.user_documents",
        "user_management.user_groups",
        "user_management.user_group_members",
    ),
    "wallet.manage": (
        "admin_settings.wallet",
        "user_management.wallets",
        "user_management.sub_wallets",
        "user_management.sub_wallet_transactions",
        "user_management.wallet_razorpay_orders",
        "user_management.wallet_recharge_requests",
    ),
    "admin_settings.wallet": (
        "admin_settings.wallet",
        "user_management.wallets",
        "user_management.sub_wallets",
        "user_management.sub_wallet_transactions",
        "user_management.wallet_razorpay_orders",
        "user_management.wallet_recharge_requests",
    ),
    "admin_settings.equipment": ("admin_settings.equipment",),
    "admin_settings.communication": ("admin_settings.communication",),
    "admin_settings.reports": (
        "admin_settings.reports",
        "admin_settings.equipment.booking_attempt_logs",
    ),
    "reports.view": (
        "admin_settings.reports",
        "admin_settings.equipment.booking_attempt_logs",
        "admin_settings.equipment",
    ),
    # Mapping Channel-i users to staff roles is part of Users module for Dept Admin.
    "oic.assign": ("user_management", "user_management.users"),
    "lab.assign": ("user_management", "user_management.users"),
    "finance.assign": ("user_management", "user_management.users"),
    "permissions.manage_staff": ("user_management", "user_management.users"),
}


def modules_grant_permission(module_keys: set[str] | list[str], code: str) -> bool:
    """True if any granted Admin Panel module implies the legacy RBAC permission code.

    Matching is exact (after parent→descendant expansion). Prefix matching was
    removed so granting one equipment leaf cannot unlock sibling modules' APIs.
    """
    if not module_keys or not code:
        return False
    keys = set(module_keys)
    for mod in PERMISSION_CODE_MODULE_KEYS.get(code, ()):
        if mod in keys:
            return True
    return False

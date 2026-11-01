"""User-defined groups of dashboard buttons in the main content area ("My shortcuts", Module 7).

Each user arranges existing dashboard buttons into named, collapsible groups with an icon. Stored per user under
``SHORTCUTS_KEY`` in ``User.dashboard_menu_layout`` (the JSON that already holds the OIC/Admin menu layout), so one
user's layout never touches another's. Only button ids are kept: which buttons a user may see is decided where the
dashboard is built, so a layout can never make a button available that the user does not already have.
"""

from __future__ import annotations

import re

SHORTCUTS_KEY = "shortcut_groups"
MAX_GROUPS = 12
MAX_ITEMS_PER_GROUP = 30
MAX_NAME = 40
ICONS = ("star", "flask", "calendar", "wallet", "file", "chart", "users", "bookmark", "settings", "bell", "book",
         "folder")
_ID = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def clean_shortcut_groups(raw) -> tuple[list[dict] | None, str | None]:
    """Validate ``[{"id", "name", "icon", "items": [...], "collapsed"}]`` (list order = group order).

    Returns ``(groups, None)`` or ``(None, error)``. A button id appears in one group at most (later copies are
    dropped); unknown icons fall back to "star".
    """
    if raw in (None, ""):
        return [], None
    if isinstance(raw, dict):
        raw = raw.get("groups")
    if not isinstance(raw, list):
        return None, "Shortcuts must be a list of groups."
    if len(raw) > MAX_GROUPS:
        return None, f"At most {MAX_GROUPS} groups can be created."
    groups, seen_groups, seen_items = [], set(), set()
    for g in raw:
        if not isinstance(g, dict):
            return None, "Each group must be an object."
        gid = str(g.get("id") or "").strip()
        if not _ID.match(gid) or gid in seen_groups:
            return None, "Each group needs a unique id."
        name = " ".join(str(g.get("name") or "").split())[:MAX_NAME]
        if not name:
            return None, "Each group needs a name."
        items = g.get("items") or []
        if not isinstance(items, list):
            return None, "Group buttons must be a list."
        clean = []
        for item in items:
            iid = str(item or "").strip()
            if _ID.match(iid) and iid not in seen_items:
                seen_items.add(iid)
                clean.append(iid)
        if len(clean) > MAX_ITEMS_PER_GROUP:
            return None, f"A group can hold at most {MAX_ITEMS_PER_GROUP} buttons."
        icon = str(g.get("icon") or "").strip()
        seen_groups.add(gid)
        groups.append({
            "id": gid,
            "name": name,
            "icon": icon if icon in ICONS else "star",
            "items": clean,
            "collapsed": bool(g.get("collapsed")),
        })
    return groups, None


def shortcut_groups_of(user) -> list[dict]:
    groups, _err = clean_shortcut_groups((getattr(user, "dashboard_menu_layout", None) or {}).get(SHORTCUTS_KEY))
    return groups or []


def save_shortcut_groups(user, groups: list[dict]) -> None:
    """Store the user's groups, keeping everything else in ``dashboard_menu_layout`` (menu layout, equipment order)."""
    stored = dict(getattr(user, "dashboard_menu_layout", None) or {})
    if groups:
        stored[SHORTCUTS_KEY] = groups
    else:
        stored.pop(SHORTCUTS_KEY, None)  # "restore default": no custom groups
    user.dashboard_menu_layout = stored
    user.save(update_fields=["dashboard_menu_layout"])

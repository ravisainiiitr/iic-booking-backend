"""End-of-session result folders: browse the Analysis PC and collect folders saved outside Output.

The agent is the authority on which folders may be read and deleted (it sees the real filesystem);
the checks here only reject malformed input and obviously system locations early.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.utils import timezone

from iic_booking.remote_analysis.constants import CommandStatus, CommandType
from iic_booking.remote_analysis.models import RemoteCommand
from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace

EXTRA_SOURCES_CAPABILITY = "extra_sources_v1"
EXTRA_FILES_CAPABILITY = "extra_files_v1"
MAX_FOLDERS = 10
MAX_ITEMS = 50
# Chosen items plus what the agent saved automatically from the session account's profile.
MAX_PLAN_SOURCES = 80
KINDS = ("folder", "file")
MAX_PATH = 1024
BROWSE_TIMEOUT = timedelta(seconds=90)
REUSE_WINDOW = timedelta(seconds=20)
RESULT_TTL_SECONDS = 600

_WIN_ABSOLUTE = re.compile(r"^[A-Za-z]:\\")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SYSTEM_PREFIXES = ("\\windows", "\\program files", "\\program files (x86)", "\\programdata", "\\$recycle.bin", "\\system volume information")


class PcFolderError(Exception):
    def __init__(self, code: str, detail: str, status: int = 400):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status


def _cache_key(command_id) -> str:
    return f"ra:pc-browse:{command_id}"


def supported(workstation) -> bool:
    return EXTRA_SOURCES_CAPABILITY in set(getattr(workstation, "agent_capabilities", None) or [])


def files_supported(workstation) -> bool:
    return EXTRA_FILES_CAPABILITY in set(getattr(workstation, "agent_capabilities", None) or [])


def max_items(workstation) -> int:
    return MAX_ITEMS if files_supported(workstation) else MAX_FOLDERS


def normalize_path(raw: Any) -> str:
    if not isinstance(raw, str):
        raise PcFolderError("invalid_path", "Folder paths must be text.")
    path = raw.strip().replace("/", "\\")
    if _CONTROL.search(path) or len(path) > MAX_PATH or not _WIN_ABSOLUTE.match(path):
        raise PcFolderError("invalid_path", "Choose a folder on the Analysis PC, for example D:\\Results.")
    parts = path.split("\\")
    if any(p in {".", ".."} for p in parts):
        raise PcFolderError("invalid_path", "Folder paths cannot contain '.' or '..'.")
    path = path.rstrip("\\")
    # "D:" alone means "current directory on D:" to Windows, so drive roots keep their backslash.
    if len(path) == 2:
        path += "\\"
    return path[0].upper() + path[1:]


def _check_selectable(path: str) -> None:
    rest = path[2:].lower().rstrip("\\")
    if not rest:
        raise PcFolderError("folder_not_allowed", f"A whole drive ({path[:2]}\\) cannot be chosen. Pick a folder inside it.")
    if any(rest == p or rest.startswith(p + "\\") for p in _SYSTEM_PREFIXES):
        raise PcFolderError("folder_not_allowed", f"{path} is a system folder and cannot be chosen.")


def _context(booking):
    from iic_booking.equipment.remote_analysis_integration.analysis_setup import current_workspace, open_session

    session = open_session(booking)
    workspace = current_workspace(booking)
    if session is None or workspace is None or not workspace.workstation_id:
        raise PcFolderError("no_active_session", "Your analysis session is not running.", 409)
    if not supported(workspace.workstation):
        raise PcFolderError(
            "picker_unsupported",
            "This Analysis PC needs an agent update before folders can be chosen. Save results in the Output folder.",
            409,
        )
    return session, workspace


def request_browse(booking, user, path: Any = None) -> dict[str, Any]:
    from iic_booking.remote_analysis.services.commands import CommandService

    session, workspace = _context(booking)
    target = normalize_path(path) if path not in (None, "") else ""
    now = timezone.now()
    cmd = (
        RemoteCommand.objects.filter(
            workstation_id=workspace.workstation_id,
            command_type=CommandType.BROWSE_PC_FOLDERS,
            status__in=[CommandStatus.PENDING, CommandStatus.DELIVERED],
            payload__workspace_id=str(workspace.id),
            payload__path=target,
            created_at__gte=now - REUSE_WINDOW,
        )
        .order_by("-created_at")
        .first()
    )
    if cmd is None:
        started = session.connected_at or session.created_at
        cmd = CommandService().create_command(
            workspace.workstation,
            CommandType.BROWSE_PC_FOLDERS,
            payload={
                "workspace_id": str(workspace.id),
                "session_id": str(session.id),
                "path": target,
                "session_started_at": started.isoformat() if started else None,
            },
            created_by=user if getattr(user, "pk", None) else None,
        )
        RemoteCommand.objects.filter(pk=cmd.pk).update(expires_at=now + BROWSE_TIMEOUT)
    return {"request_id": str(cmd.id)}


def browse_result(booking, user, request_id: str) -> dict[str, Any]:
    from iic_booking.equipment.remote_analysis_integration.analysis_setup import current_workspace

    workspace = current_workspace(booking)
    if workspace is None:
        raise PcFolderError("unknown_request", "Unknown folder request.", 404)
    try:
        cmd = RemoteCommand.objects.filter(
            pk=request_id, command_type=CommandType.BROWSE_PC_FOLDERS, payload__workspace_id=str(workspace.id)
        ).first()
    except (ValueError, ValidationError):
        cmd = None
    if cmd is None:
        raise PcFolderError("unknown_request", "Unknown folder request.", 404)
    if cmd.status in {CommandStatus.PENDING, CommandStatus.DELIVERED}:
        if cmd.created_at < timezone.now() - BROWSE_TIMEOUT:
            return {"status": "failed", "detail": "The Analysis PC did not respond. Try again."}
        return {"status": "pending"}
    if cmd.status == CommandStatus.COMPLETED:
        result = cache.get(_cache_key(cmd.id))
        if isinstance(result, dict):
            return {"status": "done", "result": result}
        return {"status": "failed", "detail": "This folder list has expired. Open the folder again."}
    detail = (cmd.error_message or "").split(": ", 1)[-1].strip() or "The Analysis PC could not list this folder."
    return {"status": "failed", "detail": detail[:300]}


def store_browse_result(command, result: dict[str, Any] | None) -> None:
    if isinstance(result, dict):
        cache.set(_cache_key(command.id), result, RESULT_TTL_SECONDS)


def _parse_item(raw: Any) -> tuple[str, str]:
    """A chosen item is a folder path, or ``{"path", "kind"}`` where kind is "folder" or "file"."""
    if isinstance(raw, dict):
        kind = raw.get("kind") or "folder"
        if kind not in KINDS:
            raise PcFolderError("invalid_path", "Each item must be a folder or a file.")
        return normalize_path(raw.get("path")), kind
    return normalize_path(raw), "folder"


def select_folders(booking, user, paths: Any) -> list[dict[str, str]]:
    from iic_booking.equipment.remote_analysis_integration.analysis_setup import merge_state

    if not isinstance(paths, list):
        raise PcFolderError("invalid_path", "extra_folders must be a list of folders or files.")
    session, workspace = _context(booking)
    limit = max_items(workspace.workstation)
    if len(paths) > limit:
        raise PcFolderError("too_many_folders", f"Choose at most {limit} folders or files.")
    allow_files = files_supported(workspace.workstation)
    chosen: list[dict[str, str]] = []
    for raw in paths:
        path, kind = _parse_item(raw)
        if kind == "file" and not allow_files:
            raise PcFolderError("picker_unsupported", "This Analysis PC needs an agent update before single files can be chosen.")
        _check_selectable(path)
        low = path.lower()
        for other in chosen:
            o = other["path"].lower()
            if low == o:
                break
            if low.startswith(o + "\\") or o.startswith(low + "\\"):
                raise PcFolderError("nested_folders", f"{path} is inside {other['path']}. Choose only the outer folder.")
        else:
            chosen.append({"path": path, "kind": kind})
    merge_state(
        AnalysisWorkspace,
        workspace.pk,
        "transfer_state",
        extra_sources={"session_id": str(session.id), "paths": [c["path"] for c in chosen], "items": chosen},
    )
    return chosen


def _selection_items(selection: dict[str, Any]) -> list[dict[str, str]]:
    items = selection.get("items")
    if isinstance(items, list):
        return [i for i in items if isinstance(i, dict) and isinstance(i.get("path"), str) and i.get("kind") in KINDS]
    return [{"path": p, "kind": "folder"} for p in selection.get("paths") or [] if isinstance(p, str)]


def _state(workspace) -> dict[str, Any]:
    return AnalysisWorkspace.objects.filter(pk=workspace.pk).values_list("transfer_state", flat=True).first() or {}


def collect_sources(workspace, session_id: str) -> list[dict[str, str]]:
    """``extra_sources`` for a COLLECT of this session (only items chosen during the same session)."""
    if not session_id or not supported(workspace.workstation):
        return []
    selection = _state(workspace).get("extra_sources") or {}
    if str(selection.get("session_id") or "") != str(session_id):
        return []
    return [{"path": i["path"], "kind": i["kind"]} for i in _selection_items(selection)]


def plan_sources(raw: Any) -> list[dict[str, Any]] | None:
    """Validate ``extra_sources`` reported in a collect plan.

    Each entry is either collected (path + the alias its files were uploaded under) or skipped with an
    ``error`` the agent explains to the user (no alias, nothing uploaded, nothing deleted). ``auto`` entries
    were saved from the session account's profile at the end of the session without being chosen.
    """
    if raw in (None, ""):
        return []
    if not isinstance(raw, list) or len(raw) > MAX_PLAN_SOURCES:
        return None
    out = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        try:
            path = normalize_path(item.get("path"))
        except PcFolderError:
            return None
        kind = item.get("kind") or "folder"
        if kind not in KINDS:
            return None
        extra = {"auto": True} if item.get("auto") is True else {}
        error = item.get("error")
        if error not in (None, ""):
            if not isinstance(error, str):
                return None
            out.append(
                {"path": path, "kind": kind, "alias": "", "files": 0, "bytes": 0, "error": _CONTROL.sub(" ", error)[:300], **extra}
            )
            continue
        alias = str(item.get("alias") or "")
        if not alias or len(alias) > 255 or alias in {".", ".."} or "/" in alias or "\\" in alias or _CONTROL.search(alias):
            return None
        out.append(
            {"path": path, "kind": kind, "alias": alias, "files": _count(item.get("files")), "bytes": _count(item.get("bytes")), **extra}
        )
    return out


def cleanup_sources(planned: Any) -> list[dict[str, Any]]:
    """Collected folders and files the agent may clean after verification (skipped ones are never touched)."""
    return [
        {"path": s["path"], "alias": s["alias"], "kind": s.get("kind") or "folder", **({"auto": True} if s.get("auto") else {})}
        for s in planned or []
        if isinstance(s, dict) and s.get("alias") and not s.get("error") and s.get("path")
    ]


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def status_folders(workspace, session_id: str) -> list[dict[str, Any]]:
    """Folders being collected for the finish screen: plan details once the agent reports them."""
    if not session_id:
        return []
    state = _state(workspace)
    selection = state.get("extra_sources") or {}
    collect = state.get("collect") or {}
    planned = collect.get("extra_sources")
    selected = str(selection.get("session_id") or "") == str(session_id)
    if planned and "session_id" in collect:
        if str(collect.get("session_id") or "") == str(session_id):
            return list(planned)
    elif planned and selected:
        return list(planned)
    return _selection_items(selection) if selected else []

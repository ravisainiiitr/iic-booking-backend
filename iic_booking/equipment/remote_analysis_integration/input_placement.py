"""Put the session's input data where the user chooses on the Analysis PC.

Input used to wait in the agent's own folder (C:\\ProgramData\\RemoteAnalysisAgent\\...), a path nobody remembers.
Now, once the desktop opens, the portal asks where to save it (a new folder on the Desktop named after the
booking, by default), the agent copies it there and opens the folder on the remote desktop. At the end of the
session the agent removes only the copies the user left untouched.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from django.core.exceptions import ValidationError
from django.utils import timezone

from iic_booking.equipment.remote_analysis_integration.pc_folders import PcFolderError, normalize_path
from iic_booking.remote_analysis.constants import CommandStatus, CommandType
from iic_booking.remote_analysis.models import RemoteCommand
from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace

CAPABILITY = "place_input_v1"
PLACES = ("Desktop", "Documents", "Downloads")
PLACE_TIMEOUT = timedelta(minutes=3)
MAX_NAME = 80
_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def supported(workstation) -> bool:
    return CAPABILITY in set(getattr(workstation, "agent_capabilities", None) or [])


def default_folder_name(booking) -> str:
    return clean_folder_name(getattr(booking, "virtual_booking_id", None) or f"Booking {booking.pk}") or "Analysis input"


def clean_folder_name(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    name = _BAD_NAME.sub("_", raw.strip()).strip().rstrip(". ")
    name = name[:MAX_NAME].rstrip(". ")
    return name if name and name not in {".", ".."} else None


def _context(booking):
    from iic_booking.equipment.remote_analysis_integration.analysis_setup import current_workspace, open_session

    session = open_session(booking)
    workspace = current_workspace(booking)
    if session is None or workspace is None or not workspace.workstation_id:
        raise PcFolderError("no_active_session", "Your analysis session is not running.", 409)
    if not supported(workspace.workstation):
        raise PcFolderError(
            "placement_unsupported",
            "This Analysis PC needs an agent update before input can be saved to a folder you choose.",
            409,
        )
    return session, workspace


def _state(workspace) -> dict[str, Any]:
    return AnalysisWorkspace.objects.filter(pk=workspace.pk).values_list("transfer_state", flat=True).first() or {}


def request_place(booking, user, data: dict[str, Any]) -> dict[str, Any]:
    """Ask the agent to copy this session's input into ``place`` (Desktop / Documents / Downloads) or ``location``."""
    from iic_booking.equipment.remote_analysis_integration.analysis_setup import merge_state
    from iic_booking.remote_analysis.services.commands import CommandService

    session, workspace = _context(booking)
    name = clean_folder_name(data.get("folder_name")) if data.get("folder_name") not in (None, "") else default_folder_name(booking)
    if name is None:
        raise PcFolderError("invalid_name", "Give the folder a name, for example your booking ID.")
    payload: dict[str, Any] = {
        "workspace_id": str(workspace.id),
        "session_id": str(session.id),
        "folder_name": name,
        "open": True,
    }
    # Raw data pre-loaded into the locked holding folder: move it (same volume, instant) instead of copying.
    from iic_booking.remote_analysis.large_data.preload import staging_for_placement

    staged = staging_for_placement(workspace)
    if staged is not None:
        payload["from_staging"] = True
        payload["reservation_id"] = str(staged.reservation_id)
        payload["preload_revision"] = staged.revision
    location = data.get("location")
    if location not in (None, ""):
        payload["location"] = normalize_path(location)
    else:
        place = str(data.get("place") or "Desktop")
        if place not in PLACES:
            raise PcFolderError("invalid_place", "Choose Desktop, Documents, Downloads or another folder.")
        payload["place"] = place

    now = timezone.now()
    cmd = CommandService().create_command(
        workspace.workstation,
        CommandType.PLACE_INPUT,
        payload=payload,
        created_by=user if getattr(user, "pk", None) else None,
    )
    RemoteCommand.objects.filter(pk=cmd.pk).update(expires_at=now + PLACE_TIMEOUT)
    merge_state(
        AnalysisWorkspace,
        workspace.pk,
        "transfer_state",
        input_placement={
            "status": "pending",
            "request_id": str(cmd.id),
            "session_id": str(session.id),
            "folder_name": name,
            "requested_at": now.isoformat(),
        },
    )
    return {"request_id": str(cmd.id), "folder_name": name}


def store_place_result(command, result: dict[str, Any] | None, *, success: bool, message: str = "") -> None:
    from iic_booking.equipment.remote_analysis_integration.analysis_setup import merge_state

    payload = command.payload or {}
    workspace_id = payload.get("workspace_id")
    if not workspace_id:
        return
    current = (AnalysisWorkspace.objects.filter(pk=workspace_id).values_list("transfer_state", flat=True).first() or {}).get(
        "input_placement"
    ) or {}
    if current.get("request_id") not in (None, str(command.id)):
        return  # a newer request replaced this one
    data = result if isinstance(result, dict) else {}
    entry: dict[str, Any] = {
        "status": "done" if success else "failed",
        "request_id": str(command.id),
        "session_id": str(payload.get("session_id") or ""),
        "folder_name": payload.get("folder_name"),
        "finished_at": timezone.now().isoformat(),
    }
    if success:
        entry.update(
            path=str(data.get("path") or "")[:1024],
            display_path=str(data.get("display_path") or data.get("path") or "")[:1024],
            files=int(data.get("files") or 0),
            bytes=int(data.get("bytes") or 0),
            opened=bool(data.get("opened")),
        )
    else:
        entry["detail"] = (message or "").split(": ", 1)[-1].strip()[:300] or "The Analysis PC could not save your input data."
    merge_state(AnalysisWorkspace, workspace_id, "transfer_state", input_placement=entry)


def placement(workspace, session) -> dict[str, Any] | None:
    """This session's placement, or None (a placement from an earlier session does not count).

    With no open session (the desktop just dropped) the latest placement is still reported, so a copy that
    finished is never shown as "still copying". A placement left "pending" is reconciled with its command.
    """
    if workspace is None:
        return None
    entry = _state(workspace).get("input_placement") or {}
    if not entry:
        return None
    if session is not None and str(entry.get("session_id") or "") != str(session.id):
        return None
    if entry.get("status") == "pending":
        try:
            cmd = (
                RemoteCommand.objects.filter(pk=entry.get("request_id"))
                .only("status", "created_at", "error_message", "payload")
                .first()
            )
        except (ValueError, ValidationError):
            cmd = None
        if cmd is None:
            return {**entry, "status": "failed", "detail": "The request was lost. Try again."}
        if cmd.status == CommandStatus.COMPLETED:
            # The agent finished but its result was not recorded: report it from the request itself.
            payload = cmd.payload or {}
            where = payload.get("place") or payload.get("location") or "the Analysis PC"
            return {**entry, "status": "done", "display_path": f"{where} › {payload.get('folder_name') or ''}".rstrip(" ›")}
        if cmd.status not in {CommandStatus.PENDING, CommandStatus.DELIVERED}:
            detail = (cmd.error_message or "").split(": ", 1)[-1].strip()[:300] or "The Analysis PC could not save your input data."
            return {**entry, "status": "failed", "detail": detail}
        if cmd.created_at < timezone.now() - PLACE_TIMEOUT:
            return {**entry, "status": "failed", "detail": "The Analysis PC did not respond. Try again."}
    return dict(entry)


def status(booking, user) -> dict[str, Any]:
    from iic_booking.equipment.remote_analysis_integration.analysis_setup import current_workspace, open_session

    session = open_session(booking)
    workspace = current_workspace(booking)
    supported_here = bool(workspace and workspace.workstation_id and supported(workspace.workstation))
    return {
        "supported": supported_here,
        "places": list(PLACES),
        "default_place": "Desktop",
        "default_folder_name": default_folder_name(booking),
        "placement": placement(workspace, session) if supported_here else None,
    }

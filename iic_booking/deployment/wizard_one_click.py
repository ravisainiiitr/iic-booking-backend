"""
One-click Equipment PC Wizard download (per equipment).

Deployment Center → choose an instrument → "Download one-click wizard". The portal streams the
latest wizard EXE named ``EquipmentWizard-<CODE>.exe`` with a small trailer appended:

    <exe bytes> <UTF-8 JSON {"equipment_code": ..., "data_folder": ...}> <uint32 LE length> "IICEPCW1"

Double-clicking it on the instrument PC finds the DSA on the Equipment Network, selects that
instrument, uses the data folder (default <data drive>\\<CODE>), configures the share and installs
the agent service — no typing. Nothing in the trailer is secret: the instrument PC never talks
to the portal, it pairs with the DSA on the dedicated Equipment Network.
"""

from __future__ import annotations

import json
import re
import struct

from django.core import signing
from django.http import StreamingHttpResponse
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.decorators import authentication_classes
from rest_framework.decorators import permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.sync.permissions import CanManageDepartmentSync

TRAILER_MAGIC = b"IICEPCW1"
_SALT = "iic-eq-wizard-one-click-v1"
_TICKET_MAX_AGE = 10 * 60
_CHUNK = 1024 * 1024
_FOLDER_RE = re.compile(r"^[A-Za-z]:\\[^<>:\"/|?*\x00-\x1f]{0,200}$")


def build_trailer(payload: dict) -> bytes:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return body + struct.pack("<I", len(body)) + TRAILER_MAGIC


def _safe_code(code: str) -> str:
    return "".join(ch for ch in code if ch.isalnum() or ch in "-_.")[:64]


def _latest_wizard():
    from iic_booking.deployment.views import _latest_wizard as latest

    return latest()


@api_view(["POST"])
@permission_classes([IsAuthenticated, CanManageDepartmentSync])
def wizard_one_click_ticket(request):
    """POST {equipment_id, data_folder?} → {url, filename, version, equipment_code}."""
    from iic_booking.equipment.models import Equipment

    rel = _latest_wizard()
    if not rel or not rel.file:
        return Response({"detail": "No Equipment PC Wizard release published."}, status=status.HTTP_404_NOT_FOUND)
    if not str(rel.file.name).lower().endswith(".exe"):
        return Response(
            {"detail": "The published wizard is not a single EXE; a one-click wizard needs the EXE release."},
            status=status.HTTP_409_CONFLICT,
        )

    try:
        equipment = Equipment.objects.filter(pk=int(request.data.get("equipment_id"))).first()
    except (TypeError, ValueError):
        equipment = None
    if equipment is None or not (equipment.code or "").strip():
        return Response({"detail": "Choose an instrument with an equipment code."}, status=status.HTTP_400_BAD_REQUEST)

    folder = str(request.data.get("data_folder") or "").strip()
    if folder and not _FOLDER_RE.match(folder):
        return Response(
            {"detail": r"Data folder must be a local path such as D:\DSATEST."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    token = signing.dumps(
        {"r": str(rel.id), "e": equipment.pk, "f": folder, "u": str(request.user.pk)},
        salt=_SALT,
        compress=True,
    )
    code = equipment.code.strip()
    return Response(
        {
            "url": request.build_absolute_uri(f"/api/v1/deployment/wizard/one-click/{token}/"),
            "expires_in": _TICKET_MAX_AGE,
            "filename": f"EquipmentWizard-{_safe_code(code)}.exe",
            "version": rel.version,
            "equipment_code": code,
        },
    )


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def wizard_one_click_download(request, token: str):
    from iic_booking.deployment.models import EquipmentPcWizardRelease
    from iic_booking.equipment.models import Equipment

    try:
        data = signing.loads(token, salt=_SALT, max_age=_TICKET_MAX_AGE)
    except signing.SignatureExpired:
        return Response({"detail": "Download link expired. Please try again."}, status=status.HTTP_403_FORBIDDEN)
    except signing.BadSignature:
        return Response({"detail": "Invalid download link."}, status=status.HTTP_403_FORBIDDEN)

    rel = EquipmentPcWizardRelease.objects.filter(pk=data.get("r"), is_active=True).first()
    equipment = Equipment.objects.filter(pk=data.get("e")).first()
    if not rel or not rel.file or equipment is None:
        return Response({"detail": "Wizard release or instrument not found."}, status=status.HTTP_404_NOT_FOUND)

    payload = {"v": 1, "equipment_code": equipment.code.strip()}
    if data.get("f"):
        payload["data_folder"] = data["f"]
    trailer = build_trailer(payload)
    field = rel.file
    try:
        size = int(field.size)
    except Exception:  # noqa: BLE001
        size = None

    def stream():
        with field.open("rb") as fh:
            while True:
                chunk = fh.read(_CHUNK)
                if not chunk:
                    break
                yield chunk
        yield trailer

    response = StreamingHttpResponse(stream(), content_type="application/octet-stream")
    response["Content-Disposition"] = f'attachment; filename="EquipmentWizard-{_safe_code(equipment.code.strip())}.exe"'
    response["Cache-Control"] = "no-store, private"
    response["X-Content-Type-Options"] = "nosniff"
    if size is not None:
        response["Content-Length"] = str(size + len(trailer))
    return response

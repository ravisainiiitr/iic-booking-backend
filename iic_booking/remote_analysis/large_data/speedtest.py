"""Phase 0: link speed test for Analysis PCs (RAA agent) and Equipment PCs (DSA).

The portal issues a presigned PUT and GET for ``speedtest/<agent-id>/<uuid>.bin`` in the results bucket. The agent
uploads ``bytes`` random bytes, downloads them back, and reports ``{upload_mbps, download_mbps, bytes, seconds,
measured_at}``. The object is deleted when the result arrives; the lifecycle rule also expires ``speedtest/`` after a day.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from iic_booking.remote_analysis.large_data import s3
from iic_booking.remote_analysis.large_data.common import URL_LIFETIME_SECONDS, TransferError, iso
from iic_booking.remote_analysis.transfer_models import SpeedTestStatus, TransferSpeedTest

logger = logging.getLogger(__name__)

SPEED_TEST_BYTES = 256 * 1024 * 1024
SPEED_TEST_PREFIX = "speedtest"
MAX_MBPS = 100_000.0


def _payload(test: TransferSpeedTest, agent_key: str, result_path: str) -> dict[str, Any]:
    bucket = s3.results_bucket()
    if not bucket:
        raise TransferError("storage_not_configured", "Cloud storage is not configured.", 503)
    key = f"{SPEED_TEST_PREFIX}/{agent_key}/{test.id}.bin"
    test.object_key = key
    test.bytes = SPEED_TEST_BYTES
    test.save(update_fields=["object_key", "bytes"])
    expires = timezone.now() + timedelta(seconds=URL_LIFETIME_SECONDS)
    return {
        "test_id": str(test.id),
        "bytes": SPEED_TEST_BYTES,
        "put_url": s3.presign_put(bucket, key, expires_in=URL_LIFETIME_SECONDS),
        "get_url": s3.presign_get(bucket, key, expires_in=URL_LIFETIME_SECONDS),
        "url_expires_at": iso(expires),
        "result_url": result_path,
    }


def issue_for_workstation(workstation, *, requested_by=None) -> tuple[TransferSpeedTest, Any]:
    """Queue SPEED_TEST on an Analysis PC. Returns (test, RemoteCommand)."""
    from iic_booking.remote_analysis.constants import CommandType
    from iic_booking.remote_analysis.services.commands import CommandService

    test = TransferSpeedTest.objects.create(
        workstation=workstation, requested_by=requested_by if getattr(requested_by, "pk", None) else None
    )
    payload = _payload(test, str(workstation.agent_id), f"/api/v1/analysis/agent/speed-test/{test.id}/result/")
    cmd = CommandService().create_command(
        workstation,
        CommandType.SPEED_TEST,
        payload=payload,
        created_by=requested_by if getattr(requested_by, "pk", None) else None,
        expires_in_hours=URL_LIFETIME_SECONDS // 3600,
    )
    return test, cmd


def issue_for_dsa(sync_agent, *, requested_by=None) -> dict[str, Any]:
    """Payload for a DSA SPEED_TEST command (the fleet action creates the AgentCommand)."""
    test = TransferSpeedTest.objects.create(
        sync_agent=sync_agent, requested_by=requested_by if getattr(requested_by, "pk", None) else None
    )
    return _payload(test, str(sync_agent.agent_uuid), f"/api/v1/sync/speed-test/{test.id}/result/")


def _number(data: dict, name: str) -> float | None:
    value = data.get(name)
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise TransferError("invalid_result", f"{name} must be a number.") from None
    if number < 0 or number > MAX_MBPS * (10_000 if name in {"seconds", "bytes"} else 1):
        raise TransferError("invalid_result", f"{name} is out of range.")
    return number


def record_result(test: TransferSpeedTest, data: dict[str, Any] | None, *, success: bool = True, message: str = "") -> TransferSpeedTest:
    data = data if isinstance(data, dict) else {}
    now = timezone.now()
    if success:
        up = _number(data, "upload_mbps")
        down = _number(data, "download_mbps")
        if up is None and down is None:
            raise TransferError("invalid_result", "upload_mbps or download_mbps is required.")
        test.upload_mbps = up
        test.download_mbps = down
        size = _number(data, "bytes")
        if size is not None:
            test.bytes = int(size)
        test.seconds = _number(data, "seconds")
        measured = parse_datetime(str(data.get("measured_at") or "")) if data.get("measured_at") else None
        test.measured_at = measured or now
        test.status = SpeedTestStatus.COMPLETED
        test.detail = ""
    else:
        test.status = SpeedTestStatus.FAILED
        test.detail = (message or str(data.get("detail") or "") or "Speed test failed")[:300]
    test.completed_at = now
    test.save()
    if test.object_key:
        try:
            s3.delete(s3.results_bucket(), test.object_key)
        except Exception:  # noqa: BLE001 - the lifecycle rule removes it after a day anyway
            logger.warning("Could not delete speed test object %s", test.object_key)
    return test


def latest_for(*, workstation=None, sync_agent=None) -> TransferSpeedTest | None:
    qs = TransferSpeedTest.objects.filter(status=SpeedTestStatus.COMPLETED)
    if workstation is not None:
        qs = qs.filter(workstation=workstation)
    elif sync_agent is not None:
        qs = qs.filter(sync_agent=sync_agent)
    else:
        return None
    return qs.order_by("-measured_at", "-created_at").first()


def serialize(test: TransferSpeedTest | None) -> dict[str, Any] | None:
    if test is None:
        return None
    return {
        "test_id": str(test.id),
        "status": test.status,
        "upload_mbps": test.upload_mbps,
        "download_mbps": test.download_mbps,
        "bytes": test.bytes,
        "seconds": test.seconds,
        "measured_at": iso(test.measured_at),
        "detail": test.detail or None,
    }


def summary(*, workstation=None, sync_agent=None) -> dict[str, Any]:
    """``{latest, pending}`` for fleet pages."""
    latest = latest_for(workstation=workstation, sync_agent=sync_agent)
    pending_qs = TransferSpeedTest.objects.filter(status=SpeedTestStatus.PENDING)
    pending_qs = pending_qs.filter(workstation=workstation) if workstation is not None else pending_qs.filter(sync_agent=sync_agent)
    pending = pending_qs.filter(created_at__gte=timezone.now() - timedelta(seconds=URL_LIFETIME_SECONDS)).exists()
    return {"latest": serialize(latest), "pending": pending}


def store_command_result(command, result: dict[str, Any] | None, *, success: bool, message: str = "") -> None:
    """SPEED_TEST command completion (RAA or DSA) carrying the result instead of the result endpoint."""
    test_id = (command.payload or {}).get("test_id")
    test = TransferSpeedTest.objects.filter(pk=test_id, status=SpeedTestStatus.PENDING).first() if test_id else None
    if test is None or (success and not result):
        return  # the result came (or still comes) through the result endpoint
    try:
        record_result(test, result, success=success, message=message)
    except TransferError as exc:
        record_result(test, None, success=False, message=exc.detail)

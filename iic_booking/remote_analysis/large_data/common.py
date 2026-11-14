"""Shared rules for agent transfers (see the shared contract, "Common transfer rules")."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from django.utils import timezone

MiB = 1024 * 1024
PART_SIZE = 64 * MiB  # files under this size use one presigned PUT
PARALLEL_PARTS = 4
URL_LIFETIME_SECONDS = 6 * 3600
PROGRESS_MIN_INTERVAL_SECONDS = 5
MAX_PARTS = 10_000  # S3 limit
MAX_PART_URLS_PER_CALL = 100
MIN_ACTIVE_SESSION_MBPS = 20
ACTIVE_SESSION_UPLINK_SHARE = 0.4

#: Agent >= this version supports every large-data command even without advertising capabilities.
RAA_LARGE_DATA_VERSION = (1, 0, 40)

CAP_SPEED_TEST = "speed_test_v1"
CAP_PRELOAD = "preload_input_v1"
CAP_OUTBOX = "results_outbox_v1"
CAP_KEEP_RUNNING = "keep_running_v1"


class TransferError(Exception):
    def __init__(self, code: str, detail: str, status: int = 400, **extra: Any):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status
        self.extra = extra

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail, **self.extra}


def _version_tuple(raw: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", raw or "")[:4])


def agent_supports(workstation, capability: str) -> bool:
    """True when the Analysis PC agent advertises ``capability`` or is at least RAA 1.0.40."""
    if workstation is None:
        return False
    caps = set(getattr(workstation, "agent_capabilities", None) or [])
    if capability in caps:
        return True
    version = _version_tuple(getattr(workstation, "agent_version", "") or "")
    return bool(version) and version[:3] >= RAA_LARGE_DATA_VERSION


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def part_size_for(size: int) -> int:
    """64 MiB parts, grown only when a file would need more than 10,000 parts."""
    part = PART_SIZE
    while size > part * MAX_PARTS:
        part *= 2
    return part


def as_int(value, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def clean_rel_path(raw) -> str | None:
    """Relative path with forward slashes; None when empty, absolute-looking or escaping upwards."""
    if not isinstance(raw, str):
        return None
    path = raw.replace("\\", "/").strip().lstrip("/")
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts) or len(path) > 1024 or ":" in parts[0]:
        return None
    return "/".join(parts)


def clean_sha256(raw) -> str | None:
    """Lower-case hex SHA-256, "" when absent, None when malformed."""
    if raw in (None, ""):
        return ""
    value = str(raw).strip().lower()
    return value if re.fullmatch(r"[0-9a-f]{64}", value) else None


def booking_size_limit() -> int:
    from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings

    return int(getattr(RemoteAnalysisSettings.get_solo(), "transfer_max_booking_bytes", 0) or 0)


def check_size_limit(total_bytes: int) -> None:
    limit = booking_size_limit()
    if limit and total_bytes > limit:
        raise TransferError(
            "size_limit",
            f"This booking's data ({total_bytes} bytes) is larger than the {limit}-byte limit set by the lab.",
            413,
            limit_bytes=limit,
            total_bytes=total_bytes,
        )


def observed_rate(previous_bytes: int, previous_at: datetime | None, now_bytes: int, now: datetime, old_rate: float | None) -> float | None:
    """Bytes per second from two progress reports, smoothed with the previous estimate."""
    if previous_at is None or now_bytes <= previous_bytes:
        return old_rate
    seconds = (now - previous_at).total_seconds()
    if seconds <= 0:
        return old_rate
    rate = (now_bytes - previous_bytes) / seconds
    return rate if old_rate is None else 0.3 * rate + 0.7 * old_rate


def eta_seconds(bytes_done: int, bytes_total: int, rate_bps: float | None, fallback_mbps: float | None = None) -> int | None:
    remaining = max(0, int(bytes_total or 0) - int(bytes_done or 0))
    if remaining == 0:
        return 0 if bytes_total else None
    rate = rate_bps
    if not rate and fallback_mbps:
        rate = fallback_mbps * 1_000_000 / 8
    if not rate:
        return None
    return int(remaining / rate)


def transfer_limits(workstation=None) -> dict[str, Any]:
    """Bandwidth/cache settings sent to agents with every transfer command and manifest."""
    from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings

    settings_obj = RemoteAnalysisSettings.get_solo()
    mbps = int(getattr(settings_obj, "transfer_active_session_mbps", 0) or 0)
    if not mbps and workstation is not None:
        from iic_booking.remote_analysis.large_data.speedtest import latest_for

        latest = latest_for(workstation=workstation)
        if latest is not None and latest.upload_mbps:
            mbps = int(latest.upload_mbps * ACTIVE_SESSION_UPLINK_SHARE)
    if mbps:
        mbps = max(MIN_ACTIVE_SESSION_MBPS, mbps)
    return {
        "part_size": PART_SIZE,
        "parallel_parts": PARALLEL_PARTS,
        "url_lifetime_seconds": URL_LIFETIME_SECONDS,
        "progress_interval_seconds": PROGRESS_MIN_INTERVAL_SECONDS,
        "transfer_active_session_mbps": mbps or None,
        "cache_max_gb": int(getattr(settings_obj, "transfer_cache_max_gb", 200) or 200),
        "max_booking_bytes": int(getattr(settings_obj, "transfer_max_booking_bytes", 0) or 0) or None,
    }


def now():
    return timezone.now()

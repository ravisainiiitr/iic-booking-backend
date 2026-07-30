"""Production hardening helpers — logging, pagination, Celery task defaults (no new features)."""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from celery import shared_task

logger = logging.getLogger("remote_analysis")

_correlation_id: ContextVar[str | None] = ContextVar("ra_correlation_id", default=None)


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def get_correlation_id() -> str | None:
    return _correlation_id.get()


@contextmanager
def correlation_scope(
    correlation_id: str | None = None,
    *,
    session_id: str | None = None,
    reservation_id: str | None = None,
    workspace_id: str | None = None,
    request_id: str | None = None,
) -> Iterator[str]:
    cid = correlation_id or new_correlation_id()
    token = _correlation_id.set(cid)
    extra = {
        "correlation_id": cid,
        "request_id": request_id or cid,
        "session_id": session_id,
        "reservation_id": reservation_id,
        "workspace_id": workspace_id,
    }
    try:
        yield cid
    finally:
        _correlation_id.reset(token)
        # keep extra referenced for static analyzers / future structured adapters
        _ = extra


def mask_secret(value: str | None, *, visible: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= visible:
        return "*" * len(value)
    return f"{'*' * (len(value) - visible)}{value[-visible:]}"


def json_safe(value: Any) -> Any:
    """Recursively convert UUID / datetime / Path / set for JSONField storage."""
    from datetime import date, datetime
    from pathlib import Path
    from uuid import UUID

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v) for v in value]
    return str(value)


def structured_log(level: int, message: str, **fields: Any) -> None:
    payload = {"correlation_id": get_correlation_id(), **{k: v for k, v in fields.items() if v is not None}}
    logger.log(level, "%s | %s", message, payload)


def parse_pagination(request, *, default_limit: int = 50, max_limit: int = 200) -> tuple[int, int]:
    """Return (offset, limit) from query params. Additive — does not change response schema."""
    try:
        limit = int(request.query_params.get("limit", default_limit))
    except (TypeError, ValueError):
        limit = default_limit
    try:
        offset = int(request.query_params.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    limit = max(1, min(limit, max_limit))
    offset = max(0, offset)
    return offset, limit


def ra_periodic_task(**kwargs):
    """
    Celery decorator defaults for RA beat jobs:
    - autoretry on transient infrastructure failures
    - limited retries / backoff to avoid stampeding
    - acks_late for safer worker restarts (idempotent jobs only)
    """
    from django.db import OperationalError

    defaults = {
        "autoretry_for": (OperationalError, ConnectionError, TimeoutError, OSError),
        "retry_backoff": True,
        "retry_backoff_max": 300,
        "retry_jitter": True,
        "max_retries": 3,
        "acks_late": True,
        "reject_on_worker_lost": True,
    }
    defaults.update(kwargs)
    return shared_task(**defaults)

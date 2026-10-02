from __future__ import annotations

import logging

from .models import TrainingAuditLog

logger = logging.getLogger(__name__)


def audit(actor, action: str, obj, *, before=None, after=None, note: str = "", request=None) -> None:
    ip = None
    if request is not None:
        ip = (request.META.get("HTTP_X_FORWARDED_FOR") or request.META.get("REMOTE_ADDR") or "").split(",")[0].strip()
        ip = ip or None
    try:
        TrainingAuditLog.objects.create(
            actor=actor if getattr(actor, "pk", None) else None,
            action=action,
            object_type=type(obj).__name__,
            object_id=str(getattr(obj, "pk", "") or ""),
            before=before or {},
            after=after or {},
            note=note or "",
            ip=ip,
        )
    except Exception:
        logger.exception("training audit write failed action=%s", action)

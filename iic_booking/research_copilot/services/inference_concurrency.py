"""Process-local concurrency gate for Copilot LLM calls (AI.17).

Keeps Ollama/OpenAI inference from starving Django workers. Does not affect
booking, DSA, RAA, Celery, wallet, or payment code paths.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from django.conf import settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_semaphore: threading.BoundedSemaphore | None = None
_active = 0
_rejected = 0


@dataclass(frozen=True)
class ConcurrencySnapshot:
    max_concurrent: int
    active: int
    rejected_total: int

    def as_public_dict(self) -> dict:
        return {
            "max_concurrent": self.max_concurrent,
            "active_generations": self.active,
            "rejected_total": self.rejected_total,
        }


def _max_concurrent() -> int:
    raw = int(getattr(settings, "RESEARCH_COPILOT_MAX_CONCURRENT", 2) or 2)
    return max(1, min(raw, 32))


def _get_semaphore() -> threading.BoundedSemaphore:
    global _semaphore
    with _lock:
        limit = _max_concurrent()
        if _semaphore is None or getattr(_semaphore, "_initial_value", limit) != limit:
            _semaphore = threading.BoundedSemaphore(limit)
            _semaphore._initial_value = limit  # type: ignore[attr-defined]
        return _semaphore


def snapshot() -> ConcurrencySnapshot:
    with _lock:
        return ConcurrencySnapshot(
            max_concurrent=_max_concurrent(),
            active=_active,
            rejected_total=_rejected,
        )


class CopilotBusyError(Exception):
    """Raised when the generation concurrency limit is reached."""


BUSY_USER_MESSAGE = (
    "Research Copilot is temporarily busy. Your booking and other "
    "portal operations are unaffected."
)


@contextmanager
def acquire_generation_slot(*, wait: bool = False, timeout: float = 0.0) -> Iterator[None]:
    """
    Acquire a Copilot generation slot.

    Default is non-blocking: if saturated, raise CopilotBusyError immediately
    so portal workers are not held waiting on AI.
    """
    global _active, _rejected
    sem = _get_semaphore()
    acquired = sem.acquire(blocking=wait, timeout=timeout if wait else None)
    if not acquired:
        with _lock:
            _rejected += 1
        logger.info(
            "COPILOT_BUSY max=%s active=%s rejected=%s",
            _max_concurrent(),
            snapshot().active,
            snapshot().rejected_total,
        )
        raise CopilotBusyError(BUSY_USER_MESSAGE)
    with _lock:
        _active += 1
    try:
        yield
    finally:
        with _lock:
            _active = max(0, _active - 1)
        sem.release()

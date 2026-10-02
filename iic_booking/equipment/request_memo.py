"""Request-scoped memo for read-only lookups repeated many times within one request.

The booking hot paths (equipment detail, charge calculation, booking create) resolve the
same PI / wallet / pricing facts several times per request. The memo is only active while
``RequestMemoMiddleware`` (or ``request_memo()``) is on the stack, so Celery tasks, shell
code and tests that call helpers directly always hit the database.

Only memoise facts that the surrounding request does not itself modify.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

_MISSING = object()
_memo: ContextVar[dict | None] = ContextVar("iic_request_memo", default=None)


def memo_active() -> bool:
    return _memo.get() is not None


@contextmanager
def request_memo():
    """Activate a fresh memo for the enclosed block (nested blocks reuse the outer memo)."""
    if _memo.get() is not None:
        yield
        return
    token = _memo.set({})
    try:
        yield
    finally:
        _memo.reset(token)


def memo_get_or_compute(key, compute):
    store = _memo.get()
    if store is None:
        return compute()
    value = store.get(key, _MISSING)
    if value is _MISSING:
        value = compute()
        store[key] = value
    return value


def memo_clear() -> None:
    store = _memo.get()
    if store is not None:
        store.clear()


def with_request_memo(view_func):
    """Decorator for write views whose reads are safe to memoise for the whole request."""

    @wraps(view_func)
    def wrapper(*args, **kwargs):
        with request_memo():
            return view_func(*args, **kwargs)

    return wrapper


class RequestMemoMiddleware:
    """Enable the memo for safe (read-only) requests."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method in ("GET", "HEAD"):
            with request_memo():
                return self.get_response(request)
        return self.get_response(request)

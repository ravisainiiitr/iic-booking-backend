import pytest


@pytest.fixture(autouse=True)
def _reset_copilot_breakers():
    """Outage breakers, query-embedding cache and retrieval memo must not leak between tests."""
    from django.core.cache import caches
    from django.core.cache.backends.locmem import LocMemCache

    from iic_booking.research_copilot.services import rag

    if isinstance(caches["default"], LocMemCache):
        caches["default"].clear()
    rag._MEMO.last = None
    yield

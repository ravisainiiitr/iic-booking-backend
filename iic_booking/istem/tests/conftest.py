"""Every I-STEM test runs with outbound HTTP disabled: a real request fails the test."""

import pytest
import requests


@pytest.fixture(autouse=True)
def _no_real_http(monkeypatch):
    def refuse(self, method, url, *args, **kwargs):
        raise AssertionError(f"real HTTP request attempted in a test: {method} {url}")

    monkeypatch.setattr(requests.sessions.Session, "request", refuse)

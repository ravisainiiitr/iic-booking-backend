"""Error-page qualification: unknown routes must not collapse to 500."""

from __future__ import annotations

import pytest
from django.template.loader import get_template
from django.urls import reverse


@pytest.mark.django_db
def test_unknown_api_route_returns_json_404(client):
    response = client.get("/api/does-not-exist/")
    assert response.status_code == 404
    assert response["Content-Type"].startswith("application/json")
    assert response.json()["detail"] == "Not found."


@pytest.mark.django_db
def test_unknown_browser_page_returns_html_404(client):
    response = client.get("/this-page-definitely-does-not-exist/")
    assert response.status_code == 404
    assert "text/html" in response["Content-Type"]
    body = response.content.decode()
    assert "Page not found" in body
    assert "Reverse for" not in body


@pytest.mark.django_db
def test_error_templates_do_not_depend_on_home_url():
    for name in ("404.html", "500.html", "403.html", "403_csrf.html"):
        source = get_template(name).template.source
        assert "url 'home'" not in source
        assert 'url "home"' not in source
        assert "extends" not in source.lower() or "base.html" not in source


@pytest.mark.django_db
def test_base_template_uses_existing_root_url():
    assert reverse("root") == "/"
    source = get_template("base.html").template.source
    assert "url 'home'" not in source
    assert "url 'about'" not in source
    assert "url 'root'" in source

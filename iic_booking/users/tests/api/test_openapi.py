from http import HTTPStatus

import pytest
from django.urls import reverse


@pytest.mark.django_db
def test_api_docs_accessible_by_admin(openapi_admin_client):
    url = reverse("api-docs")
    response = openapi_admin_client.get(url)
    assert response.status_code == HTTPStatus.OK


@pytest.mark.django_db
def test_api_docs_not_accessible_by_anonymous_users(openapi_anon_client):
    url = reverse("api-docs")
    response = openapi_anon_client.get(url)
    # Token-only auth: unauthenticated requests are challenged with 401.
    assert response.status_code == HTTPStatus.UNAUTHORIZED


@pytest.mark.django_db
def test_api_schema_generated_successfully(openapi_admin_client):
    url = reverse("api-schema")
    response = openapi_admin_client.get(url)
    assert response.status_code == HTTPStatus.OK

from http import HTTPStatus


def test_inventory_openapi_paths_present(admin_client):
    url = "/api/schema/"
    response = admin_client.get(url)
    assert response.status_code == HTTPStatus.OK

    body = response.content.decode("utf-8")

    expected_paths = [
        "/api/inventory/items/",
        "/api/inventory/equipments/{equipment_id}/stock/",
        "/api/inventory/requests/",
        "/api/inventory/requests/{request_id}/decide/",
        "/api/inventory/requests/{request_id}/issue/",
    ]
    for path in expected_paths:
        assert path in body


def test_inventory_openapi_query_params_present(admin_client):
    url = "/api/schema/"
    response = admin_client.get(url)
    assert response.status_code == HTTPStatus.OK

    body = response.content.decode("utf-8")

    # Verify documented query parameters exist in schema text.
    assert "active_only" in body
    assert "equipment_id" in body
    assert "status" in body


def test_inventory_openapi_tag_present(admin_client):
    url = "/api/schema/"
    response = admin_client.get(url)
    assert response.status_code == HTTPStatus.OK

    body = response.content.decode("utf-8")
    assert "Inventory" in body

import pytest
from rest_framework.test import APIRequestFactory

from iic_booking.users.api.views import UserViewSet
from iic_booking.users.models import User
from iic_booking.users.serializers import UserSerializer


class TestUserViewSet:
    @pytest.fixture
    def api_rf(self) -> APIRequestFactory:
        return APIRequestFactory()

    def test_get_queryset(self, user: User, api_rf: APIRequestFactory):
        view = UserViewSet()
        request = api_rf.get("/fake-url/")
        request.user = user

        view.request = request

        assert user in view.get_queryset()

    def test_me(self, user: User, api_rf: APIRequestFactory):
        view = UserViewSet()
        request = api_rf.get("/fake-url/")
        request.user = user

        view.request = request

        response = view.me(request)  # type: ignore[call-arg, arg-type, misc]

        expected = UserSerializer(user, context={"request": request}).data
        assert response.data == expected
        # Contract anchors for the current profile serializer (not cookiecutter url/name-only).
        assert response.data["id"] == user.pk
        assert response.data["email"] == user.email
        assert response.data["name"] == user.name
        assert "url" not in response.data
        assert "rbac_permissions" in response.data
        assert "admin_panel_enabled" in response.data

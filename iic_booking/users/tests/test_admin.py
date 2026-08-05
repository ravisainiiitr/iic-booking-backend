from http import HTTPStatus

import pytest
from django.contrib.auth.models import AnonymousUser
from django.urls import reverse
from pytest_django.asserts import assertRedirects

from iic_booking.users.models import User


class TestUserAdmin:
    def test_changelist(self, admin_client):
        url = reverse("admin:users_user_changelist")
        response = admin_client.get(url)
        assert response.status_code == HTTPStatus.OK

    def test_search(self, admin_client):
        url = reverse("admin:users_user_changelist")
        response = admin_client.get(url, data={"q": "test"})
        assert response.status_code == HTTPStatus.OK

    def test_add(self, admin_client):
        url = reverse("admin:users_user_add")
        response = admin_client.get(url)
        assert response.status_code == HTTPStatus.OK

        response = admin_client.post(
            url,
            data={
                "email": "new-admin@example.com",
                "name": "New Admin",
                "password1": "My_R@ndom-P@ssw0rd",
                "password2": "My_R@ndom-P@ssw0rd",
                "usable_password": "true",
                # UserDocumentInline management form (prefix="documents")
                "documents-TOTAL_FORMS": "0",
                "documents-INITIAL_FORMS": "0",
                "documents-MIN_NUM_FORMS": "0",
                "documents-MAX_NUM_FORMS": "1000",
            },
        )
        assert response.status_code == HTTPStatus.FOUND
        assert User.objects.filter(email="new-admin@example.com").exists()

    def test_view_user(self, admin_client):
        user = User.objects.get(email="admin@example.com")
        url = reverse("admin:users_user_change", kwargs={"object_id": user.pk})
        response = admin_client.get(url)
        assert response.status_code == HTTPStatus.OK

    @pytest.mark.django_db
    def test_unauthenticated_admin_redirects_to_admin_login(self, client, settings):
        """
        API-first URLConf does not mount allauth account_login.
        Unauthenticated admin traffic uses Django admin login (LOGIN_URL).
        """
        assert settings.LOGIN_URL == "admin:login"
        assert settings.DJANGO_ADMIN_FORCE_ALLAUTH is False

        request_path = reverse("admin:users_user_changelist")
        response = client.get(request_path)
        target_url = reverse(settings.LOGIN_URL) + "?next=" + request_path
        assertRedirects(response, target_url, fetch_redirect_response=False)

        # Anonymous hit of admin.site.login itself should render login (200), not reverse account_login.
        from django.contrib import admin
        from django.test import RequestFactory

        rf = RequestFactory()
        request = rf.get("/admin/login/")
        request.user = AnonymousUser()
        login_response = admin.site.login(request)
        assert login_response.status_code == HTTPStatus.OK

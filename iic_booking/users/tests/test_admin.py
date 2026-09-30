from http import HTTPStatus

import pytest
from django.contrib.auth.models import AnonymousUser
from django.forms import FileField
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

    @staticmethod
    def _change_form_data(admin_client, user):
        url = reverse("admin:users_user_change", kwargs={"object_id": user.pk})
        form = admin_client.get(url).context["adminform"].form
        data = {}
        for name, field in form.fields.items():
            if field.disabled or isinstance(field, FileField):
                continue
            value = form[name].value()
            if value is None or value is False:
                continue
            if hasattr(field.widget, "widgets"):
                for i, part in enumerate(field.widget.decompress(value)):
                    data[f"{name}_{i}"] = "" if part is None else str(part)
            elif isinstance(value, (list, tuple)):
                data[name] = [str(v) for v in value]
            else:
                data[name] = value
        data.update({
            "documents-TOTAL_FORMS": "0",
            "documents-INITIAL_FORMS": "0",
            "documents-MIN_NUM_FORMS": "0",
            "documents-MAX_NUM_FORMS": "1000",
        })
        return url, form, data

    def test_sign_in_with_email_checkbox(self, admin_client, mailoutbox):
        from iic_booking.users.models import UserType

        student = User.objects.create_user(
            email="student-toggle@example.com", password=None, name="Student", user_type=UserType.STUDENT
        )
        url, form, data = self._change_form_data(admin_client, student)
        assert form.fields["sign_in_with_email"].initial is False
        assert not form.fields["sign_in_with_email"].disabled

        data["sign_in_with_email"] = "on"
        response = admin_client.post(url, data)
        assert response.status_code == HTTPStatus.FOUND, response.context["adminform"].form.errors
        student.refresh_from_db()
        assert student.email_login_enabled is True
        assert student.is_email_login_allowed()
        assert any("turned on" in m.subject for m in mailoutbox)

    def test_untouched_checkbox_keeps_user_type_default(self, admin_client, mailoutbox):
        from iic_booking.users.models import UserType

        oic = User.objects.create_user(email="oic-toggle@example.com", password=None, name="OIC", user_type=UserType.MANAGER)
        url, form, data = self._change_form_data(admin_client, oic)
        assert form.fields["sign_in_with_email"].initial is True
        data["sign_in_with_email"] = "on"
        response = admin_client.post(url, data)
        assert response.status_code == HTTPStatus.FOUND, response.context["adminform"].form.errors
        oic.refresh_from_db()
        assert oic.email_login_enabled is None
        assert not mailoutbox

    def test_checkbox_locked_for_users_without_toggle(self, admin_client):
        from iic_booking.users.models import UserType

        external = User.objects.create_user(
            email="external-toggle@example.com", password="x-Pass-123!", name="External", user_type=UserType.EXTERNAL
        )
        _url, form, _data = self._change_form_data(admin_client, external)
        assert form.fields["sign_in_with_email"].disabled
        assert form.fields["sign_in_with_email"].initial is True

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

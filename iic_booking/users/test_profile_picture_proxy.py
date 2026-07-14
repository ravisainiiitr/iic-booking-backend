"""Tests for stable (non-expiring) profile picture proxy streaming."""

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import NoReverseMatch, reverse

from iic_booking.users.media_utils import (
    build_profile_picture_proxy_url,
    open_profile_picture_bytes,
    stream_profile_picture_response,
)
from iic_booking.users.models.user_type import UserType

User = get_user_model()


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
)
class ProfilePictureProxyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="avatar-proxy@example.com",
            password="pass12345",
            user_type=UserType.FACULTY,
        )
        self.user.profile_picture.save(
            "avatar.jpg",
            ContentFile(b"\xff\xd8\xfffakejpeg"),
            save=True,
        )

    def _proxy_url(self):
        try:
            return reverse("user-profile-picture-proxy", kwargs={"user_id": self.user.pk})
        except NoReverseMatch:
            return reverse("api:user-profile-picture-proxy", kwargs={"user_id": self.user.pk})

    def test_model_returns_proxy_not_storage_url(self):
        url = self.user.get_profile_picture_url_or_none()
        self.assertIsNotNone(url)
        self.assertIn(f"/users/{self.user.pk}/profile-picture/", url)
        self.assertNotIn("X-Amz-", url or "")
        self.assertNotIn("Signature=", url or "")

    def test_build_proxy_url_helper(self):
        path = build_profile_picture_proxy_url(self.user.pk)
        self.assertIn(str(self.user.pk), path)

    def test_open_and_stream_bytes(self):
        content, resolved, _content_type = open_profile_picture_bytes(self.user)
        self.assertTrue(content)
        self.assertTrue(resolved)
        streamed = stream_profile_picture_response(self.user)
        self.assertIsNotNone(streamed)
        self.assertEqual(streamed.status_code, 200)
        self.assertEqual(bytes(streamed.content), content)

    def test_public_proxy_endpoint_streams(self):
        res = self.client.get(self._proxy_url())
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.content.startswith(b"\xff\xd8\xff"))
        self.assertIn("image", res.get("Content-Type", ""))

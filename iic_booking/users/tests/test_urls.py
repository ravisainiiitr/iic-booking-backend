from django.urls import resolve
from django.urls import reverse

from iic_booking.users.models import User


def test_detail(user: User):
    # users.urls is mounted at site root (see config/urls.py), not under /users/.
    assert reverse("users:detail", kwargs={"pk": user.pk}) == f"/{user.pk}/"
    assert resolve(f"/{user.pk}/").view_name == "users:detail"


def test_update():
    assert reverse("users:update") == "/~update/"
    assert resolve("/~update/").view_name == "users:update"


def test_redirect():
    assert reverse("users:redirect") == "/~redirect/"
    assert resolve("/~redirect/").view_name == "users:redirect"

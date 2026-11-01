import pytest

from iic_booking.users.models import User
from iic_booking.users.tests.factories import UserFactory


@pytest.fixture(autouse=True)
def _media_storage(settings, tmpdir) -> None:
    settings.MEDIA_ROOT = tmpdir.strpath


@pytest.fixture
def user(db) -> User:
    return UserFactory()


@pytest.fixture
def midweek():
    """Run a date-sensitive test at 15:00 IST on the coming Wednesday, whatever day the suite runs: Django's clock
    (``django.utils.timezone.now``, and so ``localdate`` / ``localtime``) is moved there. Tests that need "earlier this
    week" or "a working day soon" stop failing on Mondays and weekends. Returns that moment."""
    from datetime import datetime, time, timedelta
    from unittest.mock import patch
    from zoneinfo import ZoneInfo

    from django.utils import timezone

    real_now = timezone.now
    today = timezone.localdate()
    wednesday = today + timedelta(days=(2 - today.weekday()) % 7 or 7)
    fixed = datetime.combine(wednesday, time(15, 0), tzinfo=ZoneInfo("Asia/Kolkata"))
    offset = fixed - real_now()
    with patch("django.utils.timezone.now", lambda: real_now() + offset):
        yield fixed

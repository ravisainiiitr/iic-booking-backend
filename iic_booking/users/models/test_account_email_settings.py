"""Singleton settings: where emails for is_test_account users are redirected."""

from django.db import models
from django.utils.translation import gettext_lazy as _


class TestAccountEmailSettings(models.Model):
    """
    Single-row configuration for test-account outbound email redirects.
    When a user has is_test_account=True, transactional mail is delivered to these
    addresses instead of the fake test inbox (e.g. test.student@iic-booking.test).
    """

    recipient_emails = models.TextField(
        _("Test account email redirect addresses"),
        blank=True,
        help_text=_(
            "One address per line, or comma/semicolon separated. "
            "All listed addresses receive emails generated for test accounts. "
            "If empty, falls back to the TEST_ACCOUNT_EMAIL_REDIRECT environment setting."
        ),
    )

    class Meta:
        db_table = "users_testaccountemailsettings"
        verbose_name = _("Test account email redirect settings")
        verbose_name_plural = _("Test account email redirect settings")

    def __str__(self) -> str:
        return "Test account email redirects"

    @classmethod
    def get_singleton(cls) -> "TestAccountEmailSettings":
        obj, created = cls.objects.get_or_create(
            pk=1,
            defaults={"recipient_emails": "ravisaini.15@gmail.com"},
        )
        if not created and not (obj.recipient_emails or "").strip():
            obj.recipient_emails = "ravisaini.15@gmail.com"
            obj.save(update_fields=["recipient_emails"])
        return obj

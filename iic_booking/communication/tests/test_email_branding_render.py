"""Tests for branded email rendering helpers and conditionals."""

from types import SimpleNamespace

from django.test import SimpleTestCase

from iic_booking.communication.email_branding import (
    format_duration_minutes,
    format_inr,
    user_display_name,
)
from iic_booking.communication.service import CommunicationService


class EmailBrandingFormattersTests(SimpleTestCase):
    def test_format_inr(self):
        self.assertEqual(format_inr(7080), "₹7,080.00")
        self.assertEqual(format_inr("500"), "₹500.00")
        self.assertEqual(format_inr(None), "")

    def test_format_duration(self):
        self.assertEqual(format_duration_minutes(90), "1 Hour 30 Minutes")
        self.assertEqual(format_duration_minutes(60), "1 Hour")
        self.assertEqual(format_duration_minutes(45), "45 Minutes")

    def test_user_display_name_rejects_numeric_pk(self):
        self.assertEqual(user_display_name("152", fallback="User"), "User")
        self.assertEqual(
            user_display_name(SimpleNamespace(name="152", email="a@b.com")),
            "a@b.com",
        )
        self.assertEqual(
            user_display_name(SimpleNamespace(name="Test Student", email="a@b.com")),
            "Test Student",
        )


class RenderTemplateConditionalsTests(SimpleTestCase):
    def _tpl(self, **kwargs):
        base = dict(
            subject="Booking Confirmed – {{ equipment_name }}",
            body_text="",
            body_html="",
            communication_type="email",
            sms_body="",
        )
        base.update(kwargs)
        return SimpleNamespace(**base)

    def test_missing_vars_become_empty(self):
        tpl = self._tpl(body_html="Hello {{ user_name }} / {{ missing }}")
        out = CommunicationService.render_template(tpl, {"user_name": "Ada"})
        self.assertEqual(out["html_message"], "Hello Ada / ")
        self.assertNotIn("{{", out["html_message"])

    def test_if_hides_empty_note_and_link(self):
        html = (
            "Hi {{ user_name }}"
            "{% if comment %}<div>Note: {{ comment }}</div>{% endif %}"
            "{% if link %}<a href=\"{{ link }}\">View</a>{% endif %}"
        )
        tpl = self._tpl(body_html=html)
        out = CommunicationService.render_template(
            tpl, {"user_name": "Ada", "comment": "", "link": ""}
        )
        self.assertEqual(out["html_message"], "Hi Ada")
        out2 = CommunicationService.render_template(
            tpl,
            {
                "user_name": "Ada",
                "comment": "Bring dry ice",
                "link": "https://equip.iitr.ac.in/x",
            },
        )
        self.assertIn("Note: Bring dry ice", out2["html_message"])
        self.assertIn("https://equip.iitr.ac.in/x", out2["html_message"])

    def test_subject_omits_booking_id_placeholder_when_unused(self):
        tpl = self._tpl(subject="Booking Confirmed – {{ equipment_name }}")
        out = CommunicationService.render_template(
            tpl, {"equipment_name": "MALDI-TOF", "booking_id": "IIC-1"}
        )
        self.assertEqual(out["subject"], "Booking Confirmed – MALDI-TOF")
        self.assertNotIn("IIC-1", out["subject"])

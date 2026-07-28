"""Tests for branded email rendering helpers and conditionals."""

from types import SimpleNamespace

from django.test import SimpleTestCase

from iic_booking.communication.email_branding import (
    build_booking_created_event_comment,
    format_duration_minutes,
    format_inr,
    format_on_behalf_of_user,
    scrub_internal_user_ids_from_text,
    sanitize_template_context,
    wrap_email_html,
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

    def test_on_behalf_of_uses_name_not_id(self):
        user = SimpleNamespace(
            id=76,
            name="Rahul Sharma",
            email="rahul.sharma@iitr.ac.in",
            department=SimpleNamespace(name="Department of Mechanical Engineering"),
        )
        text = format_on_behalf_of_user(user)
        self.assertIn("Rahul Sharma", text)
        self.assertIn("Department of Mechanical Engineering", text)
        self.assertIn("Indian Institute of Technology Roorkee", text)
        self.assertNotIn("76", text)
        self.assertNotIn("user 76", text.lower())

    def test_on_behalf_fallback_to_email(self):
        user = SimpleNamespace(id=76, name="", email="rahul.sharma@iitr.ac.in", department=None)
        text = format_on_behalf_of_user(user)
        self.assertIn("rahul.sharma@iitr.ac.in", text)
        self.assertNotIn("76", text)

    def test_booking_created_comment_never_exposes_user_pk(self):
        booking_user = SimpleNamespace(
            id=76,
            name="Test IITR Student",
            email="student@iitr.ac.in",
            department=SimpleNamespace(name="IIC"),
        )
        staff = SimpleNamespace(id=1, name="Officer", email="oic@iitr.ac.in")
        comment = build_booking_created_event_comment(
            equipment_name="TGA/DTA [A]",
            total_time_minutes=135,
            total_charge=100,
            booking_user=booking_user,
            created_by=staff,
        )
        self.assertIn("Test IITR Student", comment)
        self.assertIn("This booking was created on behalf of:", comment)
        self.assertNotIn("user 76", comment)
        self.assertNotRegex(comment, r"\b76\b")

    def test_scrub_legacy_on_behalf_user_id(self):
        cleaned = scrub_internal_user_ids_from_text(
            "Booking created for TGA (135 minutes) on behalf of user 76."
        )
        self.assertNotIn("user 76", cleaned.lower())
        self.assertIn("on behalf of another user", cleaned.lower())

    def test_sanitize_scrubs_comment_user_ids(self):
        ctx = sanitize_template_context(
            {"comment": "Hold created for SEM on behalf of user 99", "user_name": "Ada"}
        )
        self.assertNotIn("user 99", ctx["comment"].lower())


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

    def test_shared_email_wrapper_uses_iit_roorkee_branding(self):
        html = wrap_email_html(title="Sample Disposed", body_inner_html="<p>Done</p>")
        self.assertIn("Indian Institute of Technology Roorkee", html)
        self.assertIn("भारतीय प्रौद्योगिकी संस्थान रुड़की", html)
        self.assertIn("Equipment Booking Portal", html)
        self.assertIn("Sample Disposed", html)
        self.assertIn("org-name-en", html)
        self.assertIn("white-space: nowrap", html)

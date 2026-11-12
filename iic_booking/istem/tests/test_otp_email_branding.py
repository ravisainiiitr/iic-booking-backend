"""The I-STEM OTP emails use the standard branded layout, keep the code out of the communication log, and still go out if the template fails."""

from __future__ import annotations

import re
from unittest import mock

import pytest
from django.core.cache import cache

from iic_booking.communication.models import CommunicationLog, CommunicationTemplate
from iic_booking.istem import otp_email, otp_link
from iic_booking.istem import registration_lookup as reg
from iic_booking.istem.models import IstemUserLink
from iic_booking.istem.choices import UserLinkState
from iic_booking.users.tests.factories import UserFactory

from .test_registration_lookup import _client_factory, on  # noqa: F401
from .test_service import complete_profile, live  # noqa: F401

pytestmark = pytest.mark.django_db
BANNED = ("IIC Booking", "Institute Instrumentation Centre")


def _html(message) -> str:
    return next(body for body, kind in message.alternatives if kind == "text/html")


def _otp(text: str) -> str:
    return re.search(r"\b(\d{6})\b", text).group(1)


def test_registration_email_is_branded_and_the_code_is_not_logged(on, mailoutbox):  # noqa: F811
    reg.start("asha@lab.example.com", "", client_factory=_client_factory())
    message = mailoutbox[-1]
    assert message.subject == "Your OTP to register with your I-STEM account"
    assert "IIT Roorkee Equipment Booking Portal" in message.body and "expires in 5 minutes" in message.body
    code = _otp(message.body)
    html = _html(message)
    assert code in html and "<strong>" in html and "Verify Your I-STEM Account" in html and "a***@" in html
    for word in BANNED:
        assert word not in message.subject and word not in message.body.split("This code")[0]
    assert "{{" not in message.body and "{{" not in html and "{%" not in html
    assert not CommunicationLog.objects.filter(message__contains=code).exists()


def test_link_email_is_branded_and_the_code_is_not_logged(live, mailoutbox):  # noqa: F811
    live.ISTEM_OTP_LINKING_ENABLED = True
    user = UserFactory()
    IstemUserLink.objects.create(user=user, state=UserLinkState.AWAITING_OTP, candidate_public_user_id="60016", candidate_email="asha.registered@example.com")
    otp_link.send_otp(user)
    message = mailoutbox[-1]
    assert message.subject == "Your OTP to link your I-STEM account"
    code = _otp(message.body)
    assert code in _html(message) and "Link Your I-STEM Account" in _html(message)
    assert not CommunicationLog.objects.filter(message__contains=code).exists()


def test_an_administrators_wording_is_used(on, mailoutbox):  # noqa: F811
    CommunicationTemplate.objects.filter(code="istem_registration_otp_email").update(subject="Custom subject")
    reg.start("asha@lab.example.com", "", client_factory=_client_factory())
    assert mailoutbox[-1].subject == "Custom subject"


def test_before_the_migration_runs_the_catalogue_is_used(on, mailoutbox):  # noqa: F811
    CommunicationTemplate.objects.filter(code="istem_registration_otp_email").delete()
    reg.start("asha@lab.example.com", "", client_factory=_client_factory())
    assert mailoutbox[-1].subject == "Your OTP to register with your I-STEM account"


def test_a_broken_template_still_sends_the_code(on, mailoutbox):  # noqa: F811
    cache.clear()
    with mock.patch.object(otp_email, "_rendered", side_effect=RuntimeError("boom")):
        started = reg.start("asha@lab.example.com", "", client_factory=_client_factory())
    message = mailoutbox[-1]
    assert _otp(message.body) and "IIT Roorkee Equipment Booking Portal" in message.body and started["token"]
    assert not message.alternatives


def test_the_notice_template_is_branded():
    from iic_booking.communication.default_email_templates import get_default_email_template

    spec = get_default_email_template("external_service_notice")
    assert spec["subject"] == "{{ title }}"
    assert "IIC Booking Team" not in spec["body_text"] and "{{ message }}" in spec["body_html"] and "{{ link }}" in spec["body_html"]

"""Guarded intro update for booking_charge_recalculated_email (migration 0054)."""

import importlib
from unittest import mock

import pytest
from django.apps import apps as django_apps

from iic_booking.communication.default_email_templates import get_default_email_templates
from iic_booking.communication.models import CommunicationTemplate

mig = importlib.import_module(
    "iic_booking.communication.migrations.0054_charge_recalculated_refund_window_intro"
)


def _catalog_spec():
    return next(s for s in get_default_email_templates() if s["code"] == mig.CODE)


def _old_spec():
    spec = dict(_catalog_spec())
    spec["body_text"] = spec["body_text"].replace(mig.NEW_INTRO, mig.OLD_INTRO)
    spec["body_html"] = spec["body_html"].replace(mig.NEW_INTRO, mig.OLD_INTRO)
    return spec


def _digest(spec):
    return mig.template_digest(spec["subject"], spec["body_text"], spec["body_html"])


@pytest.fixture
def digests():
    with mock.patch.object(mig, "OLD_DEFAULT_SHA256", _digest(_old_spec())), mock.patch.object(
        mig, "NEW_DEFAULT_SHA256", _digest(_catalog_spec())
    ):
        yield


def _store(spec):
    CommunicationTemplate.objects.filter(code=mig.CODE).delete()
    return CommunicationTemplate.objects.create(
        code=mig.CODE,
        name=spec["name"],
        communication_type="email",
        subject=spec["subject"],
        body_text=spec["body_text"],
        body_html=spec["body_html"],
    )


def test_catalog_intro_is_the_new_text():
    spec = _catalog_spec()
    assert mig.NEW_INTRO in spec["body_text"] and mig.NEW_INTRO in spec["body_html"]
    assert mig.OLD_INTRO not in spec["body_html"]


@pytest.mark.django_db
def test_untouched_old_default_gets_new_intro_and_reverts(digests):
    old = _old_spec()
    tpl = _store(old)
    mig.forwards(django_apps, None)
    tpl.refresh_from_db()
    assert mig.NEW_INTRO in tpl.body_text and mig.NEW_INTRO in tpl.body_html
    assert mig.OLD_INTRO not in tpl.body_html

    mig.backwards(django_apps, None)
    tpl.refresh_from_db()
    assert tpl.body_text == old["body_text"] and tpl.body_html == old["body_html"]


@pytest.mark.django_db
def test_crlf_only_difference_still_counts_as_default(digests):
    old = _old_spec()
    old["body_text"] = old["body_text"].replace("\n", "\r\n")
    tpl = _store(old)
    mig.forwards(django_apps, None)
    tpl.refresh_from_db()
    assert mig.NEW_INTRO in tpl.body_text and mig.NEW_INTRO in tpl.body_html


@pytest.mark.django_db
def test_customised_template_is_left_alone(digests):
    custom = _old_spec()
    custom["body_html"] = custom["body_html"].replace("Pay Now", "Pay now from the booking page")
    tpl = _store(custom)
    mig.forwards(django_apps, None)
    tpl.refresh_from_db()
    assert tpl.body_html == custom["body_html"]
    assert mig.NEW_INTRO not in tpl.body_text

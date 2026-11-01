"""Website policy pages: drafts are seeded unpublished, never overwritten, and only published ones are listed."""

import pytest
from django.core.management import call_command
from rest_framework.test import APIClient

from iic_booking.cms.models import CmsPage
from iic_booking.cms.policy_pages import POLICY_SLUGS

pytestmark = pytest.mark.django_db

URL = "/api/cms/policy-pages/"


def test_seed_creates_unpublished_drafts_and_never_overwrites(capsys):
    CmsPage.objects.create(slug="privacy-policy", title="Approved privacy", content=[], is_published=True)
    call_command("seed_policy_pages", "--dry-run")
    assert not CmsPage.objects.filter(slug="terms-and-conditions").exists()
    call_command("seed_policy_pages")
    pages = {p.slug: p for p in CmsPage.objects.filter(slug__in=POLICY_SLUGS)}
    assert set(pages) == set(POLICY_SLUGS)
    assert pages["privacy-policy"].title == "Approved privacy" and pages["privacy-policy"].is_published
    assert not pages["terms-and-conditions"].is_published and pages["terms-and-conditions"].content
    assert all(b["type"] in {"heading", "paragraph", "list"} for b in pages["refund-policy"].content)


def test_only_published_policy_pages_are_listed_in_order():
    assert APIClient().get(URL).json() == {"pages": []}
    call_command("seed_policy_pages")
    CmsPage.objects.filter(slug__in=["refund-policy", "terms-and-conditions"]).update(is_published=True)
    CmsPage.objects.create(slug="unrelated", title="Other", content=[], is_published=True)
    assert APIClient().get(URL).json() == {"pages": [
        {"slug": "terms-and-conditions", "title": "Terms and Conditions"},
        {"slug": "refund-policy", "title": "Refund and Cancellation Policy"},
    ]}
    assert APIClient().get("/api/cms/pages/privacy-policy/").status_code == 404  # drafts stay hidden

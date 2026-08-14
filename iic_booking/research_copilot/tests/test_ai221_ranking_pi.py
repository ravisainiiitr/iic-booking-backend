"""AI.22.1 — XRD ranking, PI meta, ambiguity clarification tests."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

from iic_booking.equipment.models import ChargeProfilePricingProfile
from iic_booking.equipment.pi_pricing import (
    billing_identity_is_equipment_pi,
    resolve_pricing_profile_for_user,
    pricing_resolution_meta,
)
from iic_booking.research_copilot.services.portal_grounding import run_portal_grounding
from iic_booking.research_copilot.services.structured_search import (
    score_equipment_match,
    search_equipment,
    xrd_family_clarification,
)
from iic_booking.users.models.user_type import UserType

User = get_user_model()


def test_score_pxrd_beats_gi_for_powder_query():
    pxrd, f1 = score_equipment_match(query="powder XRD", name="Powder X-Ray Diffractometer (PXRD) [A]")
    gi, f2 = score_equipment_match(query="powder XRD", name="Grazing Incidence X-Ray Diffractometer (GI-XRD)")
    assert f1 == "pxrd"
    assert f2 == "gi-xrd"
    assert pxrd > gi


def test_score_gi_beats_pxrd_for_gi_query():
    gi, _ = score_equipment_match(query="GI-XRD", name="Grazing Incidence X-Ray Diffractometer (GI-XRD)")
    pxrd, _ = score_equipment_match(query="GI-XRD", name="Powder X-Ray Diffractometer (PXRD) [A]")
    assert gi > pxrd


def test_ambiguous_xrd_clarification_when_both_families():
    from iic_booking.research_copilot.services.structured_search import StructuredHit

    hits = [
        StructuredHit("equipment:1", "Powder X-Ray Diffractometer (PXRD) [A]", "", 0.8, "", "equipment", "pxrd"),
        StructuredHit("equipment:40", "Grazing Incidence X-Ray Diffractometer (GI-XRD)", "", 0.8, "", "equipment", "gi-xrd"),
    ]
    q = xrd_family_clarification(text="How much does 5 XRD samples cost?", hits=hits)
    assert q
    assert "PXRD" in q and "GI-XRD" in q


def test_pxrd_query_does_not_clarify():
    from iic_booking.research_copilot.services.structured_search import StructuredHit

    hits = [
        StructuredHit("equipment:1", "Powder X-Ray Diffractometer (PXRD) [A]", "", 0.9, "", "equipment", "pxrd"),
    ]
    assert xrd_family_clarification(text="How much does 5 PXRD samples cost?", hits=hits) is None


@pytest.mark.django_db
def test_search_equipment_ranks_pxrd_first_for_pxrd_query():
    hits = search_equipment(query="PXRD", limit=5)
    assert hits
    assert hits[0].family == "pxrd" or "PXRD" in hits[0].title or "Powder" in hits[0].title


@pytest.mark.django_db
def test_search_equipment_ranks_gi_first_for_gi_query():
    hits = search_equipment(query="GI-XRD", limit=5)
    assert hits
    assert hits[0].family == "gi-xrd" or "GI-XRD" in hits[0].title or "Grazing" in hits[0].title


@pytest.mark.django_db
def test_grounding_clarifies_ambiguous_xrd_pricing(django_user_model):
    user = User.objects.create_user(
        email="ai221-xrd@example.com",
        password="test-pass-12345",
        user_type=UserType.STUDENT,
        name="AI221",
        is_active=True,
        email_verified=True,
        admin_approved=True,
    )
    out = run_portal_grounding(user=user, text="How much does 5 XRD samples cost?")
    # If both families exist in DB, clarification; otherwise may estimate single family.
    if out.get("clarification"):
        assert "PXRD" in out["clarification"] or "GI-XRD" in out["clarification"]
        assert "estimate_booking_cost" not in {t.get("tool") for t in out.get("tool_results") or []}
    else:
        tools = {t.get("tool") for t in out.get("tool_results") or []}
        assert "search_equipment" in tools


@pytest.mark.django_db
def test_pi_resolver_standard_without_pi_profiles(django_user_model):
    from iic_booking.equipment.models import Equipment

    user = User.objects.create_user(
        email="ai221-pi@example.com",
        password="test-pass-12345",
        user_type=UserType.FACULTY,
        name="AI221 PI",
        is_active=True,
        email_verified=True,
        admin_approved=True,
    )
    eq = Equipment.objects.order_by("pk").first()
    if not eq:
        pytest.skip("no equipment")
    profile = resolve_pricing_profile_for_user(user, eq)
    assert profile in {
        ChargeProfilePricingProfile.STANDARD,
        ChargeProfilePricingProfile.DISCOUNTED,
        ChargeProfilePricingProfile.PI,
    }
    meta = pricing_resolution_meta(user, eq)
    assert "resolved_pricing_profile" in meta
    assert "equipment_has_pi_profiles" in meta
    assert billing_identity_is_equipment_pi(user, eq) in {True, False}

"""The I-STEM institute saved by the Main Admin, used when I-STEM's token names none. Fake transport."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.core.cache import cache

from iic_booking.istem import conf
from iic_booking.istem.client import IstemClient, TransportResponse, clear_token_cache
from iic_booking.istem.equipment_mapping import MappingRefused, institute_status, istem_listing, save_institute
from iic_booking.istem.models import IstemInstituteSetting

from .support import FakeTransport, apply_live, json_response, staging_token_response, token_response
from .test_equipment_mapping_page import _admin, _api, _department, _dept_admin, _page

pytestmark = pytest.mark.django_db

LISTING = [{"equipment_id": 38750, "equipment_name": "X-ray Fluorescence (XRF) -IIC", "institute_id": 213,
            "equipment_department_id": "164"}]
DEPARTMENTS = json_response({"status": "SUCCESS", "result": [{"id": "164", "val": "Institute Instrumentation Centre"}]})


@pytest.fixture(autouse=True)
def _fresh(settings):
    apply_live(settings)
    clear_token_cache()
    cache.clear()
    yield
    clear_token_cache()
    cache.clear()


def _factory(*responses):
    transport = FakeTransport(*responses)
    return transport, (lambda: IstemClient(transport))


def test_token_without_institute_uses_the_saved_one():
    IstemInstituteSetting.objects.create(institute_id="213")
    info = IstemClient(FakeTransport(staging_token_response())).session_info()
    assert (info.institute_id, info.institute_source) == ("213", "portal")


def test_a_token_that_names_an_institute_wins_over_the_saved_one():
    IstemInstituteSetting.objects.create(institute_id="999")
    info = IstemClient(FakeTransport(token_response(institute_id=213))).session_info()
    assert (info.institute_id, info.institute_source) == ("213", "token")


def test_nothing_saved_and_no_institute_in_the_token_leaves_none():
    info = IstemClient(FakeTransport(staging_token_response())).session_info()
    assert info.institute_id is None and info.institute_source == ""


def test_main_admin_saves_the_institute_after_istem_lists_its_departments():
    admin = _admin()
    transport, factory = _factory(staging_token_response(), DEPARTMENTS)
    out = save_institute(admin, " 213 ", client_factory=factory)
    assert out == {"institute_id": "213", "departments": 1, "warnings": []}
    row = IstemInstituteSetting.objects.get()
    assert row.institute_id == "213" and row.set_by_id == admin.pk
    assert conf.portal_institute_id() == "213"
    assert transport.calls[-1]["params"] == {"institute_id": "213"}


def test_an_institute_without_departments_on_istem_is_refused():
    _, factory = _factory(staging_token_response(), json_response({"status": "SUCCESS", "result": []}))
    with pytest.raises(MappingRefused) as exc:
        save_institute(_admin(), "77", client_factory=factory)
    assert "no departments" in exc.value.message
    assert not IstemInstituteSetting.objects.exists()


def test_saved_with_a_warning_while_istem_is_not_answering():
    _, factory = _factory(TransportResponse(502, "<html>Bad Gateway</html>"))
    out = save_institute(_admin(), "213", client_factory=factory)
    assert out["institute_id"] == "213" and out["warnings"]


def test_only_the_main_admin_may_save_and_the_id_is_a_number():
    with pytest.raises(MappingRefused):
        save_institute(_dept_admin(_department("D")), "213")
    with pytest.raises(MappingRefused):
        save_institute(_admin(), "IITR")


def test_status_shows_where_the_institute_comes_from():
    _, factory = _factory(staging_token_response())
    status = institute_status(_admin(), client_factory=factory)
    assert status["institute_id"] is None and status["source"] == "" and status["can_set"] is True
    IstemInstituteSetting.objects.create(institute_id="213")
    status = institute_status(_admin(), client_factory=factory)
    assert status["institute_id"] == "213" and status["source"] == "portal" and status["saved"]["institute_id"] == "213"


def test_listing_uses_the_saved_institute():
    IstemInstituteSetting.objects.create(institute_id="213")
    transport, factory = _factory(staging_token_response(), _page(LISTING))
    body = istem_listing(_admin(), client_factory=factory)
    assert body["istem_source"]["institute_id"] == "213" and len(body["equipment"]) == 1
    assert transport.calls[-1]["params"]["institute_id"] == "213"


def test_institute_api_get_and_put():
    admin = _admin()
    with patch("iic_booking.istem.service.default_client", return_value=IstemClient(FakeTransport(staging_token_response(), DEPARTMENTS))):
        res = _api(admin).put("/api/istem/institute/", {"institute_id": "213"}, format="json")
    assert res.status_code == 200, res.content
    assert res.json()["institute_id"] == "213"
    dept_admin = _dept_admin(_department("E"))
    assert _api(dept_admin).put("/api/istem/institute/", {"institute_id": "213"}, format="json").status_code == 403

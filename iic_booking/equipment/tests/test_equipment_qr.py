"""Equipment QR codes: canonical URL, PNG/SVG output, the public allowlisted page data, directions and role scope."""

import io
import zipfile
from decimal import Decimal
from xml.etree import ElementTree

import pytest
from PIL import Image
from rest_framework.test import APIClient

from iic_booking.equipment import equipment_qr as qr
from iic_booking.equipment.models import EquipmentAccessory
from iic_booking.equipment.models import EquipmentCategory
from iic_booking.equipment.models import EquipmentSpecification
from iic_booking.training.tests.conftest import client_for  # noqa: F401
from iic_booking.training.tests.conftest import make_department  # noqa: F401
from iic_booking.training.tests.conftest import make_equipment  # noqa: F401
from iic_booking.training.tests.conftest import world  # noqa: F401
from iic_booking.users.models.user_group import UserGroup

pytestmark = pytest.mark.django_db
BASE = "https://equip.example.ac.in"


@pytest.fixture(autouse=True)
def canonical(settings):
    settings.PUBLIC_EQUIPMENT_BASE_URL = BASE + "/"
    settings.FRONTEND_URL = "http://localhost:8080"


# --------------------------------------------------------------------------- canonical URL and images
def test_canonical_url_uses_the_configured_public_domain_and_survives_renames(world, settings):
    eq = world.equipment
    assert qr.public_equipment_url(eq.pk) == f"{BASE}/equipment/{eq.pk}"
    eq.name = "Renamed — FE-SEM (Zeiss) & co. / 2026"
    eq.save()
    assert qr.public_equipment_url(eq.pk) == f"{BASE}/equipment/{eq.pk}"
    assert qr.base_url_warning() == ""
    settings.PUBLIC_EQUIPMENT_BASE_URL = ""
    assert qr.public_equipment_url(eq.pk) == f"http://localhost:8080/equipment/{eq.pk}"
    assert "local address" in qr.base_url_warning()
    settings.FRONTEND_URL = ""
    assert "not set" in qr.base_url_warning()


def test_png_and_svg_are_valid_and_carry_the_url(world):
    url = qr.public_equipment_url(world.equipment.pk)
    png = qr.qr_png(url, "large")
    img = Image.open(io.BytesIO(png))
    assert img.format == "PNG" and img.size[0] == img.size[1] and img.size[0] >= 29 * 32
    # Quiet zone: the outer 4 modules are white.
    px = img.convert("L")
    assert all(px.getpixel((x, 5)) == 255 for x in range(0, img.size[0], 7))
    root = ElementTree.fromstring(qr.qr_svg(url))
    assert root.tag.endswith("svg") and root.find(".//{http://www.w3.org/2000/svg}path") is not None
    # Same data, same symbol: the code depends only on the URL.
    assert qr.qr_png(url) == qr.qr_png(url)


def test_qr_payload_is_exactly_the_canonical_https_url_with_no_redirect(world, settings):
    settings.PUBLIC_EQUIPMENT_BASE_URL = "https://equip.iitr.ac.in"
    eq = world.equipment
    url = f"https://equip.iitr.ac.in/equipment/{eq.pk}"
    symbol = qr._qr(qr.public_equipment_url(eq.pk))
    assert [d.data for d in symbol.data_list] == [url.encode()]  # the whole payload: no tracking, no shortener
    r = client_for(world.admin).get(f"/api/equipment-qr/{eq.pk}.png?size=large")
    assert r["X-QR-URL"] == url and r.content == qr.qr_png(url, "large")
    for path in ("/api/", "/admin", "token", "redirect", "?"):
        assert path not in qr.public_equipment_url(eq.pk)
    page = APIClient().get(f"/api/equipments/{eq.pk}/public/")
    assert page.status_code == 200 and page.json()["public_url"] == url  # answered directly, not redirected


def test_png_svg_endpoints_and_filenames(world):
    eq = world.equipment
    r = client_for(world.oic).get(f"/api/equipment-qr/{eq.pk}.png?size=small&download=1")
    assert r.status_code == 200 and r["Content-Type"] == "image/png" and r["X-QR-URL"] == f"{BASE}/equipment/{eq.pk}"
    assert r["Content-Disposition"].startswith("attachment;") and f"equipment-{eq.pk}-" in r["Content-Disposition"]
    s = client_for(world.oic).get(f"/api/equipment-qr/{eq.pk}.svg")
    assert s.status_code == 200 and s["Content-Type"] == "image/svg+xml" and s["Content-Disposition"].startswith("inline;")
    assert client_for(world.oic).get("/api/equipment-qr/999999.png").status_code == 404


# --------------------------------------------------------------------------- public endpoint
PRIVATE_KEYS = {"operators", "managers", "charge_profiles", "base_charges_by_user_type", "alternate_phone_number",
                "fabrication_notification_emails", "results_base_location", "analysis_raw_data_directory",
                "booking_email_extra_text", "visibility_group", "input_fields", "slot_masters", "equipment_pis"}


def test_public_page_data_is_an_allowlist_and_needs_no_login(world):
    eq = world.equipment
    eq.description = "Field-emission SEM for imaging."
    eq.important_instruction = "Bring samples mounted on stubs."
    eq.make, eq.model_information, eq.location = "Zeiss", "Gemini 300", "Lab 12, Ground floor"
    eq.office_address = "IIC office, Room 101"
    eq.alternate_phone_number = "+91 99999 00000"
    eq.fabrication_notification_emails = "staff@example.ac.in"
    eq.category = EquipmentCategory.objects.create(name="Electron Microscopy")
    eq.save()
    EquipmentSpecification.objects.create(equipment=eq, spec_key="Resolution", spec_value="0.8 nm at 15 kV")
    EquipmentSpecification.objects.create(equipment=eq, spec_key="Empty", spec_value="  ")
    EquipmentAccessory.objects.create(equipment=eq, accessory_name="EDS detector", is_optional=True)
    EquipmentAccessory.objects.create(equipment=eq, accessory_name="Hidden", is_enabled=False)

    r = APIClient().get(f"/api/equipments/{eq.pk}/public/", HTTP_AUTHORIZATION="Token stale-token")
    assert r.status_code == 200
    d = r.json()
    assert set(d) == {"equipment_id", "code", "name", "description", "important_instruction", "status", "category",
                      "department", "make", "model", "image_url", "specifications", "accessories", "location",
                      "directions", "enquiry_address", "public_url", "booking_path", "charges_path"}
    assert not PRIVATE_KEYS & set(d)
    body = r.content.decode()
    assert "99999" not in body and "staff@example" not in body and world.oic.email not in body
    assert d["specifications"] == [{"label": "Resolution", "value": "0.8 nm at 15 kV"}]
    assert d["accessories"] == [{"name": "EDS detector", "optional": True}]
    assert d["department"]["name"] == world.dept.name and d["category"] == "Electron Microscopy"
    assert d["status"] == {"key": "operational", "label": "Operational"}
    assert d["booking_path"] == f"/book-equipment?equipment_id={eq.pk}" and d["public_url"] == f"{BASE}/equipment/{eq.pk}"
    assert d["enquiry_address"] == "IIC office, Room 101"

    eq.status = "REPAIR"
    eq.save()
    assert APIClient().get(f"/api/equipments/{eq.pk}/public/").json()["status"] == {
        "key": "unavailable", "label": "Temporarily unavailable"}


def test_restricted_and_missing_equipment_get_the_same_not_available_answer(world):
    group = UserGroup.objects.create(name="Private group")
    hidden = make_equipment(world.dept, name="Restricted NMR", visibility_group=group)
    test_only = make_equipment(world.dept, name="Test-only rig", visible_to_test_accounts_only=True)
    off_dept = make_department(name="Hidden Dept")
    off_dept.equipment_visibility_enabled = False
    off_dept.save()
    hidden_dept = make_equipment(off_dept, name="Dept hidden")
    for pk in (hidden.pk, test_only.pk, hidden_dept.pk, 987654):
        r = APIClient().get(f"/api/equipments/{pk}/public/")
        assert r.status_code == 404 and r.json() == {"error": "This equipment page is not available.", "code": "not_available"}
    assert "Restricted NMR" not in APIClient().get(f"/api/equipments/{hidden.pk}/public/").content.decode()


def test_minimal_equipment_has_empty_optional_fields(world):
    eq = make_equipment(world.dept, name="Bare instrument")
    d = APIClient().get(f"/api/equipments/{eq.pk}/public/").json()
    assert d["specifications"] == [] and d["accessories"] == [] and d["description"] == "" and d["make"] == ""
    assert d["directions"] == {"maps_url": "", "source": "", "latitude": None, "longitude": None, "embed_url": ""}


# --------------------------------------------------------------------------- directions
@pytest.mark.parametrize("raw, ok", [
    ("https://maps.app.goo.gl/AbCdEf123", True),
    ("https://www.google.com/maps/place/IIT+Roorkee/@29.86,77.89,17z", True),
    ("https://maps.google.co.in/maps?q=29.86,77.89", True),
    ("http://goo.gl/maps/xyz", True),
    ("https://www.google.com/url?q=https://evil.example", False),  # Google redirector, not Maps
    ("https://evil.example/maps", False),
    ("javascript:alert(1)", False),
    ("https://user:pw@maps.google.com/maps", False),
    ("", False),
])
def test_only_google_maps_links_are_used(raw, ok):
    out = qr.safe_google_maps_url(raw)
    assert bool(out) is ok
    if ok:
        assert out.startswith("https://")


def test_directions_prefer_the_stored_link_then_coordinates(world):
    eq = world.equipment
    eq.latitude, eq.longitude = Decimal("29.8649000"), Decimal("77.8964000")
    eq.save()
    d = qr.directions(eq)
    assert d["source"] == "coordinates"
    assert d["maps_url"] == "https://www.google.com/maps/dir/?api=1&destination=29.8649%2C77.8964"
    assert d["embed_url"].startswith("https://www.google.com/maps?q=29.8649%2C77.8964") and d["latitude"] == 29.8649
    eq.google_maps_url = "https://maps.app.goo.gl/AbCdEf123"
    eq.save()
    assert qr.directions(eq)["maps_url"] == "https://maps.app.goo.gl/AbCdEf123"
    eq.google_maps_url, eq.latitude, eq.longitude = "https://evil.example/x", Decimal("0"), Decimal("0")
    eq.save()
    assert qr.directions(eq)["maps_url"] == ""  # unsafe link and "0,0" placeholder both ignored


# --------------------------------------------------------------------------- management scope
def test_qr_management_is_for_admin_and_oic_only(world):
    url = "/api/equipment-qr/"
    assert APIClient().get(url).status_code in (401, 403)
    for u in (world.student, world.faculty, world.operator):
        assert client_for(u).get(url).status_code == 403
        assert client_for(u).get(f"/api/equipment-qr/{world.equipment.pk}.png").status_code == 403
        assert client_for(u).get(f"/api/equipment-qr/batch.zip?ids={world.equipment.pk}").status_code == 403

    oic = client_for(world.oic).get(url).json()
    assert {r["equipment_id"] for r in oic["results"]} == {world.equipment.pk}
    assert oic["results"][0]["public_url"] == f"{BASE}/equipment/{world.equipment.pk}" and oic["base_url_warning"] == ""
    assert client_for(world.oic).get(f"/api/equipment-qr/{world.other_equipment.pk}.svg").status_code == 404
    assert client_for(world.temp_oic).get(url).json()["count"] >= 1  # temporary OIC while the primary is away

    admin = client_for(world.admin)
    ids = {r["equipment_id"] for r in admin.get(url).json()["results"]}
    assert {world.equipment.pk, world.other_equipment.pk} <= ids
    by_dept = admin.get(f"{url}?department_id={world.other_dept.pk}").json()["results"]
    assert {r["equipment_id"] for r in by_dept} == {world.other_equipment.pk}
    assert [r["equipment_id"] for r in admin.get(f"{url}?q={world.other_equipment.pk}").json()["results"]] == [world.other_equipment.pk]
    assert world.equipment.pk in {r["equipment_id"] for r in admin.get(f"{url}?q=FESEM").json()["results"]}
    depts = {d["id"] for d in admin.get(url).json()["departments"]}
    assert {world.dept.pk, world.other_dept.pk} <= depts


def test_batch_zip_respects_scope(world):
    ids = f"{world.equipment.pk},{world.other_equipment.pk}"
    r = client_for(world.oic).get(f"/api/equipment-qr/batch.zip?ids={ids}")
    assert r.status_code == 200 and r["Content-Type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    assert sum(n.endswith(".png") for n in names) == 1 and sum(n.endswith(".svg") for n in names) == 1
    readme = zf.read("README.txt").decode()
    assert f"{BASE}/equipment/{world.equipment.pk}" in readme and "Skipped" in readme and str(world.other_equipment.pk) in readme
    assert client_for(world.oic).get("/api/equipment-qr/batch.zip").status_code == 400
    admin_zip = zipfile.ZipFile(io.BytesIO(client_for(world.admin).get(f"/api/equipment-qr/batch.zip?ids={ids}").content))
    assert sum(n.endswith(".png") for n in admin_zip.namelist()) == 2

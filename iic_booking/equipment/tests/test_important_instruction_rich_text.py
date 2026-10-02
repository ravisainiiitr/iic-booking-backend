"""OIC-formatted important instruction: HTML is sanitized, and per-user-type text overrides the default."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from iic_booking.equipment.admin import EquipmentAdminForm
from iic_booking.equipment.models import EquipmentManager
from iic_booking.equipment.rich_text import clean_important_instruction
from iic_booking.equipment.rich_text import font_token
from iic_booking.equipment.rich_text import palette_color
from iic_booking.equipment.rich_text import rich_text_to_plain
from iic_booking.equipment.rich_text import sanitize_rich_text
from iic_booking.equipment.serializers import EquipmentAdminWriteSerializer
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


def _client(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def test_sanitizer_keeps_editor_formatting():
    html = (
        '<h3 style="text-align: center">Before you book</h3>'
        "<p><strong>Dry</strong> <em>samples</em> <u>only</u> <s>no liquids</s><br>second line</p>"
        "<ul><li><p>One</p><ul><li><p>Nested</p></li></ul></li></ul>"
        '<ol start="3"><li><p>Three</p></li></ol>'
        "<h4>Large text</h4>"
        '<p style="text-align: right"><span style="color: var(--rt-red)">red</span> '
        '<mark style="background-color: var(--rt-hl-yellow)">highlight</mark></p>'
        '<p><a href="https://iitr.ac.in/x?a=1&amp;b=2" target="_blank" rel="noopener noreferrer">site</a></p>'
    )
    assert sanitize_rich_text(html) == html


def test_sanitizer_drops_scripts_handlers_and_arbitrary_styles():
    dirty = (
        '<p style="color: rgb(185, 28, 28); font-family: Comic Sans MS; position: fixed; font-size: 40px">'
        '<b onclick="steal()">Dry</b> <i onmouseover="x()">samples</i></p>'
        "<script>alert(1)</script><img src=x onerror=alert(1)><svg><script>alert(2)</script></svg>"
        '<iframe src="https://evil"></iframe><style>p{color:red}</style><!-- c -->'
        '<span style="background-image: url(javascript:alert(1)); color: expression(alert(1))">x</span>'
        '<table><tr><td>cell</td></tr></table>'
    )
    clean = sanitize_rich_text(dirty)
    assert clean == "<p><strong>Dry</strong> <em>samples</em></p>xcell"
    for bad in ("script", "onerror", "onclick", "iframe", "font-family", "position", "url(", "expression", "<!--"):
        assert bad not in clean


def test_links_are_limited_to_http_and_mailto_and_open_in_a_new_tab():
    clean = sanitize_rich_text(
        '<p><a href="https://ok.example/a" target="_self" onclick="x()">ok</a> '
        '<a href="mailto:lab@iitr.ac.in">mail</a> <a href="javascript:alert(1)">js</a> '
        '<a href="JaVaScRiPt:alert(1)">js2</a> <a href="data:text/html,x">data</a> <a href="/relative">rel</a></p>',
    )
    assert clean == (
        '<p><a href="https://ok.example/a" target="_blank" rel="noopener noreferrer">ok</a> '
        '<a href="mailto:lab@iitr.ac.in" target="_blank" rel="noopener noreferrer">mail</a> js js2 data rel</p>'
    )


def test_colours_snap_to_the_palette():
    assert palette_color("#b91c1c", "text") == "var(--rt-red)"
    assert palette_color("rgb(29, 78, 216)", "text") == "var(--rt-blue)"
    assert palette_color("green", "text") == "var(--rt-green)"
    assert palette_color("#000000", "text") is None
    assert palette_color("#ffffff", "text") is None
    assert palette_color("#6b7280", "text") == "var(--rt-gray)"
    assert palette_color("var(--rt-purple)", "text") == "var(--rt-purple)"
    assert palette_color("var(--rt-hl-yellow)", "text") is None
    assert palette_color("var(--rt-evil)", "text") is None
    assert palette_color("#fef08a", "highlight") == "var(--rt-hl-yellow)"
    assert palette_color("rgba(0, 0, 0, 0)", "highlight") is None


def test_font_families_are_limited_to_allowed_tokens():
    kept = (
        '<p><span style="font-family: var(--rt-font-serif); color: var(--rt-red)">serif</span> '
        '<span style="font-family: var(--rt-font-devanagari)">हिंदी</span></p>'
    )
    assert sanitize_rich_text(kept) == kept
    for name in ("sans", "mono", "verdana", "tahoma", "trebuchet", "georgia", "garamond", "courier"):
        assert font_token(f"var(--rt-font-{name})") == f"var(--rt-font-{name})"

    assert font_token('"Times New Roman", serif') == "var(--rt-font-serif)"
    assert font_token("Calibri, sans-serif") == "var(--rt-font-sans)"
    assert font_token("Cambria") == "var(--rt-font-serif)"
    assert font_token("'Courier New'") == "var(--rt-font-courier)"
    assert font_token("Consolas") == "var(--rt-font-mono)"
    assert font_token("Mangal") == "var(--rt-font-devanagari)"
    assert font_token("Wingdings, Comic Sans MS") is None
    assert font_token("var(--rt-font-evil)") is None
    assert font_token("var(--rt-red)") is None

    dirty = (
        '<span style="font-family: expression(alert(1))">a</span>'
        '<span style="font-family: x; background-image: url(javascript:alert(1))">b</span>'
        '<span style="font-family: var(--rt-font-serif), url(https://evil/x.woff)">c</span>'
        '<span style="font-family: Papyrus">d</span>'
        '<span style="font-family: \'Arial\'; font-size: 30px">e</span>'
    )
    assert sanitize_rich_text(dirty) == 'abcd<span style="font-family: var(--rt-font-sans)">e</span>'
    assert palette_color("var(--rt-font-serif)", "text") is None

    pasted = '<p style="font-family: Cambria"><span style="font-family: &quot;Courier New&quot;">code</span> body</p>'
    assert sanitize_rich_text(pasted) == (
        '<p><span style="font-family: var(--rt-font-serif)">'
        '<span style="font-family: var(--rt-font-courier)">code</span> body</span></p>'
    )
    assert rich_text_to_plain(pasted) == "code body"


def test_legacy_editor_markup_is_converted():
    legacy = (
        '<div style="text-align: center; font-family: Arial">Centre</div>'
        '<h1>Big</h1><blockquote>Quote</blockquote>'
        '<span style="font-weight: bold; font-style: italic; text-decoration: underline line-through">all</span> '
        '<font color="#b91c1c" size="5" face="Arial">red</font> '
        '<span style="background-color: rgb(254, 240, 138);">hl</span> '
        '<span style="background: #bbf7d0; color: #1d4ed8">both</span> '
        '<b style="font-weight: normal" id="docs-internal-guid-1"><span style="font-weight: 700">gdocs</span></b>'
    )
    assert sanitize_rich_text(legacy) == (
        '<p style="text-align: center"><span style="font-family: var(--rt-font-sans)">Centre</span></p>'
        "<h3>Big</h3><p>Quote</p>"
        "<strong><em><u><s>all</s></u></em></strong> "
        '<span style="font-family: var(--rt-font-sans); color: var(--rt-red)">red</span> '
        '<mark style="background-color: var(--rt-hl-yellow)">hl</mark> '
        '<mark style="background-color: var(--rt-hl-green)"><span style="color: var(--rt-blue)">both</span></mark> '
        "<strong>gdocs</strong>"
    )


def test_plain_text_is_kept_and_converted_for_emails():
    assert sanitize_rich_text("  a < b\r\nc  ") == "a < b\nc"
    assert rich_text_to_plain("Line 1\nLine 2") == "Line 1\nLine 2"
    html = (
        "<p>Intro</p><ul><li><p>One</p><ul><li><p>Nested</p></li></ul></li><li><p>Two</p></li></ul>"
        '<ol start="2"><li><p>Second</p></li><li><p>Third</p></li></ol><p>a<br>b &amp; c</p>'
    )
    assert rich_text_to_plain(html) == "Intro\n• One\n  • Nested\n• Two\n2. Second\n3. Third\na\nb & c"


def test_clean_important_instruction_limits():
    assert clean_important_instruction("<p> </p><p><br></p>") == ("", None)
    assert clean_important_instruction("<p>" + "x" * 5000 + "</p>") == ("<p>" + "x" * 5000 + "</p>", None)
    _value, error = clean_important_instruction("<p>" + "x" * 5001 + "</p>")
    assert "5000 characters" in error
    heavy = "".join('<p style="text-align: center"><strong><em><u>x</u></em></strong></p>' for _ in range(400))
    _value, error = clean_important_instruction(heavy)
    assert "too much formatting" in error


@pytest.mark.django_db
def test_per_user_type_instruction_saved_and_resolved(egs_factory):
    equipment = egs_factory.equipment()
    oic = UserFactory(user_type=UserType.MANAGER, admin_approved=True)
    EquipmentManager.objects.create(equipment=equipment, manager=oic)
    url = f"/api/oic/equipment-settings/{equipment.pk}/"

    listing = _client(oic).get("/api/oic/equipment-settings/").data
    assert {"value": UserType.STUDENT, "label": "IITR Student"} in listing["instruction_user_types"]
    assert all(o["value"] not in (UserType.MANAGER, UserType.ADMIN) for o in listing["instruction_user_types"])

    res = _client(oic).patch(
        url,
        {
            "important_instruction": '<p><b>Default</b> note</p><ul><li><p onclick="x()">Point</p></li></ul>',
            "important_instruction_by_user_type": {
                UserType.STUDENT: '<p style="color: #b91c1c">Students: bring ID</p><script>x()</script>',
                UserType.FACULTY: "   ",
            },
        },
        format="json",
    )
    assert res.status_code == 200, res.data
    settings = res.data["equipment"]["settings"]
    assert settings["important_instruction"] == "<p><strong>Default</strong> note</p><ul><li><p>Point</p></li></ul>"
    assert settings["important_instruction_by_user_type"] == {UserType.STUDENT: "<p>Students: bring ID</p>"}

    student = egs_factory.student()
    faculty = UserFactory(user_type=UserType.FACULTY, admin_approved=True)
    detail = f"/api/equipments/{equipment.pk}/"
    assert _client(student).get(detail).data["important_instruction"] == "<p>Students: bring ID</p>"
    assert _client(faculty).get(detail).data["important_instruction"] == (
        "<p><strong>Default</strong> note</p><ul><li><p>Point</p></li></ul>"
    )
    assert _client(oic).get(detail, {"all_input_fields": "1"}).data["important_instruction"] == (
        "<p><strong>Default</strong> note</p><ul><li><p>Point</p></li></ul>"
    )

    bad = _client(oic).patch(url, {"important_instruction_by_user_type": {UserType.ADMIN: "x"}}, format="json")
    assert bad.status_code == 400
    assert set(bad.data["errors"]) == {"important_instruction_by_user_type"}

    too_long = _client(oic).patch(url, {"important_instruction": "<p>" + "y" * 5001 + "</p>"}, format="json")
    assert too_long.status_code == 400
    assert set(too_long.data["errors"]) == {"important_instruction"}


@pytest.mark.django_db
def test_admin_write_paths_sanitize_the_instruction(egs_factory):
    equipment = egs_factory.equipment()
    dirty = '<p onclick="x()">Hi <a href="javascript:alert(1)">there</a></p><img src=x onerror=alert(1)>'

    serializer = EquipmentAdminWriteSerializer(equipment, data={"important_instruction": dirty}, partial=True)
    assert serializer.is_valid(), serializer.errors
    serializer.save()
    equipment.refresh_from_db()
    assert equipment.important_instruction == "<p>Hi there</p>"

    blank = EquipmentAdminWriteSerializer(equipment, data={"important_instruction": "<p></p>"}, partial=True)
    assert blank.is_valid(), blank.errors
    blank.save()
    equipment.refresh_from_db()
    assert equipment.important_instruction is None

    form = EquipmentAdminForm(instance=equipment)
    form.cleaned_data = {"important_instruction": "<script>x()</script><p><u>Gloves</u></p>"}
    assert form.clean_important_instruction() == "<p><u>Gloves</u></p>"

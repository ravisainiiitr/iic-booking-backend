"""Print orientation: parsing, rotating, auto-orient (least support) and the orientation stored per part."""

from __future__ import annotations

import numpy as np
import pytest

from iic_booking.equipment.fabrication import build_print_parts, format_part_line
from iic_booking.equipment.models import PrintAnalysis
from iic_booking.equipment.print_estimate_model import PROFILE_BINS, compute_features, estimate
from iic_booking.equipment.print_orientation import (
    candidate_orientations,
    face_down_rotation,
    orientation_key,
    parse_orientation,
    rotate_triangles,
)

from .fabrication_helpers import funded_student, print_equipment, print_material
from .test_print_estimate_model import _analyze, boxes, fdm, media_tmp, mushroom, stl_bytes  # noqa: F401


def bracket():
    """An L bracket standing up: the arm overhangs 30 x 20 mm at 30 mm. Lying on its front it needs no support."""
    return boxes((0, 0, 0, 10, 20, 40), (10, 0, 30, 30, 20, 10))


def test_parse_orientation_accepts_rotations_only():
    assert parse_orientation(None) == (None, None)
    assert parse_orientation([1, 0, 0, 0, 1, 0, 0, 0, 1]) == (None, None)
    m, err = parse_orientation([[1, 0, 0], [0, 0, -1], [0, 1, 0]])
    assert err is None and m == (1.0, 0.0, 0.0, 0.0, 0.0, -1.0, 0.0, 1.0, 0.0)
    assert parse_orientation("1,0,0,0,0,-1,0,1,0")[0] == m
    assert parse_orientation([2, 0, 0, 0, 1, 0, 0, 0, 1])[1]  # scaled
    assert parse_orientation([-1, 0, 0, 0, 1, 0, 0, 0, 1])[1]  # mirrored
    assert parse_orientation([1, 0, 0])[1]
    assert parse_orientation("a,b")[1]


def test_face_down_rotation_turns_the_face_to_the_plate():
    for d in [(1, 0, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1), (0.3, -0.5, 0.81)]:
        r = np.asarray(face_down_rotation(d)).reshape(3, 3)
        v = np.asarray(d, dtype=float) / np.linalg.norm(d)
        assert np.allclose(r @ v, (0, 0, -1), atol=1e-5)
        assert np.linalg.det(r) == pytest.approx(1.0, abs=1e-5)


def test_candidates_cover_each_side_and_the_largest_flat_faces():
    cands = candidate_orientations(bracket())
    labels = [c["label"] for c in cands]
    assert labels[:6] == ["As uploaded", "Upside down", "On its right side", "On its left side", "On its back", "On its front"]
    assert cands[0]["orientation"] is None
    # The bracket's largest faces are its sides, the same as two of the axis candidates: no duplicates.
    assert len(cands) == 6


def test_turning_the_bracket_removes_its_supports_and_the_time_profile_is_cumulative():
    tris = bracket()
    upright = estimate(compute_features(tris), fdm(), infill_percent=20, density_g_cm3=1.24)
    front_down = face_down_rotation((0, -1, 0))
    lying = estimate(compute_features(rotate_triangles(tris, front_down)), fdm(), infill_percent=20, density_g_cm3=1.24)
    assert upright.support_mode == "buildplate" and upright.support_g > 0
    assert lying.support_mode == "none" and lying.support_g == 0
    assert lying.total_g < upright.total_g
    p = upright.to_dict()["progress"]
    assert len(p) == PROFILE_BINS and p[-1] == pytest.approx(1.0) and all(b >= a for a, b in zip(p, p[1:]))


@pytest.mark.django_db
def test_auto_orient_suggests_least_support_and_orientation_is_stored(egs_factory, media_tmp):  # noqa: F811
    eq = print_equipment(egs_factory)
    pla = print_material(eq)
    student, _ = funded_student(egs_factory)
    client = egs_factory.client_for(student)
    resp = _analyze(client, eq, pla, stl_bytes(bracket()), density_percent="20", support_mode="buildplate")
    assert resp.status_code == 200, resp.data
    analysis_id = resp.data["id"]
    first_weight = float(resp.data["weight_grams"])
    assert resp.data["bounding_box"]["size"]["z"] == pytest.approx(40)

    resp = client.get(f"/api/print-analyses/{analysis_id}/orientations/")
    assert resp.status_code == 200, resp.data
    rows = resp.data["candidates"]
    current = rows[resp.data["current_index"]]
    best = rows[resp.data["best_index"]]
    assert current["is_current"] and current["label"] == "As uploaded" and current["support_g"] > 0
    assert best["support_g"] == 0 and best["fits"] and best["orientation"]
    assert resp.data["saving"]["support_g"] == pytest.approx(current["support_g"])
    assert resp.data["scored_support_mode"] == "buildplate"

    # What-if estimate with the suggested orientation (nothing saved).
    preview = client.get(
        f"/api/print-analyses/{analysis_id}/estimate/", {"orientation": ",".join(str(v) for v in best["orientation"])}
    )
    assert preview.status_code == 200, preview.data
    assert preview.data["estimate_breakdown"]["support_g"] == 0
    assert preview.data["weight_grams"] < first_weight
    analysis = PrintAnalysis.objects.get(pk=analysis_id)
    assert "orientation" not in analysis.slicer_settings

    url = f"/api/print-analyses/{analysis_id}/recalculate/"
    resp = client.patch(url, {"material_id": pla.pk, "density_percent": 20, "orientation": best["orientation"]}, format="json")
    assert resp.status_code == 200, resp.data
    assert resp.data["estimate_breakdown"]["support_g"] == 0
    assert float(resp.data["weight_grams"]) < first_weight
    assert resp.data["bounding_box"]["size"]["z"] == pytest.approx(best["size_mm"][2], abs=0.01)
    analysis.refresh_from_db()
    assert analysis.slicer_settings["orientation"] == best["orientation"]
    assert analysis.bounding_box["_orientation"] == orientation_key(best["orientation"])

    # Changing only the density keeps the orientation.
    resp = client.patch(url, {"material_id": pla.pk, "density_percent": 40}, format="json")
    assert resp.status_code == 200 and resp.data["estimate_breakdown"]["support_g"] == 0
    analysis.refresh_from_db()
    assert analysis.slicer_settings["orientation"] == best["orientation"]

    # The auto-orient search now starts from the stored orientation.
    resp = client.get(f"/api/print-analyses/{analysis_id}/orientations/")
    assert resp.data["candidates"][resp.data["current_index"]]["support_g"] == 0
    assert resp.data["saving"]["support_g"] == 0

    parts = build_print_parts([analysis])
    assert parts[0]["orientation"] == best["orientation"]
    assert "user-selected orientation" in format_part_line(parts[0])

    # Reset, and refuse something that is not a rotation.
    resp = client.patch(url, {"material_id": pla.pk, "density_percent": 20, "orientation": None}, format="json")
    assert resp.status_code == 200
    assert float(resp.data["weight_grams"]) == pytest.approx(first_weight)
    assert resp.data["bounding_box"]["size"]["z"] == pytest.approx(40)
    analysis.refresh_from_db()
    assert "orientation" not in analysis.slicer_settings and "_orientation" not in analysis.bounding_box
    assert "orientation" not in build_print_parts([analysis])[0]
    bad = client.patch(url, {"material_id": pla.pk, "orientation": [2, 0, 0, 0, 1, 0, 0, 0, 1]}, format="json")
    assert bad.status_code == 400

    other = egs_factory.client_for(egs_factory.student())
    assert other.get(f"/api/print-analyses/{analysis_id}/orientations/").status_code == 404


@pytest.mark.django_db
def test_orientation_search_for_a_mushroom_beats_cap_up(egs_factory, media_tmp):  # noqa: F811
    eq = print_equipment(egs_factory)
    pla = print_material(eq)
    student, _ = funded_student(egs_factory)
    client = egs_factory.client_for(student)
    resp = _analyze(client, eq, pla, stl_bytes(mushroom()), support_mode="everywhere")
    assert resp.status_code == 200, resp.data
    resp = client.get(f"/api/print-analyses/{resp.data['id']}/orientations/", {"support_mode": "none"})
    assert resp.status_code == 200, resp.data
    assert resp.data["scored_support_mode"] == "auto"  # supports off: still compared by what they would need
    rows = {r["label"]: r for r in resp.data["candidates"]}
    best = resp.data["candidates"][resp.data["best_index"]]
    assert rows["Upside down"]["support_g"] < rows["As uploaded"]["support_g"]
    assert best["label"] != "As uploaded"
    assert best["support_g"] == min(r["support_g"] for r in rows.values())
    assert resp.data["saving"]["support_g"] > 0

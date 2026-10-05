"""DXF measuring (units, blocks, circles) and the laser sheet-cost formula."""

from __future__ import annotations

from decimal import Decimal

import pytest

from iic_booking.equipment.laser_cut_service import (
    DxfParseError,
    analyze_dxf_bytes,
    laser_part_material_cost,
    part_fits_sheet,
)

from .fabrication_helpers import dxf_bytes


def test_millimetre_rectangle_is_measured_exactly():
    result = analyze_dxf_bytes(dxf_bytes(units=4, rects=((10, 20, 200, 100),)))
    assert result.detected_units == "mm"
    assert result.units == "mm"
    assert not result.units_assumed
    assert result.width_mm == Decimal("200.000")
    assert result.height_mm == Decimal("100.000")
    assert result.area_mm2 == Decimal("20000.000")
    assert result.entity_count == 1


@pytest.mark.parametrize(
    "insunits, expected_w, expected_h",
    [
        (1, Decimal("50.800"), Decimal("25.400")),  # inches
        (2, Decimal("609.600"), Decimal("304.800")),  # feet
        (5, Decimal("20.000"), Decimal("10.000")),  # cm
        (6, Decimal("2000.000"), Decimal("1000.000")),  # m
    ],
)
def test_insunits_are_converted_to_millimetres(insunits, expected_w, expected_h):
    result = analyze_dxf_bytes(dxf_bytes(units=insunits, rects=((0, 0, 2, 1),)))
    assert (result.width_mm, result.height_mm) == (expected_w, expected_h)


def test_unitless_drawing_assumes_mm_and_accepts_an_override():
    data = dxf_bytes(units=None, rects=((0, 0, 20, 10),))
    assumed = analyze_dxf_bytes(data)
    assert assumed.detected_units == "unitless"
    assert assumed.units == "mm" and assumed.units_assumed
    assert (assumed.width_mm, assumed.height_mm) == (Decimal("20.000"), Decimal("10.000"))
    assert any("no units" in w for w in assumed.warnings)

    in_cm = analyze_dxf_bytes(data, units_override="cm")
    assert in_cm.units == "cm"
    assert (in_cm.width_mm, in_cm.height_mm) == (Decimal("200.000"), Decimal("100.000"))


def test_block_inserts_are_expanded_with_their_scale():
    data = dxf_bytes(
        rects=((0, 0, 5, 5),),
        blocks={"SQ": [(0, 0, 10, 10)]},
        inserts=[("SQ", (100, 50), 2)],
    )
    result = analyze_dxf_bytes(data)
    assert (result.width_mm, result.height_mm) == (Decimal("120.000"), Decimal("70.000"))
    assert result.entity_count == 2


def test_circle_bbox_is_its_diameter():
    result = analyze_dxf_bytes(dxf_bytes(rects=(), circles=((50, 50, 25),)))
    assert (result.width_mm, result.height_mm) == (Decimal("50.000"), Decimal("50.000"))


def test_drawing_without_cut_geometry_or_garbage_is_rejected():
    import io

    import ezdxf

    doc = ezdxf.new("R2010")
    doc.modelspace().add_text("hello")
    stream = io.StringIO()
    doc.write(stream)
    with pytest.raises(DxfParseError, match="No cut geometry"):
        analyze_dxf_bytes(stream.getvalue().encode())
    with pytest.raises(DxfParseError):
        analyze_dxf_bytes(b"this is not a dxf at all")
    with pytest.raises(DxfParseError, match="empty"):
        analyze_dxf_bytes(b"")


def test_worked_example_200x100_qty5_acrylic_3mm_costs_202_43():
    cost = laser_part_material_cost(Decimal("20000"), 5, Decimal("2438.4"), Decimal("1219.2"), Decimal("6018"))
    assert cost == Decimal("202.43")


def test_cost_scales_with_quantity_and_rounds_half_up():
    one = laser_part_material_cost(Decimal("20000"), 1, Decimal("2438.4"), Decimal("1219.2"), Decimal("6018"))
    assert one == Decimal("40.49")
    assert laser_part_material_cost(Decimal("1"), 1, Decimal("1"), Decimal("1"), Decimal("0.005")) == Decimal("0.01")


def test_part_fits_sheet_in_either_orientation():
    assert part_fits_sheet(2400, 1200, Decimal("2438.4"), Decimal("1219.2"))
    assert part_fits_sheet(1200, 2400, Decimal("2438.4"), Decimal("1219.2"))
    assert not part_fits_sheet(1300, 2400, Decimal("2438.4"), Decimal("1219.2"))
    assert not part_fits_sheet(2500, 100, Decimal("2438.4"), Decimal("1219.2"))

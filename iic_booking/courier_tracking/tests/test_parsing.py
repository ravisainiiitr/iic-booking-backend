
import pytest

from iic_booking.courier_tracking.parsing import (
    normalize_tracking_number,
    tracking_number_problem,
)



# --- tracking numbers -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [(" ee 123 456 789 in ", "EE123456789IN"), ("d3010377227", "D3010377227"),
                                          ("7X1188\t22611", "7X118822611")])
def test_normalisation_removes_spaces_and_uppercases(raw, expected):
    assert normalize_tracking_number(raw) == expected


@pytest.mark.parametrize("slug", ["indiapost", "speedpost", "registeredpost", "indiapostinternational"])
def test_postal_numbers_must_match_documented_format(slug):
    assert tracking_number_problem(slug, "EE123456789IN") == ""
    assert "13 characters" in tracking_number_problem(slug, "EE12345678IN")
    assert "13 characters" in tracking_number_problem(slug, "123456789012I")


def test_other_couriers_get_only_generic_bounds():
    assert tracking_number_problem("dtdc", "D3010377227") == ""
    assert tracking_number_problem("bluedart", "1234") != ""  # too short for any courier
    assert tracking_number_problem("bluedart", "") != ""


# --- parsing ----------------------------------------------------------------------------------------------------------

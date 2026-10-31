"""Time formulas are admin text evaluated on the server: only arithmetic and the formula functions, never Python."""

from types import SimpleNamespace

import pytest
from django.core.exceptions import ValidationError

from iic_booking.equipment.calculators import TimeCalculationEngine


def _time(formula, **inputs):
    profile = SimpleNamespace(time_formula=formula, equipment=None)
    return TimeCalculationEngine._calculate_formula_time(profile, inputs, 30)


def test_arithmetic_and_formula_functions_still_work():
    assert _time("A * 30", A=3) == 90
    assert _time("ceil(A / 2) * 30 + B", A=3, B=15) == 75
    assert _time("max(A, 2) * 10", A=1) == 20


@pytest.mark.parametrize("formula", [
    "__import__('os').getpid()",
    "open('/etc/passwd').read()",
    "(1).__class__",
    "[c for c in ().__class__.__base__.__subclasses__()]",
])
def test_python_in_a_time_formula_is_refused(formula):
    with pytest.raises(ValidationError):
        _time(formula, A=1)

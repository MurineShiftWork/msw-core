"""Direct tests of the valve curve-fitting service (no model wrapper)."""

import numpy as np
import pytest

from murineshiftwork.logic.calibration import valve_fitting as vf

_LINEAR = [[t, 100.0 * t + 2.0] for t in (0.01, 0.02, 0.05, 0.10, 0.20)]
_EXP = [[t, np.exp(15.0 * t) + 0.5] for t in (0.01, 0.03, 0.06, 0.10, 0.15)]


def test_clean_points_sorts_and_drops_dead_zone():
    s, ul = vf.clean_points([[0.2, 5.0], [0.05, 0.0], [0.1, 2.0]])
    assert s.tolist() == [0.1, 0.2]
    assert ul.tolist() == [2.0, 5.0]


def test_fit_linear_recovers_line():
    a, b = vf.fit(_LINEAR, "linear")
    assert a == pytest.approx(100.0, rel=1e-6)
    assert b == pytest.approx(2.0, abs=1e-6)


def test_fit_exponential_recovers_parameters():
    a, b, c = vf.fit(_EXP, "exponential")
    assert (a, b, c) == pytest.approx((1.0, 15.0, 0.5), rel=1e-3)


def test_fit_needs_three_positive_points():
    with pytest.raises(ValueError, match="Too few"):
        vf.fit([[0.01, 1.0], [0.02, 2.0], [0.03, 0.0]], "linear")


@pytest.mark.parametrize(
    ("points", "model"), [(_LINEAR, "linear"), (_EXP, "exponential")]
)
def test_ul_and_s_round_trip(points, model):
    s = 0.07
    ul = vf.ul_for_s(points, model, s)
    assert vf.s_for_ul(points, model, ul) == pytest.approx(s, abs=1e-3)


def test_check_quality_flags_non_monotonic():
    ok, reason = vf.check_quality([[0.01, 1.0], [0.02, 3.0], [0.03, 2.0]], "linear")
    assert not ok
    assert "monotonically" in reason


def test_check_quality_passes_clean_data():
    ok, reason = vf.check_quality(_LINEAR, "linear")
    assert ok, reason

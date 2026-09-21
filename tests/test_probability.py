import math

import pytest

from ginko.core.probability import activity_probability


def test_seconds_are_converted_to_hours():
    assert activity_probability(0.25, 600) == pytest.approx(0.04081054289)
    assert activity_probability(0, 600) == 0
    assert activity_probability(1, 0) == 0
    assert activity_probability(1, 3600) == pytest.approx(1 - math.exp(-1))


@pytest.mark.parametrize("rate,seconds", [(-1, 600), (1, -1), (math.inf, 60), (1, math.nan)])
def test_invalid_rates_and_intervals_are_rejected(rate, seconds):
    with pytest.raises(ValueError):
        activity_probability(rate, seconds)
